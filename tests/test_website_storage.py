import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber
from fastapi.testclient import TestClient

import app as web
from karry_agent import instant_website as sites
from karry_agent import website_storage as storage
from karry_agent.website_migration import migrate

HTML = '<!doctype html><html lang="ja"><title>Persistent sample</title><h1>Durable HTML</h1></html>'
NAME = 'sample_' + 'a' * 32 + '.html'


@pytest.fixture(autouse=True)
def configured(monkeypatch, tmp_path):
    for name in list(storage.os.environ):
        if name.startswith('KARRY_WEBSITE_') or name.startswith('AWS_') or name in {'KARRY_API_TOKEN','PARTNER_NAME','CORE_USER_NAME','CORE_PRIVATE_CONTEXT'}:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('KARRY_WEBSITE_STORAGE','s3')
    monkeypatch.setenv('KARRY_WEBSITE_S3_BUCKET','sample-bucket')
    monkeypatch.setenv('KARRY_WEBSITE_S3_ENDPOINT','https://' + 'a' * 32 + '.r2.cloudflarestorage.com')
    monkeypatch.setenv('KARRY_WEBSITE_S3_ACCESS_KEY_ID','synthetic-access-key')
    monkeypatch.setenv('KARRY_WEBSITE_S3_SECRET_ACCESS_KEY','synthetic-secret-key')
    monkeypatch.setenv('RENDER_EXTERNAL_URL','https://example.onrender.com')
    monkeypatch.setattr(sites,'INSTANT_DIR',tmp_path/'instant')
    monkeypatch.setattr(sites,'_PENDING',{})


def provider_error(code):
    return ClientError({'Error':{'Code':code,'Message':'provider-private-details'}},'ObjectOperation')


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.put_calls = []
        self.streams = []
    def list_objects_v2(self, **kw):
        contents=[{'Key':key,'Size':len(obj['Body'])} for (bucket,key),obj in self.objects.items() if bucket==kw['Bucket'] and key.startswith(kw['Prefix'])]
        limit=kw.get('MaxKeys',1000)
        return {'Contents':contents[:limit],'IsTruncated':len(contents)>limit}
    def put_object(self, **kw):
        key=(kw['Bucket'],kw['Key'])
        self.put_calls.append(kw)
        if kw.get('IfNoneMatch')=='*' and key in self.objects:
            raise provider_error('PreconditionFailed')
        self.objects[key]=dict(kw)
        return {'ETag':'"value"'}
    def head_object(self, **kw):
        obj=self.objects.get((kw['Bucket'],kw['Key']))
        if obj is None: raise provider_error('NoSuchKey')
        return {'ContentLength':len(obj['Body']), 'ContentType':obj['ContentType'],'Metadata':obj['Metadata']}
    def get_object(self, **kw):
        head=self.head_object(**kw)
        obj=self.objects[(kw['Bucket'],kw['Key'])]
        stream=io.BytesIO(obj['Body']); self.streams.append(stream)
        return {**head, 'Body':stream}


@pytest.fixture
def fake(monkeypatch):
    client=FakeS3()
    monkeypatch.setattr(storage,'_client',lambda config:client)
    return client


def test_r2_config_and_no_secret_repr():
    config=storage.load_storage_config()
    assert config.region=='auto' and config.prefix=='karry-websites/'
    assert 'synthetic-secret-key' not in repr(config) and 'synthetic-access-key' not in repr(config)


def test_aws_config_without_custom_endpoint(monkeypatch):
    monkeypatch.delenv('KARRY_WEBSITE_S3_ENDPOINT')
    monkeypatch.setenv('KARRY_WEBSITE_S3_REGION','ap-northeast-1')
    config=storage.load_storage_config()
    assert config.endpoint is None and config.region=='ap-northeast-1'


def test_sdk_factory_uses_explicit_credentials_and_bounded_retries(monkeypatch):
    storage._client.cache_clear()
    factory=Mock()
    monkeypatch.setattr(storage.boto3,'client',factory)
    try:
        config=storage.load_storage_config(); storage._client(config)
        opts=factory.call_args.kwargs
        assert opts['aws_access_key_id']==config.access_key
        assert opts['config'].signature_version=='s3v4'
        assert opts['config'].retries['total_max_attempts']==2
        assert opts['config'].s3['addressing_style']=='path'
        assert opts['config'].connect_timeout==3 and opts['config'].read_timeout==10
    finally:
        storage._client.cache_clear()


@pytest.mark.parametrize('key,value',[
    ('KARRY_WEBSITE_S3_BUCKET',''),('KARRY_WEBSITE_S3_BUCKET','bad/path'),('KARRY_WEBSITE_S3_PREFIX','../private/'),
    ('KARRY_WEBSITE_S3_ENDPOINT','http://localhost:9000'),('KARRY_WEBSITE_S3_ENDPOINT','https://user:key@example.com'),
    ('KARRY_WEBSITE_S3_ENDPOINT','https://example.com'),('KARRY_WEBSITE_S3_ENDPOINT','https://'+'a'*32+'.r2.cloudflarestorage.com/path'),
    ('KARRY_WEBSITE_S3_REGION','bad/region'),('KARRY_WEBSITE_S3_SECRET_ACCESS_KEY','')])
def test_invalid_configuration_fails_before_storage_or_local_write(key,value,monkeypatch):
    monkeypatch.setenv(key,value)
    factory=Mock(); monkeypatch.setattr(storage,'_client',factory)
    result=sites.deploy_instant_website(HTML)
    assert result['status']=='error' and result['error_code']=='storage_configuration'
    factory.assert_not_called(); assert not sites.INSTANT_DIR.exists()


def test_default_is_durable_and_configuration_failure_has_no_fallback(monkeypatch):
    monkeypatch.delenv('KARRY_WEBSITE_STORAGE')
    monkeypatch.delenv('KARRY_WEBSITE_S3_BUCKET')
    assert storage.storage_mode()=='s3'
    result=sites.deploy_instant_website(HTML)
    assert not result['success'] and result['error_code']=='storage_configuration'
    assert not sites.INSTANT_DIR.exists()


def test_unknown_mode_is_not_silently_local(monkeypatch):
    monkeypatch.setenv('KARRY_WEBSITE_STORAGE','unknown')
    assert sites.deploy_instant_website(HTML)['error_code']=='storage_configuration'
    assert not sites.INSTANT_DIR.exists()


def test_put_is_conditional_checked_and_durable(fake):
    result=sites.deploy_instant_website(HTML)
    assert result['success'] and result['persistence']=='durable'
    assert fake.put_calls[0]['IfNoneMatch']=='*'
    assert fake.put_calls[0]['Metadata']['sha256']==hashlib.sha256(HTML.encode()).hexdigest()
    assert fake.put_calls[0]['ContentType']=='text/html; charset=utf-8'
    assert 'ACL' not in fake.put_calls[0] and not sites.INSTANT_DIR.exists()
    assert result['public_url']=='https://example.onrender.com/static/instant/'+result['filename']


def test_head_validation_failure_is_not_reported_as_publication_success(fake,monkeypatch):
    monkeypatch.setattr(fake,'head_object',lambda **kw:{'ContentLength':1,'Metadata':{},'ContentType':'text/html'})
    result=sites.deploy_instant_website(HTML)
    assert not result['success'] and result['error_code']=='storage_integrity'
    assert not sites.INSTANT_DIR.exists()


def test_stored_html_survives_local_files_and_process_memory_reset(fake,monkeypatch,tmp_path):
    result=sites.deploy_instant_website(HTML)
    assert result['success']
    # A new app-store instance loads only remote objects. No local write/read permitted.
    monkeypatch.setattr(sites,'_PENDING',{})
    monkeypatch.setattr(sites,'INSTANT_DIR',tmp_path/'new-container'/'instant')
    monkeypatch.setattr(Path,'mkdir',Mock(side_effect=AssertionError('must not use local disk')))
    response=TestClient(web.app).get('/static/instant/'+result['filename'])
    assert response.status_code==200 and response.text==HTML
    assert 'sandbox allow-scripts' in response.headers['content-security-policy']
    assert 'allow-same-origin' not in response.headers['content-security-policy']
    assert response.headers['cache-control']=='no-store'
    assert fake.streams[-1].closed


def test_alternate_url_routes_use_remote_object_and_csp(fake):
    result=sites.deploy_instant_website(HTML)
    for route in ['/static//instant/','/static///instant/']:
        response=TestClient(web.app).get(route+result['filename'])
        assert response.status_code==200 and response.text==HTML
        assert 'sandbox allow-scripts' in response.headers['content-security-policy']


def test_unknown_remote_key_returns_404_without_falling_back_to_local(fake):
    sites.INSTANT_DIR.mkdir(); (sites.INSTANT_DIR/NAME).write_text('old-local-content')
    response=TestClient(web.app).get('/static/instant/'+NAME)
    assert response.status_code==404


def test_missing_artifact_returns_actionable_json_and_html(fake):
    client=TestClient(web.app)
    response=client.get('/static/instant/'+NAME, headers={'Accept':'application/json'})
    body=response.json()
    assert response.status_code==404 and '成果物が見つかりません' in body['detail']
    assert body['error_code']=='artifact_not_found' and '公開' in body['hint']
    assert response.headers['cache-control']=='no-store'
    page=client.get('/static/instant/'+NAME, headers={'Accept':'text/html'})
    assert page.status_code==404 and page.headers['content-type'].startswith('text/html')
    assert '成果物が見つかりません' in page.text and 'href="/"' in page.text


def test_artifact_status_api_reports_publication_without_404(fake,monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN','synthetic-owner')
    client=TestClient(web.app); auth={'Authorization':'Bearer synthetic-owner'}
    assert client.get('/api/artifacts/'+NAME).status_code==401
    missing=client.get('/api/artifacts/'+NAME, headers=auth)
    assert missing.status_code==200 and missing.json()['published'] is False
    assert missing.json()['error_code']=='artifact_not_found' and missing.json()['public_url'] is None
    invalid=client.get('/api/artifacts/unmanaged.html', headers=auth)
    assert invalid.status_code==200 and invalid.json()['error_code']=='invalid_filename'
    name=sites.deploy_instant_website(HTML)['filename']
    found=client.get('/api/artifacts/'+name, headers=auth).json()
    assert found['published'] is True and found['public_url']=='https://example.onrender.com/static/instant/'+name


def test_bad_filename_never_queries_storage(fake,monkeypatch):
    get=Mock(); monkeypatch.setattr(fake,'get_object',get)
    assert TestClient(web.app).get('/static//instant/unmanaged.html').status_code==404
    get.assert_not_called()


@pytest.mark.parametrize('error', [provider_error('AccessDenied'), EndpointConnectionError(endpoint_url='https://provider-private-details')])
def test_remote_failure_returns_503_not_secret_or_local(error,fake,monkeypatch):
    monkeypatch.setattr(fake,'get_object',Mock(side_effect=error))
    response=TestClient(web.app).get('/static/instant/'+NAME)
    assert response.status_code==503 and 'provider-private-details' not in response.text
    assert not sites.INSTANT_DIR.exists()


@pytest.mark.parametrize('change', ['length','hash','type','oversize'])
def test_corrupt_or_oversized_objects_are_not_served(change,fake):
    config=storage.load_storage_config(); key=(config.bucket,config.key(NAME))
    body=HTML.encode()
    fake.objects[key]={'Body':body,'ContentType':'text/html; charset=utf-8','Metadata':{'sha256':hashlib.sha256(body).hexdigest()}}
    if change=='length':
        fake.get_object=Mock(return_value={'ContentLength':len(body)+1,'ContentType':'text/html','Metadata':{'sha256':hashlib.sha256(body).hexdigest()},'Body':io.BytesIO(body)})
    elif change=='hash': fake.objects[key]['Metadata']['sha256']='0'*64
    elif change=='type': fake.objects[key]['ContentType']='application/octet-stream'
    elif change=='oversize': fake.objects[key]['Body']=b'x'*(storage.MAX_HTML_BYTES+1)
    response=TestClient(web.app).get('/static/instant/'+NAME)
    assert response.status_code==503
    if fake.streams: assert fake.streams[-1].closed


def test_sdk_put_shape_accepts_conditions_and_checksum():
    config=storage.load_storage_config()
    client=boto3.client('s3',region_name='us-east-1',aws_access_key_id='synthetic',aws_secret_access_key='synthetic')
    import base64
    body=HTML.encode(); sha=hashlib.sha256(body).hexdigest()
    params={'Bucket':config.bucket,'Key':config.key(NAME),'Body':body,'ContentType':'text/html; charset=utf-8',
        'CacheControl':'no-store','IfNoneMatch':'*','ContentMD5':base64.b64encode(hashlib.md5(body).digest()).decode(),'Metadata':{'sha256':sha}}
    with Stubber(client) as stub:
        stub.add_response('list_objects_v2',{'Contents':[],'IsTruncated':False},{'Bucket':config.bucket,'Prefix':config.prefix,'MaxKeys':129})
        stub.add_response('put_object',{'ETag':'"value"'},params)
        stub.add_response('head_object',{'ContentLength':len(body),'ContentType':'text/html; charset=utf-8','Metadata':{'sha256':sha}}, {'Bucket':config.bucket,'Key':config.key(NAME)})
        assert storage.S3WebsiteStore(config,client).put(NAME,body)==sha
        stub.assert_no_pending_responses()


def test_conditions_never_overwrite_existing_key(fake):
    store=storage.configured_store(); store.put(NAME,HTML.encode())
    with pytest.raises(storage.StorageError) as error:
        store.put(NAME,b'<h1>Replacement</h1>')
    assert error.value.code=='already_exists' and store.get(NAME)==HTML.encode()


def test_remote_store_quota_blocks_upload(fake):
    store=storage.configured_store(); store.put(NAME,HTML.encode(),max_files=1)
    with pytest.raises(storage.StorageError) as error:
        store.put('next_'+'b'*32+'.html',HTML.encode(),max_files=1)
    assert error.value.code=='storage_capacity' and len(fake.put_calls)==1


def test_durable_preview_binds_storage_destination_and_keeps_model_unpublished(fake,monkeypatch):
    preview=sites.prepare_instant_website(HTML)
    assert preview['status']=='needs_user_action' and preview['preview']['persistence']=='durable'
    assert not fake.put_calls
    assert '_storage_identity' not in preview['preview']
    monkeypatch.setenv('KARRY_WEBSITE_S3_BUCKET','other-bucket')
    result=sites.confirm_instant_website(preview['approval_id'],preview['preview']['sha256'],True)
    assert result['status']=='error' and not fake.put_calls


def test_credential_rotation_does_not_change_storage_destination(fake,monkeypatch):
    preview=sites.prepare_instant_website(HTML)
    monkeypatch.setenv('KARRY_WEBSITE_S3_SECRET_ACCESS_KEY','rotated-synthetic-key')
    result=sites.confirm_instant_website(preview['approval_id'],preview['preview']['sha256'],True)
    assert result['success'] and result['persistence']=='durable'


def test_owner_api_durable_publish_and_authenticated_status(fake,monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN','synthetic-owner')
    client=TestClient(web.app)
    assert client.get('/api/agent/websites/storage').status_code==401
    headers={'Authorization':'Bearer synthetic-owner'}
    status=client.get('/api/agent/websites/storage',headers=headers).json()
    assert status['configuration_valid'] and status['connection_status']=='not_checked'
    assert client.get('/api/agent/websites/storage?verify=true',headers=headers).json()['connection_status']=='verified'
    result=client.post('/api/agent/tools/execute',headers=headers,json={'tool_name':'deploy_instant_website','arguments':{'html_content':HTML}}).json()['result']
    assert result['success'] and result['persistence']=='durable'
    assert not sites.INSTANT_DIR.exists()


def test_diagnostic_does_not_return_secrets_on_bad_config(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN','synthetic-owner')
    monkeypatch.delenv('KARRY_WEBSITE_S3_BUCKET')
    response=TestClient(web.app).get('/api/agent/websites/storage',headers={'Authorization':'Bearer synthetic-owner'})
    assert not response.json()['configuration_valid']
    assert 'synthetic-secret-key' not in response.text and 'synthetic-access-key' not in response.text


def test_migration_dry_run_without_credentials_or_remote_write(fake,monkeypatch,tmp_path):
    source=tmp_path/'backup'; source.mkdir(); (source/NAME).write_text(HTML)
    monkeypatch.delenv('KARRY_WEBSITE_S3_BUCKET')
    rows=migrate(source)
    assert rows[0]['status']=='dry_run' and rows[0]['filename']==NAME and not fake.put_calls
    assert (source/NAME).read_text()==HTML


def test_migration_preserves_existing_url_and_source_and_is_idempotent(fake,tmp_path):
    source=tmp_path/'backup'; source.mkdir(); (source/NAME).write_text(HTML)
    first=migrate(source,apply=True); second=migrate(source,apply=True)
    assert first[0]['status']=='migrated' and second[0]['status']=='already_stored'
    assert first[0]['public_url']=='https://example.onrender.com/static/instant/'+NAME
    assert len(fake.put_calls)==1 and (source/NAME).read_text()==HTML


def test_migration_never_overwrites_different_remote_content(fake,tmp_path):
    storage.configured_store().put(NAME,b'<h1>Old content</h1>')
    source=tmp_path/'backup'; source.mkdir(); (source/NAME).write_text(HTML)
    rows=migrate(source,apply=True)
    assert rows[0]['status']=='error' and len(fake.put_calls)==1
    assert storage.configured_store().get(NAME)==b'<h1>Old content</h1>'


def test_migration_rejects_symlink_private_or_unmanaged_source(fake,tmp_path,monkeypatch):
    source=tmp_path/'backup'; source.mkdir()
    outside=tmp_path/'outside.html'; outside.write_text(HTML)
    (source/NAME).symlink_to(outside)
    (source/'.env').write_text('private')
    rows=migrate(source,apply=True)
    assert len(rows)==1 and rows[0]['status']=='error' and not fake.put_calls


def test_new_storage_secrets_are_blocked_from_html(fake):
    result=sites.deploy_instant_website('<p>synthetic-secret-key</p>')
    assert not result['success'] and not fake.put_calls


def test_static_alias_never_bypasses_remote_backend(fake,tmp_path):
    root=tmp_path/'static'; root.mkdir(); sites.configure_static_root(root)
    try:
        sites.INSTANT_DIR.mkdir(); (sites.INSTANT_DIR/NAME).write_text('old-local-content')
        (root/'alias').symlink_to(sites.INSTANT_DIR,target_is_directory=True)
        app=web.FastAPI(); app.mount('/static',web.ArtifactStaticFiles(directory=root))
        assert TestClient(app).get('/static/alias/'+NAME).status_code==404
        storage.configured_store().put(NAME,HTML.encode())
        response=TestClient(app).get('/static/alias/'+NAME)
        assert response.status_code==200 and response.text==HTML
        assert 'sandbox allow-scripts' in response.headers['content-security-policy']
    finally:
        sites.configure_static_root(web.STATIC_DIR)


def test_failed_upload_never_creates_local_file(fake,monkeypatch):
    monkeypatch.setattr(fake,'put_object',Mock(side_effect=provider_error('AccessDenied')))
    result=sites.deploy_instant_website(HTML)
    assert not result['success'] and result['error_code']=='storage_unavailable'
    assert 'provider-private-details' not in str(result) and not sites.INSTANT_DIR.exists()


def test_read_without_declared_size_is_rejected_and_stream_closed(fake,monkeypatch):
    stream=io.BytesIO(HTML.encode())
    monkeypatch.setattr(fake,'get_object',Mock(return_value={'ContentType':'text/html','Metadata':{},'Body':stream}))
    assert TestClient(web.app).get('/static/instant/'+NAME).status_code==503
    assert stream.closed

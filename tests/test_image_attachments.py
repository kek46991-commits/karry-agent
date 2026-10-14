import asyncio
import base64
import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image
from fastapi.testclient import TestClient

import app as web
from karry_agent import image_attachments as images, task_persistence as tasks, website_storage as websites
from karry_agent.core import AgentCore, AgentResult
from test_task_persistence import FakeS3

HEADERS={'Authorization':'Bearer synthetic-owner-key'}


def picture(fmt='PNG',size=(12,8),color='blue',**kwargs):
    out=io.BytesIO()
    with Image.new('RGB',size,color) as image:image.save(out,format=fmt,**kwargs)
    return out.getvalue()


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv('KARRY_TASK_STORAGE','memory')
    monkeypatch.setenv('KARRY_API_TOKEN','synthetic-owner-key')
    monkeypatch.setenv('KARRY_WEBSITE_STORAGE','local')
    for name in ['_RUNS','_SESSIONS','_PRIVATE_SESSIONS','_PENDING_CALENDAR','_SESSION_META','_SESSION_LOCKS','_SESSION_APPROVALS']:
        monkeypatch.setattr(web,name,{})
    monkeypatch.setattr(images,'_MEMORY_DOCUMENTS',{})
    monkeypatch.setattr(images,'_MEMORY_BYTES',{})
    old=dict(web.app.dependency_overrides)
    yield
    web.app.dependency_overrides.clear();web.app.dependency_overrides.update(old)


@pytest.fixture
def durable(monkeypatch):
    monkeypatch.setenv('KARRY_TASK_STORAGE','s3')
    monkeypatch.setenv('KARRY_TASK_S3_PREFIX','karry-tasks/')
    cfg=websites.StorageConfig('private-bucket','karry-websites/',None,'us-east-1','synthetic-access','synthetic-secret')
    client=FakeS3()
    monkeypatch.setattr(tasks,'repository',lambda:tasks.S3TaskRepository(cfg,client))
    return client


def create(client):return client.post('/api/sessions',headers=HEADERS,json={}).json()['session_id']
def upload(client,sid,body=None,mime='image/png',filename='sample.png'):
    return client.post('/api/upload_image',headers=HEADERS,data={'session_id':sid},files={'file':(filename,body if body is not None else picture(),mime)})


@pytest.mark.parametrize('mime,fmt',[('image/png','PNG'),('image/jpeg','JPEG'),('image/webp','WEBP'),('image/gif','GIF')])
def test_all_four_input_formats_are_actual_decodable_model_images(mime,fmt):
    result=images.normalize(picture(fmt),mime)
    assert result.mime_type in {'image/png','image/jpeg','image/webp'}
    with Image.open(io.BytesIO(result.body)) as normalized:
        assert normalized.size==(12,8)
        assert not normalized.getexif()
    assert result.mime_type=='image/png' if fmt=='GIF' else result.mime_type==mime


def test_jpeg_orientation_is_correct_and_metadata_removed():
    exif=Image.Exif();exif[274]=6;exif[270]='private-metadata-description'
    result=images.normalize(picture('JPEG',exif=exif),'image/jpeg')
    assert (result.width,result.height)==(8,12)
    assert b'private-metadata-description' not in result.body
    with Image.open(io.BytesIO(result.body)) as normalized:assert len(normalized.getexif())==0


def test_gif_uses_only_first_frame():
    first=Image.new('RGB',(5,5),'red');second=Image.new('RGB',(5,5),'blue');out=io.BytesIO()
    first.save(out,format='GIF',save_all=True,append_images=[second]);first.close();second.close()
    result=images.normalize(out.getvalue(),'image/gif')
    with Image.open(io.BytesIO(result.body)) as normalized:
        assert normalized.getpixel((0,0))[:3]==(255,0,0)
        assert getattr(normalized,'n_frames',1)==1
    assert 'GIF' in result.warning


@pytest.mark.parametrize('body,mime',[(b'<svg></svg>','image/svg+xml'),(b'<script>bad</script>','image/png'),(picture(),'image/jpeg'),(b'','image/png')])
def test_invalid_or_spoofed_images_never_pass(body,mime):
    with pytest.raises(images.ImageAttachmentError):images.normalize(body,mime)


def test_upload_byte_limit_is_checked():
    with pytest.raises(images.ImageAttachmentError) as exc:images.normalize(b'0'*(images.MAX_UPLOAD_BYTES+1),'image/png')
    assert exc.value.status_code==413


def test_pixel_limit_is_checked_before_decode(monkeypatch):
    monkeypatch.setattr(images,'MAX_PIXELS',5)
    with pytest.raises(images.ImageAttachmentError) as exc:images.normalize(picture(),'image/png')
    assert exc.value.status_code==413


def test_normalized_output_limit_is_enforced_while_encoding(monkeypatch):
    monkeypatch.setattr(images,'MAX_IMAGE_BYTES',10)
    with pytest.raises(images.ImageAttachmentError) as exc:images.normalize(picture(),'image/png')
    assert exc.value.status_code==413


def test_upload_and_preview_and_cancel_use_no_model_and_no_public_path():
    factory=Mock(side_effect=AssertionError('upload must not initialize model'))
    web.app.dependency_overrides[web.get_agent]=factory
    client=TestClient(web.app);sid=create(client)
    result=upload(client,sid,filename='../../synthetic-owner-key.png')
    assert result.status_code==200
    data=result.json();assert data['session_id']==sid and 'synthetic-owner-key' not in data['filename']
    assert data['image_url'].startswith('data:image/png;base64,')
    details=client.get('/api/sessions/'+sid,headers=HEADERS).json()
    assert details['pending_images'][0]['id']==data['image_id']
    read=client.get('/api/images/'+data['image_id'],params={'session_id':sid},headers=HEADERS)
    assert read.json()['image_url']==data['image_url'] and 'no-store' in read.headers['cache-control']
    assert client.get('/static/'+data['image_id']+'.png').status_code==404
    assert client.delete('/api/pending_images/'+data['image_id'],params={'session_id':sid},headers=HEADERS).status_code==200
    assert client.get('/api/sessions/'+sid,headers=HEADERS).json()['pending_images']==[]
    factory.assert_not_called()


def test_unknown_session_does_not_report_success():
    client=TestClient(web.app)
    assert upload(client,'missing').status_code==404
    assert not images._MEMORY_BYTES


@pytest.mark.parametrize('route,method',[('/api/upload_image','post'),('/api/images/'+'a'*32+'?session_id=test','get'),('/api/pending_images/'+'a'*32+'?session_id=test','delete')])
def test_all_image_endpoints_require_owner_auth(route,method):
    assert getattr(TestClient(web.app),method)(route).status_code==401


def test_cross_task_image_read_is_rejected():
    client=TestClient(web.app);a=create(client);b=create(client);data=upload(client,a).json()
    assert client.get('/api/images/'+data['image_id'],params={'session_id':b},headers=HEADERS).status_code==404


def test_pending_image_count_is_bounded():
    client=TestClient(web.app);sid=create(client)
    for _ in range(4):assert upload(client,sid).status_code==200
    assert upload(client,sid).status_code==409


def test_header_limit_rejects_before_multipart_parser():
    assert TestClient(web.app).post('/api/upload_image',headers={**HEADERS,'Content-Length':str(images.FORM_BODY_LIMIT+1)}).status_code==413


def test_streamed_body_limit_rejects_without_content_length(monkeypatch):
    monkeypatch.setattr(images,'FORM_BODY_LIMIT',10)
    called=[];sent=[];messages=[{'type':'http.request','body':b'a'*6,'more_body':True},{'type':'http.request','body':b'b'*6,'more_body':False}]
    async def downstream(*args):called.append(True)
    async def receive():return messages.pop(0)
    async def send(message):sent.append(message)
    scope={'type':'http','method':'POST','path':'/api/upload_image','headers':[(b'authorization',b'Bearer synthetic-owner-key')]}
    asyncio.run(web.ImageUploadGuard(downstream)(scope,receive,send))
    assert not called and sent[0]['status']==413


def test_unauthenticated_body_is_not_read():
    sent=[]
    async def downstream(*args):raise AssertionError('not parsed')
    async def receive():raise AssertionError('not read')
    async def send(message):sent.append(message)
    scope={'type':'http','method':'POST','path':'/api/upload_image','headers':[]}
    asyncio.run(web.ImageUploadGuard(downstream)(scope,receive,send))
    assert sent[0]['status']==401


def sdk_client():
    captured=[]
    def generate(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text='Image understood',thought=False,function_call=None)]))])
    return SimpleNamespace(models=SimpleNamespace(generate_content=generate)),captured


def test_actual_agent_core_receives_inline_image_bytes_and_mime():
    client=TestClient(web.app);sid=create(client);image=upload(client,sid).json()
    fake,captured=sdk_client();agent=AgentCore(client=fake,max_retries=1)
    web.app.dependency_overrides[web.get_agent]=lambda:agent
    result=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Describe','image_ids':[image['image_id']]}).json()
    assert result['status']=='completed' and len(captured)==1
    parts=captured[0]['contents'][0].parts
    assert parts[1].inline_data.mime_type=='image/png'
    assert parts[1].inline_data.data==base64.b64decode(image['image_url'].split(',',1)[1])
    assert result['messages'][0]['images']==[image['image_id']] and result['pending_images']==[]


def test_detached_image_is_not_implicitly_sent():
    client=TestClient(web.app);sid=create(client);data=upload(client,sid).json()
    client.delete('/api/pending_images/'+data['image_id'],params={'session_id':sid},headers=HEADERS)
    fake,captured=sdk_client();web.app.dependency_overrides[web.get_agent]=lambda:AgentCore(client=fake,max_retries=1)
    client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Text only'})
    assert len(captured[0]['contents'][0].parts)==1


def test_failed_model_response_keeps_pending_image():
    client=TestClient(web.app);sid=create(client);data=upload(client,sid).json()
    web.app.dependency_overrides[web.get_agent]=lambda:Mock(run=lambda prompt,**kwargs:AgentResult('Failure',1,0,status='failed'))
    result=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Read','image_ids':[data['image_id']]}).json()
    assert result['status']=='failed' and result['pending_images'][0]['id']==data['image_id']


def test_explicit_empty_selection_keeps_unselected_pending():
    client=TestClient(web.app);sid=create(client);data=upload(client,sid).json()
    web.app.dependency_overrides[web.get_agent]=lambda:Mock(run=lambda prompt:AgentResult('Text reply',1,0))
    result=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Text','image_ids':[]}).json()
    assert result['pending_images'][0]['id']==data['image_id']


def test_s3_images_and_pending_restore_after_process_memory_reset(durable):
    client=TestClient(web.app);sid=create(client);data=upload(client,sid).json()
    for name in ['_SESSIONS','_SESSION_META','_SESSION_LOCKS','_RUNS']:getattr(web,name).clear()
    images._MEMORY_DOCUMENTS.clear();images._MEMORY_BYTES.clear()
    restored=client.get('/api/sessions/'+sid,headers=HEADERS).json()
    assert restored['pending_images'][0]['id']==data['image_id']
    preview=client.get('/api/images/'+data['image_id'],params={'session_id':sid},headers=HEADERS)
    assert preview.json()['image_url']==data['image_url']
    raw=[value['Body'] for key,value in durable.objects.items() if key.endswith('.json')]
    assert not any(b'data:image/' in body for body in raw)
    assert all(key.startswith('karry-tasks/') for key in durable.objects)
    assert all(stream.closed for stream in durable.streams)


def test_image_request_replay_does_not_regenerate_and_changed_ids_conflict(durable):
    client=TestClient(web.app);sid=create(client);one=upload(client,sid).json();two=upload(client,sid).json()
    fake,captured=sdk_client();web.app.dependency_overrides[web.get_agent]=lambda:AgentCore(client=fake,max_retries=1)
    body={'session_id':sid,'prompt':'Read','image_ids':[one['image_id']],'request_id':'same-request'}
    first=client.post('/api/chat',headers=HEADERS,json=body).json()
    factory=Mock(side_effect=AssertionError('replay must not initialize model'));web.app.dependency_overrides[web.get_agent]=factory
    replay=client.post('/api/chat',headers=HEADERS,json=body).json()
    assert replay['replayed'] and replay['run_id']==first['run_id'] and len(captured)==1
    assert replay['pending_images'][0]['id']==two['image_id']
    assert client.post('/api/chat',headers=HEADERS,json={**body,'image_ids':[two['image_id']]}).status_code==409
    factory.assert_not_called()


def test_s3_failed_model_keeps_image_and_allows_manual_retry(durable):
    client=TestClient(web.app);sid=create(client);data=upload(client,sid).json();calls=[]
    def run(prompt,**kwargs):calls.append(1);return AgentResult('Failure',1,0,status='failed')
    web.app.dependency_overrides[web.get_agent]=lambda:Mock(run=run)
    body={'session_id':sid,'prompt':'Read','image_ids':[data['image_id']],'request_id':'retry'}
    for _ in range(2):
        result=client.post('/api/chat',headers=HEADERS,json=body).json()
        assert result['pending_images'][0]['id']==data['image_id']
    assert len(calls)==2


def test_corrupt_s3_image_is_not_forwarded_to_model(durable):
    client=TestClient(web.app);sid=create(client);data=upload(client,sid).json()
    key=next(key for key in durable.objects if key.endswith('.bin'));durable.objects[key]['Metadata']['sha256']='wrong'
    fake,captured=sdk_client();web.app.dependency_overrides[web.get_agent]=lambda:AgentCore(client=fake,max_retries=1)
    response=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Read','image_ids':[data['image_id']]})
    assert response.status_code==503 and not captured
    assert client.get('/api/sessions/'+sid,headers=HEADERS).json()['pending_images']


def test_remote_lease_blocks_upload_and_detach(durable):
    client=TestClient(web.app);sid=create(client);data=upload(client,sid).json();tasks.repository().begin(sid,'another-instance')
    assert upload(client,sid).status_code==409
    assert client.delete('/api/pending_images/'+data['image_id'],params={'session_id':sid},headers=HEADERS).status_code==409


def test_image_url_analysis_remains_one_fetch_plus_one_generation(monkeypatch):
    fake,captured=sdk_client();agent=AgentCore(client=fake,max_steps=2,max_retries=1);calls=[]
    def fetch(name,args):
        calls.append((name,args));return {'status':'success','url':args['url'],'title':'Page','content_preview':'Fetched HTML'}
    monkeypatch.setattr(agent.registry,'execute',fetch)
    image=images.normalize(picture(),'image/png')
    result=agent.run('https://example.com/',images=[{'data':image.body,'mime_type':image.mime_type}])
    assert result.status=='completed' and result.steps==2 and result.tool_calls==1
    assert len(calls)==len(captured)==1
    assert captured[0]['contents'][0].parts[1].inline_data.data==image.body
    assert captured[0]['config'].tools==[]


def test_persistence_failure_keeps_pending_and_releases_lease(durable,monkeypatch):
    client=TestClient(web.app);sid=create(client);data=upload(client,sid).json()
    fake,captured=sdk_client();web.app.dependency_overrides[web.get_agent]=lambda:AgentCore(client=fake,max_retries=1)
    def fail(*args):raise tasks.TaskStorageError('save failed','storage_unavailable')
    monkeypatch.setattr(tasks.S3TaskRepository,'finish',fail)
    response=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Read','image_ids':[data['image_id']]})
    assert response.status_code==503 and len(captured)==1
    document=tasks.repository().load(sid)
    assert document['pending_images']==[data['image_id']] and document['lease'] is None


def test_wrong_task_selection_and_detach_are_rejected_without_model_call():
    client=TestClient(web.app);a=create(client);b=create(client);data=upload(client,a).json()
    fake,captured=sdk_client();web.app.dependency_overrides[web.get_agent]=lambda:AgentCore(client=fake,max_retries=1)
    assert client.post('/api/chat',headers=HEADERS,json={'session_id':b,'prompt':'Read','image_ids':[data['image_id']]}).status_code==404
    assert not captured
    assert client.delete('/api/pending_images/'+data['image_id'],params={'session_id':b},headers=HEADERS).status_code==404
    assert client.get('/api/sessions/'+a,headers=HEADERS).json()['pending_images']


def test_file_closed_when_session_is_unknown(monkeypatch):
    from starlette.datastructures import UploadFile
    closed=[];original=UploadFile.close
    async def close(self):
        closed.append(self.filename);await original(self)
    monkeypatch.setattr(UploadFile,'close',close)
    assert upload(TestClient(web.app),'missing').status_code==404
    assert closed


def test_invalid_pending_schema_is_rejected_as_storage_integrity(durable):
    r=tasks.repository();r.create('session','Title');document=r.load('session');document['pending_images']=[{}]
    with pytest.raises(tasks.TaskStorageError) as exc:r._validate(document,'session')
    assert exc.value.code=='storage_integrity'


def test_cancelled_ids_cannot_be_sent_from_a_stale_client():
    client=TestClient(web.app);sid=create(client);data=upload(client,sid).json()
    client.delete('/api/pending_images/'+data['image_id'],params={'session_id':sid},headers=HEADERS)
    fake,captured=sdk_client();web.app.dependency_overrides[web.get_agent]=lambda:AgentCore(client=fake,max_retries=1)
    response=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Read','image_ids':[data['image_id']]})
    assert response.status_code==409 and not captured


def test_consumed_image_mixed_with_new_image_is_rejected(durable):
    client=TestClient(web.app);sid=create(client);one=upload(client,sid).json()
    fake,captured=sdk_client();web.app.dependency_overrides[web.get_agent]=lambda:AgentCore(client=fake,max_retries=1)
    assert client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'First','image_ids':[one['image_id']],'request_id':'one'}).status_code==200
    two=upload(client,sid).json()
    response=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Second','image_ids':[one['image_id'],two['image_id']],'request_id':'two'})
    assert response.status_code==409 and len(captured)==1
    assert tasks.repository().load(sid)['pending_images']==[two['image_id']]


def test_image_reference_is_reserved_before_binary_put(durable,monkeypatch):
    client=TestClient(web.app);sid=create(client);original=durable.put_object;seen=[]
    def put(**kwargs):
        if kwargs['Key'].endswith('.bin'):
            image_id=kwargs['Key'].split('/')[-1].split('.')[0]
            assert image_id in tasks.repository().load(sid)['image_uploads'];seen.append(image_id)
        return original(**kwargs)
    monkeypatch.setattr(durable,'put_object',put)
    data=upload(client,sid).json();assert seen==[data['image_id']]
    assert tasks.repository().load(sid)['image_uploads']=={}


def test_lost_binary_put_ack_restores_same_reserved_image(durable,monkeypatch):
    client=TestClient(web.app);sid=create(client);original=durable.put_object
    def lost(**kwargs):
        result=original(**kwargs)
        if kwargs['Key'].endswith('.bin'):raise OSError('lost image acknowledgement')
        return result
    monkeypatch.setattr(durable,'put_object',lost)
    assert upload(client,sid).status_code==503
    document=tasks.repository().load(sid);assert len(document['image_uploads'])==1 and document.get('pending_images',[])==[]
    restored=client.get('/api/sessions/'+sid,headers=HEADERS).json()
    assert len(restored['pending_images'])==1 and restored['image_uploads_pending']==0
    assert len([key for key in durable.objects if key.endswith('.bin')])==1


def test_failed_binary_puts_keep_bounded_reachable_reservations(durable,monkeypatch):
    client=TestClient(web.app);sid=create(client);original=durable.put_object;count=[]
    def fail(**kwargs):
        if kwargs['Key'].endswith('.bin'):
            count.append(1);raise OSError('image storage unavailable')
        return original(**kwargs)
    monkeypatch.setattr(durable,'put_object',fail)
    for color in ['blue','red','green','black']:
        assert upload(client,sid,body=picture(color=color)).status_code==503
    assert upload(client,sid,body=picture(color='white')).status_code==409
    document=tasks.repository().load(sid)
    assert len(document['image_uploads'])==4 and len(count)==4
    assert not document.get('pending_images')
    details=client.get('/api/sessions/'+sid,headers=HEADERS).json()
    assert details['image_uploads_pending']==4 and details['pending_images']==[]


def test_retry_same_image_reuses_reservation_after_put_failure(durable,monkeypatch):
    client=TestClient(web.app);sid=create(client);original=durable.put_object
    def fail(**kwargs):
        if kwargs['Key'].endswith('.bin'):raise OSError('temporary image outage')
        return original(**kwargs)
    monkeypatch.setattr(durable,'put_object',fail)
    assert upload(client,sid).status_code==503
    reserved=next(iter(tasks.repository().load(sid)['image_uploads']))
    monkeypatch.setattr(durable,'put_object',original)
    response=upload(client,sid)
    assert response.status_code==200 and response.json()['image_id']==reserved
    assert tasks.repository().load(sid)['image_uploads']=={}
    assert len([key for key in durable.objects if key.endswith('.bin')])==1


def test_memory_reference_failure_removes_only_unreferenced_new_bytes(monkeypatch):
    client=TestClient(web.app);sid=create(client);service=images.AttachmentService(sid,None)
    def fail(callback):raise images.ImageAttachmentError('concurrent pending limit',409)
    monkeypatch.setattr(service,'_mutate',fail)
    with pytest.raises(images.ImageAttachmentError):service.upload(images.normalize(picture(),'image/png'),'photo.png')
    assert not images._MEMORY_BYTES and not images.memory_document(sid)['images']

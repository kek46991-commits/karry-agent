import base64
import copy
import hashlib
import io
import json
import threading
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import app as web
from karry_agent import task_persistence as tasks
from karry_agent import website_storage as websites
from karry_agent import instant_website as sites
from karry_agent.core import AgentResult, ExecutionEvent

HEADERS={'Authorization':'Bearer synthetic-owner-key'}


def error(code):
    return ClientError({'Error':{'Code':code,'Message':'provider-private-details'}},'Object')


class FakeS3:
    def __init__(self):
        self.objects={};self.lock=threading.RLock();self.puts=[];self.streams=[];self.fail=False;self.conflicts=0
    def get_object(self,**kw):
        if self.fail:raise error('AccessDenied')
        with self.lock:
            if kw['Key'] not in self.objects:raise error('NoSuchKey')
            record=copy.deepcopy(self.objects[kw['Key']]);body=io.BytesIO(record['Body']);self.streams.append(body)
            return {'Body':body,'ContentLength':len(record['Body']),'ContentType':record['ContentType'],'Metadata':record['Metadata'],'ETag':record['ETag']}
    def put_object(self,**kw):
        if self.fail:raise error('AccessDenied')
        with self.lock:
            if self.conflicts:self.conflicts-=1;raise error('PreconditionFailed')
            old=self.objects.get(kw['Key'])
            if kw.get('IfNoneMatch')=='*' and old is not None:raise error('PreconditionFailed')
            if kw.get('IfMatch') and (old is None or old['ETag']!=kw['IfMatch']):raise error('PreconditionFailed')
            assert kw['ContentMD5']==base64.b64encode(hashlib.md5(kw['Body']).digest()).decode()
            self.puts.append(copy.deepcopy(kw));tag='"'+hashlib.md5(kw['Body']).hexdigest()+'"'
            self.objects[kw['Key']]={**copy.deepcopy(kw),'ETag':tag}
            return {'ETag':tag}
    def delete_object(self,**kw):
        if self.fail:raise error('AccessDenied')
        with self.lock:
            self.objects.pop(kw['Key'],None)
            return {}


@pytest.fixture
def env(monkeypatch,tmp_path):
    monkeypatch.setattr(sites,'INSTANT_DIR',tmp_path/'instant')
    monkeypatch.setattr(sites,'_PENDING',{})
    monkeypatch.setenv('KARRY_TASK_STORAGE','s3')
    monkeypatch.setenv('KARRY_TASK_S3_PREFIX','karry-tasks/')
    monkeypatch.setenv('KARRY_API_TOKEN','synthetic-owner-key')
    monkeypatch.setenv('KARRY_WEBSITE_STORAGE','local')
    monkeypatch.setenv('RENDER_EXTERNAL_URL','https://example.onrender.com')
    for name in ['PARTNER_NAME','CORE_USER_NAME','CORE_PRIVATE_CONTEXT']:
        monkeypatch.delenv(name,raising=False)
    for name in ['_RUNS','_SESSIONS','_PRIVATE_SESSIONS','_PENDING_CALENDAR','_SESSION_META','_SESSION_LOCKS','_SESSION_APPROVALS']:
        monkeypatch.setattr(web,name,{})
    cfg=websites.StorageConfig('private-bucket','karry-websites/',None,'us-east-1','synthetic-access','synthetic-secret')
    client=FakeS3()
    monkeypatch.setattr(tasks,'repository',lambda:tasks.S3TaskRepository(cfg,client))
    old=dict(web.app.dependency_overrides)
    yield cfg,client
    web.app.dependency_overrides.clear();web.app.dependency_overrides.update(old)


def repo(env):return tasks.S3TaskRepository(env[0],env[1])
def run_summary(id='run-1'):return {'run_id':id,'status':'completed','steps':1,'tool_calls':0,'metrics':{},'trace':[{'private':'must not persist'}],'private_mode':True}


def test_task_json_create_list_load_and_dedicated_prefix(env):
    r=repo(env);r.create('task-1','Title')
    assert r.list()==[{'session_id':'task-1','title':'Title'}]
    assert r.load('task-1')['messages']==[]
    assert all(key.startswith('karry-tasks/') for key in env[1].objects)
    assert all(s.closed for s in env[1].streams)
    assert all('IfNoneMatch' in p or 'IfMatch' in p for p in env[1].puts)


def test_independent_repository_instance_restores_json(env):
    first=repo(env);first.create('task-1','Title');first.begin('task-1','lease-1')
    first.finish('task-1','lease-1',[{'role':'user','content':'Remember A'},{'role':'model','content':'Answer A'}],run_summary())
    restored=repo(env).load('task-1')
    assert restored['messages'][0]['content']=='Remember A' and restored['last_run']['run_id']=='run-1'
    assert restored['lease'] is None and restored['last_run']['trace']==[]
    assert 'private_mode' not in restored['last_run']


def test_active_lease_rejects_another_worker(env):
    r=repo(env);r.create('task-1','Title');r.begin('task-1','worker-a')
    with pytest.raises(tasks.TaskStorageError) as failure:repo(env).begin('task-1','worker-b')
    assert failure.value.code=='session_busy'


def test_release_and_expired_lease_can_be_claimed(env,monkeypatch):
    r=repo(env);r.create('task-1','Title');r.begin('task-1','worker-a');r.release('task-1','worker-a');r.begin('task-1','worker-b')
    monkeypatch.setattr(tasks.time,'time',lambda:10**12)
    assert repo(env).begin('task-1','worker-c')['lease']['id']=='worker-c'


def test_wrong_lease_cannot_commit_history(env):
    r=repo(env);r.create('task-1','Title');r.begin('task-1','worker-a')
    with pytest.raises(tasks.TaskStorageError):r.finish('task-1','worker-b',[],run_summary())
    assert r.load('task-1')['messages']==[]


def test_cas_retry_keeps_all_index_entries(env):
    r=repo(env);r.create('first','First');env[1].conflicts=1
    r.begin('first','worker')
    r.create('second','Second')
    assert [row['session_id'] for row in r.list()]==['first','second']


def test_latest_url_update_during_lease_survives_commit(env):
    r=repo(env);r.create('task-1','Title');r.begin('task-1','worker')
    repo(env).update_url('task-1','https://example.onrender.com/static/instant/demo.html')
    r.finish('task-1','worker',[],run_summary())
    assert r.load('task-1')['latest_url'].endswith('demo.html')


def test_credentials_not_saved_but_private_conversation_remains(env,monkeypatch):
    monkeypatch.setenv('SECRET_PASSPHRASE','synthetic-passphrase')
    r=repo(env);r.create('task-1','Private notes');r.begin('task-1','worker')
    r.finish('task-1','worker',[{'role':'user','content':'Bearer synthetic-owner-key; synthetic-passphrase; ordinary private note'}],run_summary())
    text=r.load('task-1')['messages'][0]['content']
    assert 'synthetic-owner-key' not in text and 'synthetic-passphrase' not in text
    assert 'ordinary private note' in text


def test_json_hash_corruption_and_stream_close(env):
    r=repo(env);r.create('task-1','Title');key=r._key('task-1')
    env[1].objects[key]['Metadata']['sha256']='wrong'
    with pytest.raises(tasks.TaskStorageError) as exc:r.load('task-1')
    assert exc.value.code=='storage_integrity' and env[1].streams[-1].closed


def test_size_is_checked_before_body_read(env,monkeypatch):
    r=repo(env);r.create('task-1','Title');key=r._key('task-1')
    original=env[1].get_object
    def large(**kw):
        obj=original(**kw);obj['ContentLength']=tasks.MAX_DOCUMENT_BYTES+1;return obj
    monkeypatch.setattr(env[1],'get_object',large)
    with pytest.raises(tasks.TaskStorageError):r.load('task-1')
    assert env[1].streams[-1].closed


def test_schema_rejects_mismatched_id_and_excess_history(env):
    r=repo(env);r.create('task-1','Title')
    value=r.load('task-1');value['session_id']='different';tag=r._read(r._key('task-1'))[1];r._write(r._key('task-1'),value,tag)
    with pytest.raises(tasks.TaskStorageError):r.load('task-1')


def test_missing_and_provider_error_are_distinguished(env):
    with pytest.raises(tasks.TaskStorageError) as failure:repo(env).load('missing')
    assert failure.value.code=='not_found'
    env[1].fail=True
    with pytest.raises(tasks.TaskStorageError) as failure:repo(env).list()
    assert failure.value.code=='storage_unavailable' and 'provider-private-details' not in str(failure.value)


def test_duplicate_create_never_overwrites_messages(env):
    r=repo(env);r.create('task-1','First');r.begin('task-1','worker');r.finish('task-1','worker',[{'role':'user','content':'Keep this'}],run_summary())
    with pytest.raises(tasks.TaskStorageError):repo(env).create('task-1','Replacement')
    assert r.load('task-1')['messages'][0]['content']=='Keep this'


def test_retry_finish_is_idempotent_without_duplicate_messages(env):
    r=repo(env);r.create('task-1','Title');r.begin('task-1','worker');messages=[{'role':'user','content':'One'}]
    first=r.finish('task-1','worker',messages,run_summary())
    second=r.finish('task-1','worker',messages,run_summary())
    assert first==second and len(second['messages'])==1


def test_lost_put_ack_is_verified_without_repeat_model(env,monkeypatch):
    r=repo(env);r.create('task-1','Title');r.begin('task-1','worker')
    original=env[1].put_object;calls=[]
    def lost(**kw):
        result=original(**kw);calls.append(1)
        raise OSError('lost acknowledgement')
    monkeypatch.setattr(env[1],'put_object',lost)
    assert r.finish('task-1','worker',[],run_summary())['last_run']['run_id']=='run-1'
    assert len(calls)==1


def test_overlapping_html_and_task_prefix_rejected(env,monkeypatch):
    monkeypatch.setenv('KARRY_TASK_S3_PREFIX','karry-websites/private/')
    with pytest.raises(tasks.TaskStorageError):repo(env)


def test_invalid_mode_never_silently_uses_memory(monkeypatch):
    monkeypatch.setenv('KARRY_TASK_STORAGE','typo')
    with pytest.raises(tasks.TaskStorageError):tasks.mode()


def test_default_is_s3_and_missing_config_is_503(monkeypatch):
    monkeypatch.delenv('KARRY_TASK_STORAGE',raising=False)
    for name in ['KARRY_WEBSITE_S3_BUCKET','KARRY_WEBSITE_S3_ACCESS_KEY_ID','AWS_ACCESS_KEY_ID','KARRY_WEBSITE_S3_SECRET_ACCESS_KEY','AWS_SECRET_ACCESS_KEY']:
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('KARRY_API_TOKEN','synthetic-owner-key')
    assert tasks.mode()=='s3'
    response=TestClient(web.app).get('/api/sessions',headers=HEADERS)
    assert response.status_code==503 and response.json()['error_code']=='storage_configuration'


def test_actual_api_restores_tasks_history_and_context_after_memory_reset(env):
    calls=[]
    class Agent:
        def run(self,prompt,**kw):calls.append(prompt);return AgentResult('Answer '+prompt,1,0)
    web.app.dependency_overrides[web.get_agent]=lambda:Agent()
    client=TestClient(web.app)
    sid=client.post('/api/sessions',headers=HEADERS,json={'title':'Long-lived task'}).json()['session_id']
    first=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Remember topic'}).json()
    assert first['persistence']=='durable' and len(calls)==1
    for name in ['_SESSIONS','_SESSION_META','_SESSION_LOCKS','_RUNS','_PRIVATE_SESSIONS','_PENDING_CALENDAR','_SESSION_APPROVALS']:
        getattr(web,name).clear()
    factory=Mock(side_effect=AssertionError('no model for restoration'))
    web.app.dependency_overrides[web.get_agent]=factory
    assert client.get('/api/sessions',headers=HEADERS).json()[0]['session_id']==sid
    details=client.get('/api/sessions/'+sid,headers=HEADERS).json()
    assert details['messages'][0]['content']=='Remember topic' and details['last_run']['run_id']==first['run_id']
    factory.assert_not_called()
    web.app.dependency_overrides[web.get_agent]=lambda:Agent()
    next_response=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Continue'}).json()
    assert 'user: Remember topic' in calls[-1] and len(next_response['messages'])==4 and len(calls)==2


def test_storage_write_failure_does_not_report_chat_success(env,monkeypatch):
    r=repo(env);r.create('task-1','Title');calls=[]
    class Agent:
        def run(self,prompt):calls.append(prompt);return AgentResult('Generated once',1,0)
    web.app.dependency_overrides[web.get_agent]=lambda:Agent()
    def fail(*args):raise tasks.TaskStorageError('save failed','storage_unavailable')
    monkeypatch.setattr(tasks.S3TaskRepository,'finish',fail)
    response=TestClient(web.app).post('/api/chat',headers=HEADERS,json={'session_id':'task-1','prompt':'Generate'})
    assert response.status_code==503 and len(calls)==1
    assert r.load('task-1')['messages']==[] and r.load('task-1')['lease'] is None


def test_real_api_returns_409_when_another_instance_holds_lease(env):
    r=repo(env);r.create('task-1','Title');r.begin('task-1','remote-worker')
    agent=Mock();web.app.dependency_overrides[web.get_agent]=lambda:agent
    response=TestClient(web.app).post('/api/chat',headers=HEADERS,json={'session_id':'task-1','prompt':'Hello'})
    assert response.status_code==409 and not agent.run.called


def test_task_json_is_never_exposed_by_public_static_routes(env):
    r=repo(env);r.create('task-1','Private title')
    client=TestClient(web.app)
    assert client.get('/api/sessions/task-1').status_code==401
    assert client.get('/static/instant/index.json').status_code==404
    assert client.get('/static/karry-tasks/index.json').status_code==404


def test_private_mode_authorization_is_not_restored(env,monkeypatch):
    monkeypatch.setenv('SECRET_PASSPHRASE','synthetic-passphrase')
    web.app.dependency_overrides[web.get_agent]=lambda:Mock(run=lambda prompt,**kw:AgentResult('Done',1,0))
    client=TestClient(web.app);sid=client.post('/api/sessions',headers=HEADERS,json={}).json()['session_id']
    first=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'synthetic-passphrase'}).json()
    assert first['private_mode']
    web._PRIVATE_SESSIONS.clear();web._SESSIONS.clear();web._SESSION_LOCKS.clear();web._SESSION_META.clear()
    second=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Normal'}).json()
    assert not second['private_mode'] and 'synthetic-passphrase' not in json.dumps(repo(env).load(sid))


@pytest.mark.parametrize('route',['/sessions/known','/runs/known'])
def test_legacy_private_routes_fail_closed_without_token(env,monkeypatch,route):
    monkeypatch.delenv('KARRY_API_TOKEN')
    response=TestClient(web.app).get(route)
    assert response.status_code==503


def test_access_ids_and_all_signing_credentials_are_removed(env,monkeypatch):
    credentials={'KARRY_WEBSITE_S3_ACCESS_KEY_ID':'synthetic-access-id','AWS_ACCESS_KEY_ID':'another-access-id',
        'KARRY_WEBSITE_S3_SECRET_ACCESS_KEY':'synthetic-secret-key','AWS_SESSION_TOKEN':'synthetic-session-token'}
    for key,value in credentials.items():monkeypatch.setenv(key,value)
    r=repo(env);r.create('task-1','Title');r.begin('task-1','worker')
    r.finish('task-1','worker',[{'role':'model','content':' '.join(credentials.values())}],run_summary())
    stored=json.dumps(r.load('task-1'))
    assert all(value not in stored for value in credentials.values())


def test_all_completed_history_is_retained_beyond_context_window(env):
    class Agent:
        def run(self,prompt):return AgentResult('Short answer',1,0)
    web.app.dependency_overrides[web.get_agent]=lambda:Agent()
    client=TestClient(web.app);sid=client.post('/api/sessions',headers=HEADERS,json={}).json()['session_id']
    for i in range(12):
        result=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Message '+str(i)})
        assert result.status_code==200
    assert len(result.json()['messages'])==24 and result.json()['messages'][0]['content']=='Message 0'
    assert len(web._SESSIONS[sid].history)==20


def test_post_put_verification_failure_keeps_successful_commit(env,monkeypatch):
    r=repo(env);r.create('task-1','Title');r.begin('task-1','worker')
    original=env[1].put_object;get=env[1].get_object;failed=[False]
    def put(**kw):
        result=original(**kw);failed[0]=True;return result
    def read(**kw):
        if failed[0]:raise OSError('verification temporarily unavailable')
        return get(**kw)
    monkeypatch.setattr(env[1],'put_object',put);monkeypatch.setattr(env[1],'get_object',read)
    assert r.finish('task-1','worker',[],run_summary())['last_run']['run_id']=='run-1'
    failed[0]=False
    assert r.load('task-1')['last_run']['run_id']=='run-1'


def test_latest_completed_request_id_replays_without_model_init(env):
    calls=[]
    class Agent:
        def run(self,prompt):calls.append(prompt);return AgentResult('Saved response',1,0)
    web.app.dependency_overrides[web.get_agent]=lambda:Agent()
    client=TestClient(web.app);sid=client.post('/api/sessions',headers=HEADERS,json={}).json()['session_id']
    body={'session_id':sid,'prompt':'Generate once','request_id':'request-1'}
    first=client.post('/api/chat',headers=HEADERS,json=body).json()
    factory=Mock(side_effect=AssertionError('cached reply must not initialize model'))
    web.app.dependency_overrides[web.get_agent]=factory
    replay=client.post('/api/chat',headers=HEADERS,json=body).json()
    assert replay['replayed'] and replay['run_id']==first['run_id'] and len(calls)==1
    assert len(replay['messages'])==2
    streamed=client.post('/api/chat',headers=HEADERS,json={**body,'stream':True})
    assert 'event: completed' in streamed.text and '"replayed": true' in streamed.text
    factory.assert_not_called()


def test_same_request_id_with_changed_prompt_is_conflict(env):
    web.app.dependency_overrides[web.get_agent]=lambda:Mock(run=lambda prompt:AgentResult('Done',1,0))
    client=TestClient(web.app);sid=client.post('/api/sessions',headers=HEADERS,json={}).json()['session_id']
    body={'session_id':sid,'prompt':'Original','request_id':'request-1'}
    assert client.post('/api/chat',headers=HEADERS,json=body).status_code==200
    assert client.post('/api/chat',headers=HEADERS,json={**body,'prompt':'Changed'}).status_code==409


def test_direct_publication_outbox_recovers_url_after_ack_failure(env,monkeypatch):
    client=TestClient(web.app);sid=client.post('/api/sessions',headers=HEADERS,json={}).json()['session_id']
    original=tasks.S3TaskRepository.acknowledge_publication
    def fail(*args):raise tasks.TaskStorageError('record write unavailable','storage_unavailable')
    monkeypatch.setattr(tasks.S3TaskRepository,'acknowledge_publication',fail)
    result=client.post('/api/agent/tools/execute',headers=HEADERS,json={'tool_name':'deploy_instant_website','session_id':sid,'arguments':{'html_content':'<h1>Published once</h1>'}}).json()['result']
    assert result['success'] and result['history_persistence']=='failed'
    assert len(repo(env).load(sid)['publication_outbox'])==1
    monkeypatch.setattr(tasks.S3TaskRepository,'acknowledge_publication',original)
    recovered=client.get('/api/sessions/'+sid,headers=HEADERS).json()
    assert recovered['latest_url']==result['public_url'] and repo(env).load(sid)['publication_outbox']==[]
    assert len(list(sites.INSTANT_DIR.glob('*.html')))==1


def test_confirmed_publication_outbox_recovers_originating_task(env,monkeypatch):
    class Agent:
        def run(self,prompt):
            pending=sites.prepare_instant_website('<h1>Approved once</h1>')
            return AgentResult('Please confirm',1,1,trace=[ExecutionEvent(1,'deploy_instant_website',{},pending,0)],status='needs_user_action')
    web.app.dependency_overrides[web.get_agent]=lambda:Agent()
    client=TestClient(web.app);sid=client.post('/api/sessions',headers=HEADERS,json={}).json()['session_id']
    prepared=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Prepare'}).json()['trace'][0]['observation']
    original=tasks.S3TaskRepository.acknowledge_publication
    monkeypatch.setattr(tasks.S3TaskRepository,'acknowledge_publication',Mock(side_effect=tasks.TaskStorageError('ack failed','storage_unavailable')))
    result=client.post('/api/agent/websites/confirm',headers=HEADERS,json={'approval_id':prepared['approval_id'],'sha256':prepared['preview']['sha256'],'confirmed':True}).json()
    assert result['success'] and result['history_persistence']=='failed'
    assert prepared['approval_id'] not in sites._PENDING
    monkeypatch.setattr(tasks.S3TaskRepository,'acknowledge_publication',original)
    assert client.get('/api/sessions/'+sid,headers=HEADERS).json()['latest_url']==result['public_url']


def test_outbox_never_links_nonexistent_or_wrong_hash_content(env):
    r=repo(env);r.create('task-1','Title');plan=sites._plan('<h1>Never published</h1>',None)
    r.stage_publication('task-1',plan)
    assert r.reconcile('task-1',web._verify_publication)['latest_url']==''
    sites.INSTANT_DIR.mkdir();(sites.INSTANT_DIR/plan['filename']).write_text('Different content')
    assert r.reconcile('task-1',web._verify_publication)['latest_url']==''


def test_failed_outbox_stage_prevents_publication(env,monkeypatch):
    client=TestClient(web.app);sid=client.post('/api/sessions',headers=HEADERS,json={}).json()['session_id']
    monkeypatch.setattr(tasks.S3TaskRepository,'stage_publication',Mock(side_effect=tasks.TaskStorageError('stage failed','storage_unavailable')))
    response=client.post('/api/agent/tools/execute',headers=HEADERS,json={'tool_name':'deploy_instant_website','session_id':sid,'arguments':{'html_content':'<h1>Not published</h1>'}})
    assert response.status_code==503 and not sites.INSTANT_DIR.exists()


def test_api_commit_with_post_put_get_outage_replays_one_model_result(env,monkeypatch):
    calls=[]
    class Agent:
        def run(self,prompt):calls.append(prompt);return AgentResult('One result',1,0)
    web.app.dependency_overrides[web.get_agent]=lambda:Agent()
    client=TestClient(web.app);sid=client.post('/api/sessions',headers=HEADERS,json={}).json()['session_id']
    original_put=env[1].put_object;original_get=env[1].get_object;outage=[0]
    def put(**kw):
        result=original_put(**kw)
        data=json.loads(kw['Body'])
        if data.get('last_run',{}):outage[0]=2
        return result
    def get(**kw):
        if outage[0]:outage[0]-=1;raise OSError('verification GET failed')
        return original_get(**kw)
    monkeypatch.setattr(env[1],'put_object',put);monkeypatch.setattr(env[1],'get_object',get)
    body={'session_id':sid,'prompt':'Once','request_id':'one-request'}
    first=client.post('/api/chat',headers=HEADERS,json=body)
    assert first.status_code==200
    replay=client.post('/api/chat',headers=HEADERS,json=body)
    assert replay.status_code==200 and replay.json()['replayed'] and len(calls)==1


def test_real_sdk_accepts_conditional_json_put_with_checksum(env):
    import boto3
    from botocore.stub import Stubber
    client=boto3.client('s3',region_name='us-east-1',aws_access_key_id='synthetic-access',aws_secret_access_key='synthetic-secret')
    r=tasks.S3TaskRepository(env[0],client);value={'version':1};body=json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    expected={'Bucket':env[0].bucket,'Key':r._key('task-1'),'Body':body,'ContentType':'application/json; charset=utf-8','CacheControl':'no-store',
        'Metadata':{'sha256':hashlib.sha256(body).hexdigest()},'ContentMD5':base64.b64encode(hashlib.md5(body).digest()).decode(),'IfMatch':'"old-tag"'}
    with Stubber(client) as stub:
        stub.add_response('put_object',{'ETag':'"new-tag"'},expected)
        r._write(r._key('task-1'),value,'"old-tag"')
        stub.assert_no_pending_responses()


def test_clear_history_persists_and_keeps_task_url_and_images(env):
    calls=[]
    class Agent:
        def run(self,prompt,**kw):calls.append(prompt);return AgentResult('Answer '+prompt,1,0)
    web.app.dependency_overrides[web.get_agent]=lambda:Agent()
    client=TestClient(web.app)
    sid=client.post('/api/sessions',headers=HEADERS,json={'title':'Kept task'}).json()['session_id']
    client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Remember topic'})
    repo(env).update_url(sid,'https://example.onrender.com/static/instant/demo.html')
    cleared=client.delete('/api/sessions/'+sid+'/messages',headers=HEADERS)
    assert cleared.status_code==200 and cleared.json()['messages']==[] and cleared.json()['persistence']=='durable'
    stored=repo(env).load(sid)
    assert stored['messages']==[] and stored['last_run'] is None and stored['title']=='Kept task'
    assert stored['latest_url'].endswith('demo.html') and client.get('/api/sessions',headers=HEADERS).json()==[{'session_id':sid,'title':'Kept task'}]
    for name in ['_SESSIONS','_SESSION_META','_SESSION_LOCKS','_RUNS']:
        getattr(web,name).clear()
    client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Fresh'})
    assert 'Remember topic' not in calls[-1] and len(repo(env).load(sid)['messages'])==2


def test_clear_history_refuses_active_lease_from_another_instance(env):
    r=repo(env);r.create('task-1','Title');r.begin('task-1','worker-a')
    response=TestClient(web.app).delete('/api/sessions/task-1/messages',headers=HEADERS)
    assert response.status_code==409 and response.json()['error_code']=='session_busy'
    assert r.load('task-1')['lease']['id']=='worker-a'


def test_delete_task_removes_document_index_and_restored_context(env):
    web.app.dependency_overrides[web.get_agent]=lambda:SimpleAgent()
    client=TestClient(web.app)
    sid=client.post('/api/sessions',headers=HEADERS,json={'title':'Gone'}).json()['session_id']
    other=client.post('/api/sessions',headers=HEADERS,json={'title':'Kept'}).json()['session_id']
    run_id=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'Secret topic'}).json()['run_id']
    response=client.delete('/api/sessions/'+sid,headers=HEADERS)
    assert response.status_code==200 and response.json()=={'session_id':sid,'deleted':True}
    assert repo(env)._key(sid) not in env[1].objects
    assert sid not in web._SESSIONS and run_id not in web._RUNS
    for name in ['_SESSIONS','_SESSION_META','_SESSION_LOCKS','_RUNS']:
        getattr(web,name).clear()
    assert client.get('/api/sessions',headers=HEADERS).json()==[{'session_id':other,'title':'Kept'}]
    assert client.get('/api/sessions/'+sid,headers=HEADERS).status_code==404
    assert client.delete('/api/sessions/'+sid,headers=HEADERS).status_code==404


def test_delete_task_refuses_active_lease_from_another_instance(env):
    r=repo(env);r.create('task-1','Title');r.begin('task-1','worker-a')
    response=TestClient(web.app).delete('/api/sessions/task-1',headers=HEADERS)
    assert response.status_code==409 and response.json()['error_code']=='session_busy'
    assert r.load('task-1')['lease']['id']=='worker-a' and r.list()==[{'session_id':'task-1','title':'Title'}]


def test_delete_task_failure_is_reported_and_retry_finishes(env,monkeypatch):
    r=repo(env);r.create('task-1','Title')
    original=env[1].delete_object
    monkeypatch.setattr(env[1],'delete_object',lambda **kw:(_ for _ in ()).throw(error('AccessDenied')))
    client=TestClient(web.app)
    assert client.delete('/api/sessions/task-1',headers=HEADERS).status_code==503
    assert r.list()==[{'session_id':'task-1','title':'Title'}]
    monkeypatch.setattr(env[1],'delete_object',original)
    assert client.delete('/api/sessions/task-1',headers=HEADERS).status_code==200
    assert r.list()==[] and r._key('task-1') not in env[1].objects
    # An index row left without its document is still removable.
    r.create('task-2','Orphan');env[1].objects.pop(r._key('task-2'))
    assert client.delete('/api/sessions/task-2',headers=HEADERS).status_code==200 and r.list()==[]


class SimpleAgent:
    def run(self,prompt,**kw):return AgentResult('Answer '+prompt,1,0)

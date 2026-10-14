import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import app as web
from karry_agent import instant_website as sites
from karry_agent.core import AgentResult, AgentMetrics, ExecutionEvent

HEADERS={'Authorization':'Bearer synthetic-owner'}
HTML='<h1>Task preview</h1>'


class EchoAgent:
    def __init__(self): self.prompts=[]
    def run(self,prompt,**kw):
        self.prompts.append(prompt)
        return AgentResult(text='Answer: '+prompt,steps=1,tool_calls=0,metrics=AgentMetrics())


@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    for name in ['_RUNS','_SESSIONS','_PRIVATE_SESSIONS','_PENDING_CALENDAR','_SESSION_META','_SESSION_LOCKS','_SESSION_APPROVALS']:
        monkeypatch.setattr(web,name,{})
    monkeypatch.setattr(sites,'_PENDING',{})
    monkeypatch.setattr(sites,'INSTANT_DIR',tmp_path/'instant')
    monkeypatch.setenv('KARRY_API_TOKEN','synthetic-owner')
    monkeypatch.setenv('KARRY_WEBSITE_STORAGE','local')
    monkeypatch.setenv('RENDER_EXTERNAL_URL','https://example.onrender.com')
    for key in ['PARTNER_NAME','CORE_USER_NAME','CORE_PRIVATE_CONTEXT']:
        monkeypatch.delenv(key,raising=False)
    old=dict(web.app.dependency_overrides)
    yield
    web.app.dependency_overrides.clear();web.app.dependency_overrides.update(old)


def create(client,title='Task'):
    response=client.post('/api/sessions',json={'title':title},headers=HEADERS)
    assert response.status_code==200
    return response.json()['session_id']


def send(client,sid,prompt,**extra):
    return client.post('/api/chat',json={'session_id':sid,'prompt':prompt,**extra},headers=HEADERS)


def test_create_list_and_detail_never_initialize_llm():
    factory=Mock(side_effect=AssertionError('must not initialize LLM'))
    web.app.dependency_overrides[web.get_agent]=factory
    client=TestClient(web.app);sid=create(client,'First task')
    assert len(sid)==32
    assert client.get('/api/sessions',headers=HEADERS).json()==[{'session_id':sid,'title':'First task'}]
    detail=client.get('/api/sessions/'+sid,headers=HEADERS)
    assert detail.json()['messages']==[] and detail.json()['latest_url']=='' and detail.json()['last_run'] is None
    assert detail.headers['cache-control']=='no-store'
    factory.assert_not_called()


@pytest.mark.parametrize('route,method,payload',[
    ('/api/sessions','get',None),('/api/sessions','post',{'title':'Task'}),
    ('/api/sessions/missing','get',None),('/api/chat','post',{'session_id':'missing','prompt':'Hello'})])
def test_session_api_authentication(route,method,payload):
    client=TestClient(web.app)
    response=getattr(client,method)(route,**({'json':payload} if payload else {}))
    assert response.status_code==401


def test_new_api_fail_closed_without_owner_token(monkeypatch):
    monkeypatch.delenv('KARRY_API_TOKEN')
    assert TestClient(web.app).post('/api/sessions',json={}).status_code==503
    assert not web._SESSIONS


@pytest.mark.parametrize('title',['','   ','a'*121,'bad\nname'])
def test_invalid_titles_rejected(title):
    assert TestClient(web.app).post('/api/sessions',headers=HEADERS,json={'title':title}).status_code==422
    assert not web._SESSIONS


def test_default_and_trimmed_titles():
    client=TestClient(web.app)
    assert client.post('/api/sessions',headers=HEADERS,json={}).json()['title']=='新しいタスク'
    assert client.post('/api/sessions',headers=HEADERS,json={'title':'  Named task  '}).json()['title']=='Named task'


def test_real_agent_path_uses_shared_memory_and_separates_tasks():
    agent=EchoAgent();web.app.dependency_overrides[web.get_agent]=lambda:agent
    client=TestClient(web.app);a=create(client,'A');b=create(client,'B')
    first=send(client,a,'Remember topic A').json()
    other=send(client,b,'Topic B only').json()
    again=send(client,a,'Continue A').json()
    assert first['response']==first['text'] and first['messages'][1]['role']=='assistant'
    assert 'Remember topic A' not in other['response']
    assert 'user: Remember topic A' in again['response'] and 'Topic B only' not in again['response']
    assert len(client.get('/api/sessions/'+a,headers=HEADERS).json()['messages'])==4
    assert len(client.get('/api/sessions/'+b,headers=HEADERS).json()['messages'])==2
    assert len(agent.prompts)==3
    assert client.get('/sessions/'+a,headers=HEADERS).json()['history'][-1]['role']=='model'


def test_missing_task_is_404_without_model_or_automatic_creation():
    factory=Mock(side_effect=AssertionError('no model'))
    web.app.dependency_overrides[web.get_agent]=factory
    response=send(TestClient(web.app),'missing','Hello')
    assert response.status_code==404 and not web._SESSIONS
    factory.assert_not_called()


@pytest.mark.parametrize('prompt',['','   ','x'*20001])
def test_message_validation(prompt):
    client=TestClient(web.app);sid=create(client)
    assert send(client,sid,prompt).status_code==422


def test_json_reply_contains_metrics_run_and_full_messages():
    web.app.dependency_overrides[web.get_agent]=lambda:EchoAgent()
    client=TestClient(web.app);sid=create(client)
    result=send(client,sid,'Hello').json()
    assert result['status']=='completed' and result['steps']==1 and result['tool_calls']==0
    assert result['session_id']==sid and result['messages'][0]['content']=='Hello'
    assert client.get('/runs/'+result['run_id'],headers=HEADERS).status_code==200
    assert client.get('/api/sessions/'+sid,headers=HEADERS).json()['last_run']['run_id']==result['run_id']


def test_sse_completed_has_task_messages_and_no_extra_model_calls():
    agent=EchoAgent();web.app.dependency_overrides[web.get_agent]=lambda:agent
    client=TestClient(web.app);sid=create(client)
    response=send(client,sid,'Streaming task',stream=True)
    assert response.status_code==200 and 'text/event-stream' in response.headers['content-type']
    events=[]
    for chunk in response.text.split('\n\n'):
        if chunk.startswith('event: completed'):
            events.append(json.loads(chunk.split('data: ',1)[1]))
    assert len(events)==1 and events[0]['messages'][0]['content']=='Streaming task'
    assert events[0]['session_id']==sid and len(agent.prompts)==1


def test_task_capacity_does_not_delete_existing_tasks(monkeypatch):
    monkeypatch.setattr(web,'_MAX_SESSIONS',1)
    client=TestClient(web.app);sid=create(client)
    assert client.post('/api/sessions',headers=HEADERS,json={}).status_code==409
    assert sid in web._SESSIONS


def test_uuid_collision_does_not_reset_existing_task(monkeypatch):
    client=TestClient(web.app);sid=create(client)
    monkeypatch.setattr(web.uuid,'uuid4',lambda:SimpleNamespace(hex=sid))
    assert client.post('/api/sessions',headers=HEADERS,json={}).status_code==409
    assert web._SESSION_META[sid]['title']=='Task'


def test_history_stays_bounded_to_ten_turns():
    web.app.dependency_overrides[web.get_agent]=lambda:EchoAgent()
    client=TestClient(web.app);sid=create(client)
    for i in range(12): assert send(client,sid,'Message '+str(i)).status_code==200
    messages=client.get('/api/sessions/'+sid,headers=HEADERS).json()['messages']
    assert len(messages)==20 and messages[0]['content']=='Message 2'


def test_same_task_busy_returns_409_without_second_model_call():
    entered=threading.Event();release=threading.Event();calls=[]
    class Blocking:
        def run(self,prompt):
            calls.append(prompt);entered.set();assert release.wait(3)
            return AgentResult('Done',1,0)
    web.app.dependency_overrides[web.get_agent]=lambda:Blocking()
    client=TestClient(web.app);sid=create(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(send,client,sid,'First')
        try:
            assert entered.wait(2)
            second=send(client,sid,'Second')
            assert second.status_code==409 and len(calls)==1
        finally: release.set()
        assert first.result(timeout=3).status_code==200


def test_different_tasks_can_run_independently():
    entered=threading.Event();release=threading.Event()
    class Blocking:
        def run(self,prompt):
            if prompt=='A': entered.set();assert release.wait(3)
            return AgentResult('Done '+prompt,1,0)
    web.app.dependency_overrides[web.get_agent]=lambda:Blocking()
    client=TestClient(web.app);a=create(client,'A');b=create(client,'B')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(send,client,a,'A')
        try:
            assert entered.wait(2)
            assert send(client,b,'B').status_code==200
        finally: release.set()
        assert first.result(timeout=3).status_code==200


def test_execution_error_releases_lock_and_does_not_add_dummy_history():
    factory=Mock();factory.side_effect=[SimpleNamespace(run=Mock(side_effect=RuntimeError('synthetic failure'))),EchoAgent()]
    web.app.dependency_overrides[web.get_agent]=factory
    client=TestClient(web.app);sid=create(client)
    assert send(client,sid,'Fail').status_code==500
    assert web._SESSIONS[sid].get_history()==[]
    assert send(client,sid,'Retry').status_code==200


def test_private_session_state_does_not_cross_tasks(monkeypatch):
    monkeypatch.setenv('SECRET_PASSPHRASE','synthetic-passphrase')
    web.app.dependency_overrides[web.get_agent]=lambda:EchoAgent()
    client=TestClient(web.app);a=create(client,'A');b=create(client,'B')
    assert send(client,a,'synthetic-passphrase').json()['private_mode']
    assert not send(client,b,'Normal').json()['private_mode']


def test_legacy_session_eviction_cleans_related_state(monkeypatch):
    monkeypatch.setattr(web,'_MAX_SESSIONS',1)
    client=TestClient(web.app);sid=create(client)
    web._PRIVATE_SESSIONS[sid]=SimpleNamespace()
    web._PENDING_CALENDAR[sid]={'summary':'Old'}
    web._SESSION_APPROVALS['old']=(sid,web.time.monotonic()+10)
    web._session('legacy-new')
    for store in [web._SESSIONS,web._SESSION_META,web._SESSION_LOCKS,web._PRIVATE_SESSIONS,web._PENDING_CALENDAR]:assert sid not in store
    assert 'old' not in web._SESSION_APPROVALS


def test_active_session_is_not_evicted(monkeypatch):
    monkeypatch.setattr(web,'_MAX_SESSIONS',1)
    client=TestClient(web.app);sid=create(client);lock=web._SESSION_LOCKS[sid];lock.acquire()
    try:
        with pytest.raises(web.HTTPException) as error:web._session('new')
        assert error.value.status_code==409 and sid in web._SESSIONS
    finally:lock.release()


def test_only_actual_publication_updates_task_latest_url():
    client=TestClient(web.app);sid=create(client)
    payload={'tool_name':'deploy_instant_website','session_id':sid,'arguments':{'html_content':HTML}}
    result=client.post('/api/agent/tools/execute',headers=HEADERS,json=payload).json()['result']
    assert result['success']
    assert client.get('/api/sessions/'+sid,headers=HEADERS).json()['latest_url']==result['public_url']
    assert client.get('/static/instant/'+result['filename']).status_code==200


def test_unknown_task_does_not_publish_direct_owner_html():
    client=TestClient(web.app)
    response=client.post('/api/agent/tools/execute',headers=HEADERS,json={'tool_name':'deploy_instant_website','session_id':'missing','arguments':{'html_content':HTML}})
    assert response.status_code==404 and not sites.INSTANT_DIR.exists()


def test_approval_maps_to_originating_task_not_last_selected_task():
    class Publisher:
        def run(self,prompt):
            pending=sites.prepare_instant_website(HTML)
            return AgentResult('Please confirm',1,1,trace=[ExecutionEvent(1,'deploy_instant_website',{'html_content':HTML},pending,0)],status='needs_user_action')
    web.app.dependency_overrides[web.get_agent]=lambda:Publisher()
    client=TestClient(web.app);a=create(client,'A');b=create(client,'B')
    result=send(client,a,'Prepare preview').json();pending=result['trace'][0]['observation']
    assert client.get('/api/sessions/'+a,headers=HEADERS).json()['latest_url']==''
    # Switching only reads task B, then confirms the A approval.
    client.get('/api/sessions/'+b,headers=HEADERS)
    published=client.post('/api/agent/websites/confirm',headers=HEADERS,json={'approval_id':pending['approval_id'],'sha256':pending['preview']['sha256'],'confirmed':True}).json()
    assert published['success']
    assert client.get('/api/sessions/'+a,headers=HEADERS).json()['latest_url']==published['public_url']
    assert client.get('/api/sessions/'+b,headers=HEADERS).json()['latest_url']==''


def test_model_text_url_is_not_treated_as_real_publication():
    web.app.dependency_overrides[web.get_agent]=lambda:SimpleNamespace(run=lambda prompt:AgentResult('https://example.onrender.com/static/instant/not-real.html',1,0))
    client=TestClient(web.app);sid=create(client)
    assert send(client,sid,'Invented URL').json()['latest_url']==''

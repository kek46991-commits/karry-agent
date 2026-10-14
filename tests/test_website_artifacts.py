import copy
import hashlib
import pytest
from fastapi.testclient import TestClient
import app as web
from karry_agent import instant_website as sites, website_artifacts as drafts, task_persistence as tasks
from karry_agent.core import AgentResult, ExecutionEvent
from test_task_persistence import env, repo, HEADERS

HTML='<!doctype html><html><head><title>Homepage</title></head><body><h1>Created homepage</h1></body></html>'

class Agent:
    def __init__(self):self.calls=0
    def run(self,prompt):
        self.calls+=1
        prepared=sites.prepare_instant_website(HTML)
        return AgentResult('HTML作成済み。公開は確認後です。',1,1,[ExecutionEvent(1,'deploy_instant_website',{'html_content':HTML},prepared,0)],status='needs_user_action')

@pytest.fixture
def prepared(env):
    agent=Agent();web.app.dependency_overrides[web.get_agent]=lambda:agent
    client=TestClient(web.app)
    sid=client.post('/api/sessions',headers=HEADERS,json={}).json()['session_id']
    response=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'ホームページを作成して','request_id':'request-once'})
    assert response.status_code==200,response.text
    result=response.json();meta=result['artifacts'][0]
    return client,sid,result,meta,agent


def test_generated_html_and_id_are_saved_before_result(env,prepared):
    client,sid,result,meta,agent=prepared
    row=repo(env).load(sid)['website_artifacts'][meta['artifact_id']]
    assert row['plan']['html_content']==HTML and row['plan']['sha256']==hashlib.sha256(HTML.encode()).hexdigest()
    assert meta['state']=='draft' and meta['public_url'] is None and result['latest_url']==''
    assert result['trace'][0]['observation']['preview']['public_url'] is None
    assert not sites.INSTANT_DIR.exists()
    assert client.get(meta['source_url'],headers=HEADERS).json()['html_content']==HTML
    assert agent.calls==1


def test_reload_and_restart_preserve_private_preview(env,prepared):
    client,sid,result,meta,agent=prepared
    for name in ('_RUNS','_SESSIONS','_SESSION_META','_SESSION_APPROVALS','_SESSION_LOCKS'):
        getattr(web,name).clear()
    sites._PENDING.clear()
    detail=client.get('/api/sessions/'+sid,headers=HEADERS)
    assert detail.status_code==200 and detail.json()['artifacts'][0]==meta
    page=client.get(meta['preview_url'],headers=HEADERS)
    assert page.status_code==200 and page.text==HTML
    assert 'sandbox allow-scripts' in page.headers['content-security-policy']
    assert page.headers['cache-control']=='no-store'
    assert agent.calls==1


def test_drafts_are_owner_only_and_session_bound(env,prepared):
    client,sid,result,meta,agent=prepared
    assert client.get(meta['source_url']).status_code==401
    assert client.get(meta['preview_url']).status_code==401
    other=client.post('/api/sessions',headers=HEADERS,json={'title':'Other'}).json()['session_id']
    missing=client.get('/api/website-artifacts/'+meta['artifact_id']+'?session_id='+other,headers=HEADERS)
    assert missing.status_code==404 and HTML not in missing.text
    assert client.get('/api/website-artifacts/'+'f'*32+'?session_id='+sid,headers=HEADERS).status_code==404


def test_saved_draft_can_be_confirmed_after_memory_expiration(env,prepared):
    client,sid,result,meta,agent=prepared
    sites._PENDING.clear();web._SESSION_APPROVALS.clear()
    fresh=client.post('/api/website-artifacts/'+meta['artifact_id']+'/prepare?session_id='+sid,headers=HEADERS).json()
    assert fresh['confirmation_required'] and fresh['preview']['filename']==meta['filename']
    assert fresh['preview']['public_url'] is None
    published=client.post('/api/agent/websites/confirm',headers=HEADERS,json={'approval_id':fresh['approval_id'],'sha256':meta['sha256'],'confirmed':True}).json()
    assert published['success'] and published['status']=='published'
    assert client.get('/static/instant/'+meta['filename']).text==HTML
    after=client.get('/api/sessions/'+sid,headers=HEADERS).json()
    assert after['latest_url']==published['public_url'] and after['artifacts'][0]['state']=='published'
    repeat=client.post('/api/website-artifacts/'+meta['artifact_id']+'/prepare?session_id='+sid,headers=HEADERS).json()
    assert repeat['success'] and repeat['status']=='published' and agent.calls==1
    assert len(list(sites.INSTANT_DIR.glob('*.html')))==1


def test_changed_public_origin_is_rejected_without_losing_draft(env,prepared,monkeypatch):
    client,sid,result,meta,agent=prepared
    monkeypatch.setenv('RENDER_EXTERNAL_URL','https://different.onrender.com')
    response=client.post('/api/website-artifacts/'+meta['artifact_id']+'/prepare?session_id='+sid,headers=HEADERS).json()
    assert not response['success']
    assert client.get(meta['source_url'],headers=HEADERS).json()['html_content']==HTML
    assert not sites.INSTANT_DIR.exists()


def test_retry_after_restart_reuses_saved_html_without_model(env,prepared):
    client,sid,result,meta,agent=prepared
    web._RUNS.clear();sites._PENDING.clear();web._SESSION_APPROVALS.clear()
    replay=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'ホームページを作成して','request_id':'request-once'})
    assert replay.status_code==200 and replay.json()['replayed']
    assert replay.json()['artifacts'][0]==meta and agent.calls==1


def test_artifact_hash_validation_cannot_serve_corruption(env,prepared):
    client,sid,result,meta,agent=prepared
    repository=repo(env);doc=repository.load(sid)
    doc['website_artifacts'][meta['artifact_id']]['plan']['html_content']='<p>Changed</p>'
    _,tag=repository._read(repository._key(sid));repository._write(repository._key(sid),doc,tag)
    response=client.get(meta['source_url'],headers=HEADERS)
    assert response.status_code==503 and 'Changed' not in response.text
    assert client.get('/health').status_code==200


def test_draft_capacity_failure_is_not_a_saved_result(env,prepared,monkeypatch):
    client,sid,result,meta,agent=prepared
    monkeypatch.setattr(drafts,'MAX_ARTIFACTS',1)
    response=client.post('/api/chat',headers=HEADERS,json={'session_id':sid,'prompt':'もう一つ作成','request_id':'different'})
    assert response.status_code==503
    assert len(repo(env).load(sid)['website_artifacts'])==1
    assert not sites.INSTANT_DIR.exists()


def test_false_confirmation_keeps_private_preview(env,prepared):
    client,sid,result,meta,agent=prepared
    approval=result['trace'][0]['observation']['approval_id']
    cancelled=client.post('/api/agent/websites/confirm',headers=HEADERS,json={'approval_id':approval,'sha256':meta['sha256'],'confirmed':False}).json()
    assert cancelled['status']=='cancelled'
    assert client.get(meta['source_url'],headers=HEADERS).json()['state']=='draft'
    assert not sites.INSTANT_DIR.exists()

@pytest.mark.parametrize('prompt,expected',[
 ('ホームページを作成して。会社紹介と説明文を入れてください。',True),
 ('ホームページを作成して、公開しないで',True),
 ('ホームページを作成しないで',False),
 ('HTMLの作り方を教えて',False),
 ('Explain how to create a website',False),
])
def test_creation_prompt_does_not_confuse_content_with_explanation(prompt,expected):
    from karry_agent.core import _is_website_creation_request
    assert _is_website_creation_request(prompt)==expected

@pytest.mark.parametrize('failure',['missing','changed','origin'])
def test_public_url_is_hidden_when_published_content_is_not_verified(env,prepared,monkeypatch,failure):
    client,sid,result,meta,agent=prepared
    published=client.post('/api/agent/websites/confirm',headers=HEADERS,json={'approval_id':meta['artifact_id'],'sha256':meta['sha256'],'confirmed':True}).json()
    assert published['success']
    path=sites.INSTANT_DIR/meta['filename']
    if failure=='missing':path.unlink()
    elif failure=='changed':path.write_text('<h1>Wrong contents</h1>')
    else:monkeypatch.setenv('RENDER_EXTERNAL_URL','https://new-origin.onrender.com')
    source=client.get(meta['source_url'],headers=HEADERS).json()
    assert source['state']=='unavailable' and source['public_url'] is None and source['html_content']==HTML
    details=client.get('/api/sessions/'+sid,headers=HEADERS).json()
    assert details['latest_url']=='' and details['artifacts'][0]['public_url'] is None
    fresh=client.post('/api/website-artifacts/'+meta['artifact_id']+'/prepare?session_id='+sid,headers=HEADERS)
    assert fresh.status_code==409 and not fresh.json().get('success')


def test_one_pending_slot_suffices_for_create_prepare_and_publish(env,prepared,monkeypatch):
    client,sid,result,meta,agent=prepared
    monkeypatch.setattr(sites,'MAX_PENDING',1)
    fresh=client.post('/api/website-artifacts/'+meta['artifact_id']+'/prepare?session_id='+sid,headers=HEADERS).json()
    assert fresh['confirmation_required'] and fresh['approval_id']==meta['artifact_id'] and len(sites._PENDING)==1
    published=client.post('/api/agent/websites/confirm',headers=HEADERS,json={'approval_id':fresh['approval_id'],'sha256':meta['sha256'],'confirmed':True}).json()
    assert published['success'] and sites._PENDING=={}

"""Private HTML snapshots in the existing durable task JSON, never public by staging."""
from __future__ import annotations
import copy
import hashlib
import re
import threading
import time
from urllib.parse import urlencode
from . import instant_website as sites, website_storage as storage

MAX_ARTIFACTS = 20
MEMORY: dict[str, dict] = {}
LOCK = threading.RLock()


def make(artifact_id: str, plan: dict) -> dict:
    safe_plan = {key: copy.deepcopy(plan[key]) for key in ('html_content','filename','sha256','public_url','size_bytes','persistence','_storage_identity')}
    row = {'id':artifact_id,'state':'draft','created_at':time.time(),'plan':safe_plan}
    validate({artifact_id:row})
    return row


def validate(records: dict) -> None:
    if not isinstance(records,dict) or len(records)>MAX_ARTIFACTS:
        raise ValueError('HTML下書きの保存件数上限または形式が不正です。')
    for aid,row in records.items():
        if not isinstance(aid,str) or not re.fullmatch(r'[a-f0-9]{32}',aid) or not isinstance(row,dict) or row.get('id')!=aid or row.get('state') not in {'draft','published'} or type(row.get('created_at')) not in {int,float}:
            raise ValueError('保存済みHTML下書きの参照が不正です。')
        plan=row.get('plan')
        if not isinstance(plan,dict) or not isinstance(plan.get('html_content'),str) or not storage.FILENAME_RE.fullmatch(str(plan.get('filename',''))) or not isinstance(plan.get('_storage_identity'),str):
            raise ValueError('保存済みHTML下書きの構造が不正です。')
        body=plan['html_content'].encode('utf-8')
        if not 0<len(body)<=sites.MAX_HTML_BYTES or '\x00' in plan['html_content'] or plan.get('size_bytes')!=len(body) or plan.get('sha256')!=hashlib.sha256(body).hexdigest():
            raise ValueError('保存済みHTML下書きの内容が一致しません。')
        if not isinstance(plan.get('public_url'),str) or len(plan['public_url'])>2048 or not plan['public_url'].endswith('/static/instant/'+plan['filename']):
            raise ValueError('保存済みHTMLの公開先参照が不正です。')


def metadata(row: dict, sid: str) -> dict:
    aid=row['id'];plan=row['plan'];query=urlencode({'session_id':sid})
    return {'artifact_id':aid,'state':row['state'],'filename':plan['filename'],'sha256':plan['sha256'],
        'created_at':row['created_at'],'size_bytes':plan['size_bytes'],'persistence':plan['persistence'],
        'source_url':'/api/website-artifacts/'+aid+'?'+query,
        'preview_url':'/artifacts/'+aid+'?'+query,
        'public_url':plan['public_url'] if row['state']=='published' else None}


def summaries(document: dict) -> list[dict]:
    return [metadata(row,document['session_id']) for row in document.get('website_artifacts',{}).values()]


def source(row: dict, sid: str) -> dict:
    return {**metadata(row,sid),'status':'ready','html_content':row['plan']['html_content'],
            'planned_public_url':row['plan']['public_url'],
            'message':'作成済みの非公開プレビューです。公開は内容確認後に行えます。'}


def verified_document(document: dict) -> dict:
    """Availability is a read-time fact, not permanent proof from an earlier publication."""
    result=copy.deepcopy(document)
    for row in result.get('website_artifacts',{}).values():
        if row['state']!='published':
            continue
        plan=row['plan']; valid=False
        try:
            if plan['_storage_identity']==storage.storage_identity() and plan['public_url']==sites.public_base_url()+'/static/instant/'+plan['filename']:
                content=sites.public_artifact_content(plan['filename'])
                body=content if isinstance(content,bytes) else sites.read_local_artifact(content) if content is not None else None
                valid=body is not None and hashlib.sha256(body).hexdigest()==plan['sha256']
        except (storage.StorageError,OSError,ValueError):
            valid=False
        if not valid:
            row['state']='unavailable'
            if result.get('latest_url')==plan['public_url']:
                result['latest_url']=''
    return result

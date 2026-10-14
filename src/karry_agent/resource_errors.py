"""Safe retrieval errors. Missing resources never become fabricated successes."""
from __future__ import annotations

from fastapi.responses import HTMLResponse, JSONResponse
from starlette.datastructures import Headers

_MESSAGES = {
    'artifact': ('指定された成果物が見つかりません。', '公開URLをご確認ください。再起動前の一時保存データや未公開の成果物は、このURLでは取得できません。'),
    'session': ('指定されたタスク・セッションが見つかりません。', 'チャット画面でタスク一覧を再読み込みし、存在するタスクを選択してください。'),
    'image': ('指定された画像が見つかりません。', '対象タスクを確認し、画像が必要な場合はもう一度添付してください。'),
    'run': ('指定された実行記録が見つかりません。', 'タスクの履歴をご確認ください。一時的な実行記録は再起動や保持上限で失われる場合があります。'),
    'resource': ('指定されたページ・リソースが見つかりません。', 'URLをご確認いただくか、チャット画面から目的のタスクを開いてください。'),
}
_PAGE_HEADERS = {
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
}


def kind(path: str) -> str:
    parts = [part for part in path.split('/') if part]
    if parts[:2] == ['static', 'instant'] or path.startswith('/api/artifacts/'):
        return 'artifact'
    if path.startswith(('/api/sessions/', '/sessions/')):
        return 'session'
    if path.startswith(('/api/images/', '/api/pending_images/', '/api/media/')):
        return 'image'
    if path.startswith('/runs/'):
        return 'run'
    return 'resource'


def is_retrieval(path: str, method: str) -> bool:
    return method in {'GET', 'HEAD'} and path.startswith(('/static/', '/api/sessions', '/sessions/', '/api/images/', '/runs/', '/api/artifacts/', '/api/media/'))


def missing(resource: str = 'resource', error_code: str | None = None) -> dict:
    resource = resource if resource in _MESSAGES else 'resource'
    detail, hint = _MESSAGES[resource]
    return {'status': 'not_found', 'detail': detail, 'error_code': error_code or resource + '_not_found',
            'resource_type': resource, 'hint': hint, 'recovery_url': '/'}


def failed(resource: str = 'resource', status_code: int = 503) -> dict:
    resource = resource if resource in _MESSAGES else 'resource'
    return {'status': 'unavailable' if status_code == 503 else 'error',
        'detail': '成果物・リソースを現在読み込めません。' if status_code == 503 else 'リソースの取得中にエラーが発生しました。',
        'error_code': resource + ('_unavailable' if status_code == 503 else '_read_failed'),
        'resource_type': resource,
        'hint': 'データが削除されたとは限りません。時間を置いて再読み込みしてください。解消しない場合は管理者が保存先を確認してください。',
        'recovery_url': '/'}


def denied(resource: str, status_code: int) -> dict:
    detail, hint = {
        400: ('リソース取得のリクエストを確認してください。', 'URLと入力内容をご確認ください。'),
        401: ('このリソースを取得するには認証が必要です。', 'チャット画面で所有者APIトークンを確認してください。'),
        403: ('このリソースにはアクセスできません。', '所有者または管理者へアクセス権をご確認ください。'),
        409: ('リソースの取得が競合しました。', '処理完了後にもう一度読み込み直してください。'),
    }.get(status_code, ('このリソースは現在取得できません。', 'URLと操作内容をご確認ください。'))
    return {'status': 'error', 'detail': detail, 'hint': hint, 'error_code': resource + '_request_failed',
            'resource_type': resource, 'recovery_url': '/'}


def wants_page(path: str, headers: Headers) -> bool:
    if path.startswith(('/api/', '/sessions/', '/runs/', '/health')):
        return False
    accept = headers.get('accept', '').lower()
    # Static images/scripts/styles keep structured errors, not injected HTML.
    qualities = {}
    for item in accept.split(','):
        pieces = [part.strip() for part in item.split(';')]
        quality = 1.0
        for part in pieces[1:]:
            if part.startswith('q='):
                try:
                    quality = float(part[2:])
                except ValueError:
                    quality = 0.0
        qualities[pieces[0]] = quality if 0 <= quality <= 1 else 0.0
    if 'text/html' in qualities:
        return qualities['text/html'] > max(0.0, qualities.get('application/json', 0.0))
    if 'application/json' in qualities:
        return False
    return kind(path) == 'artifact' and (not accept or qualities.get('*/*', 0.0) > 0)


def respond(scope: dict, payload: dict, status_code: int):
    path = scope.get('path', '')
    if wants_page(path, Headers(scope=scope)):
        title = '成果物が見つかりません' if status_code == 404 and payload.get('resource_type') == 'artifact' else 'ページが見つかりません' if status_code == 404 else '現在読み込めません'
        # Every displayed string comes from this module, never IDs, filenames,
        # exception messages, request URLs, provider text or secret configuration.
        trusted = missing(payload.get('resource_type')) if status_code == 404 else failed(payload.get('resource_type'), status_code) if status_code >= 500 else denied(payload.get('resource_type'), status_code)
        html = f'''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} | karry-agent</title><style>body{{margin:0;background:#0b1224;color:#edf2fa;font:16px/1.8 system-ui,sans-serif;display:flex;min-height:100dvh;align-items:center;justify-content:center}}main{{width:min(580px,calc(100% - 64px));padding:32px 0}}.brand{{color:#93bbf7;letter-spacing:.04em}}h1{{font-size:clamp(24px,5vw,34px);line-height:1.4}}p{{color:#b8c6dc}}.code{{color:#8c9bb0;font-size:14px}}a{{display:inline-block;background:#3d6cac;color:white;text-decoration:none;padding:12px 20px;border-radius:10px;margin-top:16px}}a:focus-visible{{outline:3px solid #b9d7ff;outline-offset:4px}}</style></head><body><main><div class="brand">karry-agent</div><p class="code">HTTP {status_code}</p><h1>{title}</h1><p>{trusted['detail']}</p><p>{trusted['hint']}</p><a href="/">チャット画面に戻る</a></main></body></html>'''
        response = HTMLResponse(html, status_code=status_code, headers=_PAGE_HEADERS)
    else:
        response = JSONResponse(payload, status_code=status_code, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
    if scope.get('method') == 'HEAD':
        response.body = b''
    return response


def http_failure(scope: dict, status_code: int, headers: dict | None = None):
    resource = kind(scope.get('path', ''))
    payload = missing(resource) if status_code == 404 else failed(resource, status_code) if status_code >= 500 else denied(resource, status_code)
    response = respond(scope, payload, status_code)
    # Preserve protocol headers, not arbitrary error-specific/internal headers.
    for name, value in (headers or {}).items():
        if name.lower() in {'allow', 'retry-after', 'www-authenticate'}:
            response.headers[name] = value
    return response

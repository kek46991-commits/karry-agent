"""Request-local privacy guard; it never changes process-wide storage configuration."""
from __future__ import annotations

import json
import logging
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from starlette.responses import JSONResponse

_ACTIVE: ContextVar[bool] = ContextVar('karry_secret_chat', default=False)
_LOG_LOCK = threading.Lock()
CHAT_PATHS = {'/api/chat', '/chat', '/run', '/agent/run', '/api/secret/forget'}
MAX_JSON_BYTES = 256 * 1024


class DuplicateJSONKey(ValueError):
    pass


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateJSONKey("duplicate JSON key")
        value[key] = item
    return value


def is_active() -> bool:
    return _ACTIVE.get()


class _NoSecretLogs(logging.Filter):
    def filter(self, record):
        return not is_active()


_FILTER = _NoSecretLogs()


def _filter_handlers() -> None:
    # ContextVar is checked at emit time, so concurrent normal requests keep their logs.
    with _LOG_LOCK:
        loggers = [logging.getLogger()] + [item for item in logging.Logger.manager.loggerDict.values()
                                          if isinstance(item, logging.Logger)]
        for logger in loggers:
            for handler in logger.handlers:
                if _FILTER not in handler.filters:
                    handler.addFilter(_FILTER)


@contextmanager
def scope():
    _filter_handlers()
    token = _ACTIVE.set(True)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


class SecretChatPrivacyMiddleware:
    """Mark privacy before parsing/validation, including malformed flagged requests."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, request_scope, receive, send):
        if request_scope.get('type') != 'http' or request_scope.get('method') != 'POST' or request_scope.get('path', '').rstrip('/') not in CHAT_PATHS:
            return await self.app(request_scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            body.extend(message.get('body', b''))
            if len(body) > MAX_JSON_BYTES:
                return await JSONResponse({'detail': 'チャット要求の容量上限を超えています。'}, status_code=413,
                                          headers={'Cache-Control': 'no-store'})(request_scope, receive, send)
            if not message.get('more_body', False):
                break
        try:
            value = json.loads(body, object_pairs_hook=_unique_object)
            marked = request_scope.get('path', '').rstrip('/') == '/api/secret/forget' or (isinstance(value, dict) and any(value.get(key) is True for key in ('isSecret', 'is_secret')))
        except DuplicateJSONKey:
            return await JSONResponse({'detail': '同じJSONキーを重複して指定できません。'}, status_code=400,
                                      headers={'Cache-Control': 'private, no-store'})(request_scope, receive, send)
        except (ValueError, UnicodeError):
            marked = bool(re.search(rb'"(?:isSecret|is_secret)"\s*:\s*true\b', body))
        request_scope['secret_chat'] = marked
        consumed = False

        async def buffered_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        async def private_send(message):
            if marked and message['type'] == 'http.response.start':
                message = dict(message)
                headers = [(k, v) for k, v in message.get('headers', []) if k.lower() != b'cache-control']
                headers.append((b'cache-control', b'private, no-store'))
                message['headers'] = headers
            await send(message)

        if marked:
            with scope():
                await self.app(request_scope, buffered_receive, private_send)
        else:
            await self.app(request_scope, buffered_receive, private_send)

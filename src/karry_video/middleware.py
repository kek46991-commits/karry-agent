import secrets

from starlette.responses import JSONResponse


def auth_error(settings, authorization):
    if settings.allow_unauthenticated:
        return None
    if not settings.api_token:
        return 503, "api_token_not_configured", "VIDEO_API_TOKENまたはKARRY_API_TOKENを設定してください。"
    if not secrets.compare_digest(authorization.encode(), f"Bearer {settings.api_token}".encode()):
        return 401, "unauthorized", "有効なBearerトークンが必要です。"
    return None


class VideoGuardMiddleware:
    """動画APIだけに適用する早期認証・リクエスト容量制限。"""

    def __init__(self, app, settings, enforce_token=True):
        self.app = app
        self.settings = settings
        self.enforce_token = enforce_token

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/api/video" or path.startswith("/api/video/")):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        if self.enforce_token:
            error = auth_error(self.settings, headers.get(b"authorization", b"").decode("latin1"))
            if error:
                status, code, message = error
                response = JSONResponse({"detail": {"code": code, "message": message}}, status_code=status, headers={"Cache-Control": "no-store"})
                return await response(scope, receive, send)
        if scope.get("method") not in {"POST", "PATCH", "PUT"}:
            return await self.app(scope, receive, send)
        maximum = self.settings.max_upload_bytes + 65536 if path == "/api/video/projects" else 65536
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = maximum + 1
        if length > maximum or length < 0:
            response = JSONResponse({"detail": {"code": "request_too_large", "message": "リクエストの容量上限を超えています。"}}, status_code=413)
            return await response(scope, receive, send)
        chunks = []
        total = 0
        while True:
            chunk = await receive()
            if chunk["type"] == "http.disconnect":
                return
            body = chunk.get("body", b"")
            total += len(body)
            if total > maximum:
                response = JSONResponse({"detail": {"code": "request_too_large", "message": "リクエストの容量上限を超えています。"}}, status_code=413)
                return await response(scope, receive, send)
            chunks.append(body)
            if not chunk.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)

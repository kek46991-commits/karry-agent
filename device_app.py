"""Role-specific web shell for independently deployed Karry device apps."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
ROLES = {"monitor": "monitor.html", "worker": "companion.html"}
REQUEST_HEADERS = {"accept", "content-type", "cookie", "x-device-token", "x-karry-device"}
RESPONSE_HEADERS = {"cache-control", "content-type", "set-cookie"}
MAX_PROXY_BODY = 700 * 1024


def configured_role() -> str:
    role = os.getenv("KARRY_DEVICE_ROLE", "").strip().lower()
    if role not in ROLES:
        raise RuntimeError("KARRY_DEVICE_ROLE must be monitor or worker")
    return role


def configured_backend() -> str:
    raw = os.getenv("KARRY_BACKEND_URL", "").strip().rstrip("/")
    parsed = urlsplit(raw)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme not in ({"http", "https"} if local else {"https"}) or not parsed.hostname or parsed.username:
        raise RuntimeError("KARRY_BACKEND_URL must be an HTTPS origin")
    return raw


ROLE = configured_role()
BACKEND = configured_backend()
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "Permissions-Policy": "display-capture=()" if ROLE == "monitor" else "display-capture=(self)",
}

app = FastAPI(title=f"Karry {ROLE}", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "role": ROLE}


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    return role_page()


@app.get("/monitor", include_in_schema=False)
def monitor_alias():
    if ROLE != "monitor":
        raise HTTPException(status_code=404)
    return role_page()


@app.get("/companion", include_in_schema=False)
def worker_alias():
    if ROLE != "worker":
        raise HTTPException(status_code=404)
    return role_page()


def role_page() -> HTMLResponse:
    page = (STATIC_DIR / ROLES[ROLE]).read_text(encoding="utf-8")
    page = page.replace(f'/static/{ROLE if ROLE == "monitor" else "companion"}.webmanifest', "/manifest.webmanifest", 1)
    page = page.replace(
        '<script src="/static/device-common.js"></script>',
        '<script>window.KARRY_DEVICE_SCOPE="/"</script><script src="/static/device-common.js"></script>',
        1,
    )
    return HTMLResponse(page, headers=PAGE_HEADERS)


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> JSONResponse:
    filename = "monitor.webmanifest" if ROLE == "monitor" else "companion.webmanifest"
    import json

    data = json.loads((STATIC_DIR / filename).read_text(encoding="utf-8"))
    data["start_url"] = "/"
    data["scope"] = "/"
    return JSONResponse(data, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@app.get("/device-sw.js", include_in_schema=False)
def device_service_worker() -> FileResponse:
    return FileResponse(STATIC_DIR / "device-sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.api_route(
    "/api/autonomy/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    include_in_schema=False,
)
async def autonomy_proxy(path: str, request: Request) -> Response:
    headers = {key: value for key, value in request.headers.items() if key.lower() in REQUEST_HEADERS}
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_PROXY_BODY:
        raise HTTPException(status_code=413, detail="request is too large")
    body = await request.body()
    if len(body) > MAX_PROXY_BODY:
        raise HTTPException(status_code=413, detail="request is too large")
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            upstream = await client.request(
                request.method,
                f"{BACKEND}/api/autonomy/{path}",
                params=request.query_params,
                headers=headers,
                content=body,
            )
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Karry本体に接続できません") from None
    response_headers = {key: value for key, value in upstream.headers.items() if key.lower() in RESPONSE_HEADERS}
    return Response(upstream.content, status_code=upstream.status_code, headers=response_headers)

"""Owner-hosted iPhone enrollment: automatic UDID capture plus OTA install.

Flow: ``GET /ota/`` → tap the button → ``GET /ota/profile`` returns a one-time
"Profile Service" ``.mobileconfig`` → iOS posts only UDID/PRODUCT/VERSION to
``POST /api/register-udid/{token}`` → 301 to ``/ota/status/{token}`` which opens
``itms-services://`` when the signed IPA already covers that UDID, otherwise shows
"registered, waiting for the next build". Disabled unless ``KARRY_OTA_ENROLLMENT=1``.
"""
from __future__ import annotations

import json
import logging
import os
import plistlib
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

TOKEN_TTL_SECONDS = 30 * 60
MAX_PENDING = 500
MAX_BODY = 64 * 1024
ATTRIBUTES = ["UDID", "PRODUCT", "VERSION"]
_UDID = re.compile(r"^(?:[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9A-Fa-f]{40})$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_SAFE_TEXT = re.compile(r"[^A-Za-z0-9 ._,()-]")
NO_STORE = {"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex"}
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_WORKFLOW = re.compile(r"^[\w.-]+\.ya?ml$")
log = logging.getLogger(__name__)


def dispatch_signing(udid: str, covered: bool) -> str:
    """Start the GitHub signing workflow for a newly enrolled UDID (best effort, never raises).

    Needs ``KARRY_GITHUB_TOKEN`` (fine-grained PAT with Actions: write on the repo); without it the
    device stays in the registry and the workflow must be run by hand. The UDID is never logged.
    """
    token = os.getenv("KARRY_GITHUB_TOKEN", "").strip()
    repo = os.getenv("KARRY_GITHUB_REPO", "kek46991-commits/karry-agent").strip()
    workflow = os.getenv("KARRY_OTA_WORKFLOW", "ota-karry-agent-2.yml").strip()
    ref = os.getenv("KARRY_OTA_WORKFLOW_REF", "main").strip()
    if not token:
        return "skipped: KARRY_GITHUB_TOKEN not set"
    if covered:
        return "skipped: current build already covers this device"
    if not _REPO.match(repo) or not _WORKFLOW.match(workflow) or not re.fullmatch(r"[\w./-]{1,100}", ref):
        return "skipped: invalid KARRY_GITHUB_REPO / KARRY_OTA_WORKFLOW / KARRY_OTA_WORKFLOW_REF"
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches",
        data=json.dumps({"ref": ref, "inputs": {"udids": udid}}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
                 "User-Agent": "karry-agent-ota"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return f"dispatched: HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return f"failed: HTTP {exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError):
        return "failed: network error"


def _notify_signing(udid: str, covered: bool) -> None:
    def run() -> None:
        log.info("ota signing workflow for %s: %s", mask_udid(udid), dispatch_signing(udid, covered))
    threading.Thread(target=run, name="ota-dispatch", daemon=True).start()


def enabled() -> bool:
    return os.getenv("KARRY_OTA_ENROLLMENT", "").strip() == "1"


def _require_enabled() -> None:
    if not enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _clean(value: Any, limit: int = 64) -> str:
    return _SAFE_TEXT.sub("", str(value))[:limit]


def extract_plist(body: bytes) -> dict[str, Any]:
    """Read the device attribute plist from a raw or PKCS7/CMS-wrapped body."""
    if len(body) > MAX_BODY:
        raise ValueError("body too large")
    start, end = body.find(b"<?xml"), body.rfind(b"</plist>")
    if start < 0 or end < 0:
        try:
            data = plistlib.loads(body)
        except Exception as exc:
            raise ValueError("no plist in body") from exc
    else:
        data = plistlib.loads(body[start:end + len(b"</plist>")])
    if not isinstance(data, dict):
        raise ValueError("plist is not a dictionary")  # noqa: TRY004 - callers treat any bad body as invalid
    return data


def normalize_udid(value: Any) -> str:
    text = str(value or "").strip()
    if not _UDID.match(text):
        raise ValueError("invalid UDID")
    return text.upper()


def mask_udid(udid: str) -> str:
    return udid[:8] + "…" + udid[-4:]


class DeviceRegistry:
    """Pending one-time tokens and registered UDIDs, in memory or one JSON file."""

    def __init__(self, path: str | None = None, clock=time.time) -> None:
        self._path = None if not path or path == "memory" else Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {"pending": {}, "devices": {}}
        if self._path and self._path.is_file():
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = {"pending": dict(loaded.get("pending", {})), "devices": dict(loaded.get("devices", {}))}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=self._path.parent, delete=False, encoding="utf-8") as handle:
            json.dump(self._data, handle, ensure_ascii=False, indent=1)
            temporary = handle.name
        os.replace(temporary, self._path)

    def _expire(self) -> None:
        now = self._clock()
        for token, entry in list(self._data["pending"].items()):
            if entry.get("udid") is None and now - entry["created_at"] > TOKEN_TTL_SECONDS:
                del self._data["pending"][token]

    def issue(self) -> str:
        with self._lock:
            self._expire()
            if len(self._data["pending"]) >= MAX_PENDING:
                raise HTTPException(status_code=429, detail="too many pending registrations")
            token = secrets.token_urlsafe(24)
            self._data["pending"][token] = {"created_at": self._clock(), "udid": None, "error": None}
            self._save()
            return token

    def register(self, token: str, attributes: dict[str, Any]) -> str:
        with self._lock:
            self._expire()
            entry = self._data["pending"].get(token)
            if entry is None:
                raise KeyError("unknown or expired token")
            if entry.get("udid"):
                raise KeyError("token already used")
            try:
                udid = normalize_udid(attributes.get("UDID"))
            except ValueError:
                entry["error"] = "invalid_device_data"
                self._save()
                raise
            entry.update({"udid": udid, "error": None, "registered_at": self._clock()})
            self._data["devices"][udid] = {
                "product": _clean(attributes.get("PRODUCT", "")),
                "version": _clean(attributes.get("VERSION", "")),
                "registered_at": entry["registered_at"],
            }
            self._save()
            return udid

    def pending(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            self._expire()
            entry = self._data["pending"].get(token)
            return dict(entry) if entry else None

    def devices(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {udid: dict(info) for udid, info in self._data["devices"].items()}

    def remove(self, udid: str) -> bool:
        with self._lock:
            removed = self._data["devices"].pop(udid, None) is not None
            self._save()
            return removed


class SignedBuild:
    """The one signed ad hoc IPA in ``KARRY_OTA_DIR`` and the UDIDs its profile covers."""

    def __init__(self, directory: str | None) -> None:
        self.directory = Path(directory) if directory else None

    def ipa(self) -> Path | None:
        if not self.directory or not self.directory.is_dir():
            return None
        ipas = sorted(self.directory.glob("*.ipa"))
        return ipas[0] if len(ipas) == 1 else None

    def info(self) -> dict[str, Any] | None:
        ipa = self.ipa()
        if ipa is None:
            return None
        with zipfile.ZipFile(ipa) as archive:
            infos = [n for n in archive.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info.plist", n)]
            if len(infos) != 1:
                return None
            info = plistlib.loads(archive.read(infos[0]))
            profile_name = infos[0].removesuffix("Info.plist") + "embedded.mobileprovision"
            if profile_name not in archive.namelist():
                return None
            devices_file = self.directory / "provisioned_devices.json"
            if devices_file.is_file():
                devices = json.loads(devices_file.read_text(encoding="utf-8"))
            else:
                try:
                    devices = extract_plist(archive.read(profile_name)).get("ProvisionedDevices", [])
                except ValueError:
                    devices = []
        return {
            "bundle_id": str(info.get("CFBundleIdentifier", "")),
            "version": str(info.get("CFBundleVersion", "")),
            "title": str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or "Karry-Agent 2"),
            "devices": {str(d).upper() for d in devices if isinstance(d, str)},
        }

    def covers(self, udid: str) -> bool:
        info = self.info()
        return bool(info and udid.upper() in info["devices"])


def classify_browser(user_agent: str) -> str:
    """ios_safari | ios_other | android | other — only iOS Safari can receive a profile."""
    ua = user_agent or ""
    if "Android" in ua:
        return "android"
    if not re.search(r"iPhone|iPad|iPod", ua):
        return "other"
    if re.search(r"CriOS|FxiOS|EdgiOS|OPiOS|Line/|FBAN|FBAV|Instagram|Twitter|GSA/|DuckDuckGo", ua) or "Safari" not in ua:
        return "ios_other"
    return "ios_safari"


def public_base(request: Request) -> str:
    configured = os.getenv("KARRY_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    host = request.headers.get("host", request.url.netloc)
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    return f"{scheme}://{host}"


def build_mobileconfig(base: str, token: str, display_name: str, identifier: str) -> bytes:
    payload = {
        "PayloadContent": {"URL": f"{base}/api/register-udid/{token}", "DeviceAttributes": list(ATTRIBUTES)},
        "PayloadDescription": "この端末のUDIDだけを登録サーバーへ送り、Karry-Agent 2のインストールに進みます。",
        "PayloadDisplayName": display_name,
        "PayloadIdentifier": identifier,
        "PayloadOrganization": "Karry-Agent",
        "PayloadRemovalDisallowed": False,
        "PayloadType": "Profile Service",
        "PayloadUUID": str(uuid.uuid4()).upper(),
        "PayloadVersion": 1,
    }
    raw = plistlib.dumps(payload)
    cert, key = os.getenv("KARRY_OTA_PROFILE_SIGNING_CERT", ""), os.getenv("KARRY_OTA_PROFILE_SIGNING_KEY", "")
    if cert and key:
        signed = subprocess.run(
            ["openssl", "smime", "-sign", "-binary", "-nodetach", "-outform", "DER", "-signer", cert, "-inkey", key],
            input=raw, capture_output=True, check=False,
        )
        if signed.returncode == 0 and signed.stdout:
            return signed.stdout
    return raw


def _page(title: str, body: str, status: int = 200, head: str = "") -> HTMLResponse:
    html = (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title>{head}"
        "<style>body{font-family:-apple-system,system-ui,sans-serif;background:#0f172a;color:#f8fafc;margin:0;padding:28px 20px}"
        "h1{font-size:22px}p{color:#cbd5e1;line-height:1.6}code{color:#fde68a}"
        ".btn{display:block;text-align:center;background:#2563eb;color:#fff;text-decoration:none;padding:16px;border-radius:12px;font-size:17px;margin:20px 0}"
        ".box{background:#1e293b;border-radius:12px;padding:14px 16px;margin:14px 0}.ok{color:#4ade80}.warn{color:#fbbf24}.err{color:#f87171}"
        "input{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #334155;background:#0b1220;color:#f8fafc;font-size:16px}</style>"
        f"</head><body>{body}</body></html>"
    )
    return HTMLResponse(html, status_code=status, headers=NO_STORE)


def build_router(require_owner, registry: DeviceRegistry | None = None, build: SignedBuild | None = None) -> APIRouter:
    router = APIRouter(dependencies=[Depends(_require_enabled)])
    state = {"registry": registry, "build": build}

    def reg() -> DeviceRegistry:
        if state["registry"] is None:
            state["registry"] = DeviceRegistry(os.getenv("KARRY_OTA_REGISTRY", "data/ota_devices.json"))
        return state["registry"]

    def signed() -> SignedBuild:
        if state["build"] is None:
            state["build"] = SignedBuild(os.getenv("KARRY_OTA_DIR"))
        return state["build"]

    def invite_ok(code: str | None) -> bool:
        expected = os.getenv("KARRY_OTA_INVITE_CODE", "")
        return not expected or secrets.compare_digest((code or "").strip(), expected)

    @router.get("/ota/", response_class=HTMLResponse, include_in_schema=False)
    def register_page(request: Request) -> HTMLResponse:
        needs_code = bool(os.getenv("KARRY_OTA_INVITE_CODE", ""))
        browser = classify_browser(request.headers.get("user-agent", ""))
        if browser != "ios_safari":
            reason = {"android": "この端末はAndroidです。構成プロファイルはiPhone専用のため、ボタンを押しても何も起きません。",
                      "ios_other": "iPhoneですがSafari以外のブラウザ（アプリ内ブラウザ・Chromeなど）です。プロファイルを受け取れるのはSafariだけです。",
                      }.get(browser, "iPhone以外の環境です。構成プロファイルはiPhoneのSafariでのみ受け取れます。")
            body = ('<h1 class="warn">この画面はiPhoneのSafariで開いてください</h1>'
                    f'<div class="box"><p>{escape(reason)}</p>'
                    "<p>iPhoneのSafariでこのURLを開き直してください（共有 → 「Safariで開く」、またはURLをコピーしてSafariに貼り付け）。</p></div>"
                    '<p><a class="btn" href="/ota/?force=1">それでも続ける</a></p>')
            if request.query_params.get("force") != "1":
                return _page("Karry-Agent 2 端末登録", body)
        form = (
            '<form method="get" action="/ota/profile">'
            + ('<p><input name="code" placeholder="招待コード" autocomplete="off" required></p>' if needs_code else "")
            + '<button class="btn" type="submit" style="width:100%;border:0">デバイスを登録して進む</button></form>'
        )
        body = (
            "<h1>Karry-Agent 2 をこのiPhoneに入れる</h1>"
            '<div class="box"><p>1. ボタンを押すと構成プロファイルがダウンロードされます。<br>'
            "2. 設定 → 「プロファイルがダウンロード済み」→ インストール。<br>"
            "3. 端末のUDIDだけが自動送信され、インストール画面に戻ります。</p>"
            "<p>送信されるのは UDID・機種・iOSバージョンのみです。連絡先・写真・位置情報などは送信しません。プロファイルは登録後に削除できます。</p></div>"
            + form
            + '<p id="after" hidden class="box">ダウンロードの許可が出たら「許可」→ 設定アプリ →「プロファイルがダウンロード済み」→ インストール。'
            '何も出ない場合は、Safariで開いているか確認してください。</p>'
            "<p>Safariで開いてください。他のブラウザではプロファイルを受け取れません。</p>"
            "<script>document.querySelector('form').addEventListener('submit',function(){"
            "setTimeout(function(){document.getElementById('after').hidden=false},1500)})</script>"
        )
        return _page("Karry-Agent 2 端末登録", body)

    @router.get("/ota/profile", include_in_schema=False)
    def profile(request: Request, code: str | None = None) -> Response:
        if not invite_ok(code):
            return _page("招待コードが違います", '<h1 class="err">招待コードが違います</h1><p><a class="btn" href="/ota/">戻る</a></p>', 403)
        token = reg().issue()
        base = public_base(request)
        content = build_mobileconfig(base, token, "Karry-Agent 2 端末登録", "ai.karry.agent2.udid-registration")
        return Response(content, media_type="application/x-apple-aspen-config",
                        headers={**NO_STORE, "Content-Disposition": 'attachment; filename="karry-agent-2-register.mobileconfig"'})

    @router.post("/api/register-udid/{token}", include_in_schema=False)
    async def register_udid(token: str, request: Request) -> Response:
        if not _TOKEN.match(token):
            return RedirectResponse("/ota/status/invalid", status_code=301, headers=NO_STORE)
        body = await request.body()
        try:
            attributes = extract_plist(body)
            udid = reg().register(token, attributes)
        except (KeyError, ValueError):
            pass  # the status page explains; never echo device data back
        else:
            _notify_signing(udid, signed().covers(udid))
        return RedirectResponse(f"/ota/status/{token}", status_code=301, headers=NO_STORE)

    @router.get("/ota/status/{token}", response_class=HTMLResponse, include_in_schema=False)
    def status_page(token: str, request: Request) -> HTMLResponse:
        entry = reg().pending(token) if _TOKEN.match(token) else None
        retry = '<a class="btn" href="/ota/">最初からやり直す</a>'
        if entry is None:
            return _page("登録リンクの期限切れ", '<h1 class="warn">登録リンクが無効か期限切れです</h1>'
                         "<p>30分以内にプロファイルをインストールしてください。</p>" + retry, 410)
        if entry.get("error"):
            return _page("登録に失敗しました", '<h1 class="err">端末情報を受け取れませんでした</h1>'
                         "<p>Safariで登録ページを開き、プロファイルを再度インストールしてください。</p>" + retry, 400)
        udid = entry.get("udid")
        if not udid:
            return _page("プロファイル待ち", '<h1 class="warn">まだ端末情報が届いていません</h1>'
                         "<p>設定アプリで「プロファイルがダウンロード済み」を開き、インストールしてください。</p>" + retry, 202)
        masked = escape(mask_udid(udid))
        if signed().covers(udid):
            link = "itms-services://?action=download-manifest&url=" + quote(public_base(request) + "/ota/manifest.plist", safe="")
            safe_link = escape(link, quote=True)
            head = f'<meta http-equiv="refresh" content="1;url={safe_link}">'
            return _page("インストール", f'<h1 class="ok">登録完了: {masked}</h1>'
                         "<p>インストール画面を開いています。開かない場合は下のボタンを押してください。</p>"
                         f'<a class="btn" href="{safe_link}">Karry-Agent 2 をインストール</a>'
                         "<p>インストール後、設定 → 一般 → VPNとデバイス管理 から登録用プロファイルを削除できます。</p>", 200, head)
        return _page("登録完了（ビルド待ち）", f'<h1 class="ok">登録完了: {masked}</h1>'
                     '<div class="box"><p class="warn">この端末向けの署名済みビルドはまだありません。</p>'
                     "<p>管理者が端末を登録して再ビルドすると、このページからインストールできます。このページをブックマークしてください。</p></div>"
                     f'<a class="btn" href="/ota/status/{escape(token)}">もう一度確認する</a>', 200)

    @router.get("/ota/manifest.plist", include_in_schema=False)
    def manifest(request: Request) -> Response:
        info = signed().info()
        if info is None:
            raise HTTPException(status_code=404, detail="No signed build is published")
        data = {"items": [{
            "assets": [{"kind": "software-package", "url": public_base(request) + "/ota/app.ipa"}],
            "metadata": {"bundle-identifier": info["bundle_id"], "bundle-version": info["version"],
                         "kind": "software", "title": info["title"]},
        }]}
        return Response(plistlib.dumps(data), media_type="application/xml", headers=NO_STORE)

    @router.get("/ota/app.ipa", include_in_schema=False)
    def ipa() -> FileResponse:
        path = signed().ipa()
        if path is None:
            raise HTTPException(status_code=404, detail="No signed build is published")
        return FileResponse(path, media_type="application/octet-stream", filename=path.name, headers=NO_STORE)

    @router.get("/api/ota/devices", dependencies=[Depends(require_owner)])
    def list_devices() -> dict[str, Any]:
        info = signed().info()
        covered = info["devices"] if info else set()
        devices = [{"udid": udid, **meta, "provisioned": udid in covered} for udid, meta in sorted(reg().devices().items())]
        return {"devices": devices, "signed_build": None if info is None else
                {"bundle_id": info["bundle_id"], "version": info["version"], "provisioned_count": len(covered)}}

    @router.delete("/api/ota/devices/{udid}", dependencies=[Depends(require_owner)])
    def delete_device(udid: str) -> dict[str, bool]:
        try:
            return {"removed": reg().remove(normalize_udid(udid))}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid UDID") from exc

    return router

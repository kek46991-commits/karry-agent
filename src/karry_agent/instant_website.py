"""Instant static HTML previews; ephemeral filesystem, no external publishing API."""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .self_healing import _SECRET_KEY, healing_engine

STATIC_ROOT = Path(__file__).resolve().parents[2] / "static"
INSTANT_DIR = STATIC_ROOT / "instant"
MAX_HTML_BYTES = 524_288
MAX_FILES = 128
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_PENDING = 20
APPROVAL_TTL = 600
_LOCK = threading.RLock()
_PENDING: dict[str, dict[str, Any]] = {}

# Sandbox without allow-same-origin gives inline scripts an opaque origin:
# generated JS cannot read the chat application's storage or make same-origin API calls.
PUBLIC_HEADERS = {
    "Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline' https:; "
        "style-src 'unsafe-inline' https:; img-src data: https:; font-src data: https:; "
        "connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def configure_static_root(root: Path) -> None:
    """Host application binds the actual static mount, never a model/user path."""
    global STATIC_ROOT, INSTANT_DIR
    STATIC_ROOT = root.resolve()
    INSTANT_DIR = STATIC_ROOT / "instant"


def _html_bytes(html_content: str) -> bytes:
    if not isinstance(html_content, str) or not html_content.strip():
        raise ValueError("HTMLコンテンツは空にできません。")
    if len(html_content) > MAX_HTML_BYTES:
        raise ValueError("HTML容量は512 KiB以内にしてください。")
    body = html_content.encode("utf-8")
    if len(body) > MAX_HTML_BYTES or "\x00" in html_content:
        raise ValueError("HTML容量または文字の形式が不正です。")
    for key, value in os.environ.items():
        if _SECRET_KEY.search(key) and value.strip():
            secret = value.strip()
            private = key in {"PARTNER_NAME", "CORE_USER_NAME", "CORE_PRIVATE_CONTEXT"}
            found = secret in html_content if private or len(secret) >= 8 else re.search(
                r"(?<![\w-])" + re.escape(secret) + r"(?![\w-])", html_content)
            if found:
                raise ValueError("公開HTMLに秘密情報または非公開設定値が含まれています。")
    return body


def _filename(filename: str | None) -> str:
    if filename is not None and (not isinstance(filename, str) or len(filename) > 200):
        raise ValueError("ファイル名は200文字以内にしてください。")
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", Path(filename or "site").stem).strip("_")[:60] or "site"
    # A full random ID isolates each publication and never overwrites the chat UI
    # or a previous artifact, even when the caller requests index.html.
    return f"{stem}_{uuid.uuid4().hex}.html"


def public_base_url() -> str:
    base = os.getenv("RENDER_EXTERNAL_URL", "https://karry-agent-1.onrender.com").rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("公開ベースURLの形式が不正です。")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or any(ord(c) <= 32 for c in base):
        raise ValueError("公開ベースURLはHTTP(S)のオリジンを指定してください。")
    return base


def _plan(html_content: str, filename: str | None) -> dict[str, Any]:
    body = _html_bytes(html_content)
    name = _filename(filename)
    return {"html_content": html_content, "filename": name, "sha256": hashlib.sha256(body).hexdigest(),
            "public_url": public_base_url() + "/static/instant/" + name, "size_bytes": len(body)}


def _publish(plan: dict[str, Any]) -> dict[str, Any]:
    body = _html_bytes(plan["html_content"])
    if hashlib.sha256(body).hexdigest() != plan["sha256"]:
        raise ValueError("確認済みHTMLと内容が一致しません。")
    # Public destination is also part of the confirmed payload.
    if public_base_url() + "/static/instant/" + plan["filename"] != plan["public_url"]:
        raise ValueError("公開先の設定が変わりました。内容を再確認してください。")
    with _LOCK:
        if INSTANT_DIR.is_symlink():
            raise ValueError("公開ディレクトリの形式が不正です。")
        INSTANT_DIR.mkdir(parents=True, exist_ok=True)
        entries = list(INSTANT_DIR.iterdir())
        if len(entries) >= MAX_FILES or sum(p.stat().st_size for p in entries if p.is_file()) + len(body) > MAX_TOTAL_BYTES:
            raise ValueError("一時公開領域の容量上限に達しています。")
        target = INSTANT_DIR / plan["filename"]
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=INSTANT_DIR, prefix=".pending-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(body)
                stream.flush()
            # Atomic no-replace link: the preannounced URL never serves a partial
            # write and neither an existing file nor symlink is overwritten.
            os.link(temporary, target)
        except FileExistsError:
            raise ValueError("公開ファイルが既に存在します。再作成してください。") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return {"success": True, "status": "published", "message": "HTMLの即時公開が完了しました。",
            "filename": plan["filename"], "public_url": plan["public_url"], "sha256": plan["sha256"],
            "size_bytes": len(body), "persistence": "ephemeral",
            "warning": "Renderの再起動・再デプロイ・インスタンス切替で失われる一時公開です。永続保存ではありません。"}


def _error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ValueError):
        message = str(exc)
    else:
        healing_engine.analyze_and_heal(exc, {"source": "instant_website", "operation": "publish"})
        message = "HTMLの即時公開に失敗しました。"
    return {"success": False, "status": "error", "error": message}


def deploy_instant_website(html_content: str, filename: str | None = None) -> dict[str, Any]:
    """Publish an exact caller-supplied payload; model registry uses prepare instead."""
    try:
        return _publish(_plan(html_content, filename))
    except Exception as exc:
        return _error(exc)


def prepare_instant_website(html_content: str, filename: str | None = None) -> dict[str, Any]:
    """Model calls only stage the exact payload for owner confirmation, never publish."""
    try:
        plan = _plan(html_content, filename)
        with _LOCK:
            now = time.monotonic()
            for key in list(_PENDING):
                if _PENDING[key]["expires"] <= now:
                    _PENDING.pop(key)
            if len(_PENDING) >= MAX_PENDING:
                raise ValueError("公開確認待ちが上限に達しています。")
            approval_id = uuid.uuid4().hex
            _PENDING[approval_id] = {**plan, "expires": now + APPROVAL_TTL}
        return {"success": False, "status": "needs_user_action", "confirmation_required": True,
                "approval_id": approval_id, "preview": plan, "expires_in_seconds": APPROVAL_TTL,
                "message": "このHTML全文・公開URLを確認してください。公開前に所有者の確認が必要です。",
                "confirmation_endpoint": "/api/agent/websites/confirm"}
    except Exception as exc:
        return _error(exc)


def confirm_instant_website(approval_id: str, sha256: str, confirmed: bool) -> dict[str, Any]:
    """Called only by the authenticated owner API, never exposed as a model tool."""
    with _LOCK:
        plan = _PENDING.get(approval_id)
        if plan is None or plan["expires"] <= time.monotonic():
            _PENDING.pop(approval_id, None)
            return _error(ValueError("確認待ちが存在しないか、有効期限が切れています。"))
        if sha256 != plan["sha256"]:
            return _error(ValueError("確認対象のSHA256が一致しません。"))
        if not confirmed:
            _PENDING.pop(approval_id)
            return {"success": False, "status": "cancelled", "message": "公開をキャンセルしました。"}
        # Remove before side effect: concurrent/replayed confirmations cannot publish twice.
        _PENDING.pop(approval_id)
        try:
            return _publish(plan)
        except Exception as exc:
            return _error(exc)


def public_artifact_path(filename: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}_[a-f0-9]{32}\.html", filename) or INSTANT_DIR.is_symlink():
        return None
    target = INSTANT_DIR / filename
    return target if target.is_file() and not target.is_symlink() else None

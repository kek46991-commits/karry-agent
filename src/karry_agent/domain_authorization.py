"""Short-lived domain-owner consent and control verification for hack targets."""
from __future__ import annotations

import hashlib
import html
import ipaddress
import secrets
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import requests

TTL_SECONDS = 24 * 60 * 60
WELL_KNOWN_PATH = "/.well-known/karry-agent-verification.txt"
DNS_PREFIX = "_karry-agent-verification"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "testserver"})


class AuthorizationError(ValueError):
    pass


@dataclass
class Authorization:
    id: str
    session_id: str
    origin: str
    host: str
    challenge: str
    approval_hash: str
    expires_at: float
    consented: bool = False
    verified: bool = False
    method: str | None = None

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "origin": self.origin,
            "host": self.host,
            "challenge": self.challenge,
            "expires_at": self.expires_at,
            "consented": self.consented,
            "verified": self.verified,
            "method": self.method,
            "dns_name": f"{DNS_PREFIX}.{self.host}",
            "file_url": f"{self.origin}{WELL_KNOWN_PATH}",
        }


_LOCK = threading.Lock()
_AUTHORIZATIONS: dict[str, Authorization] = {}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_origin(target_url: str) -> tuple[str, str]:
    parsed = urlsplit(target_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AuthorizationError("対象は http(s)://host 形式で指定してください。")
    if parsed.username or parsed.password or parsed.fragment:
        raise AuthorizationError("ユーザー情報やフラグメントを含むURLは指定できません。")
    host = parsed.hostname.rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise AuthorizationError("対象URLのポート番号が不正です。") from exc
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", "")), host


def create(session_id: str, target_url: str, *, now: float | None = None) -> tuple[Authorization, str]:
    origin, host = normalize_origin(target_url)
    created = time.time() if now is None else now
    approval_token = secrets.token_urlsafe(32)
    record = Authorization(
        id=secrets.token_hex(16),
        session_id=session_id,
        origin=origin,
        host=host,
        challenge=f"karry-agent={secrets.token_urlsafe(24)}",
        approval_hash=_digest(approval_token),
        expires_at=created + TTL_SECONDS,
    )
    with _LOCK:
        _AUTHORIZATIONS[record.id] = record
    return record, approval_token


def _active(record: Authorization, now: float | None = None) -> Authorization:
    if record.expires_at <= (time.time() if now is None else now):
        raise AuthorizationError("この確認依頼は期限切れです。新しく発行してください。")
    return record


def get(authorization_id: str, *, session_id: str | None = None) -> Authorization:
    with _LOCK:
        record = _AUTHORIZATIONS.get(authorization_id)
    if record is None or (session_id is not None and record.session_id != session_id):
        raise AuthorizationError("確認依頼が見つかりません。")
    return _active(record)


def approve(authorization_id: str, approval_token: str) -> Authorization:
    record = get(authorization_id)
    if not secrets.compare_digest(record.approval_hash, _digest(approval_token)):
        raise AuthorizationError("承認リンクが無効です。")
    with _LOCK:
        record.consented = True
    return record


def list_for_session(session_id: str) -> list[dict[str, object]]:
    now = time.time()
    with _LOCK:
        records = [
            record.public() for record in _AUTHORIZATIONS.values()
            if record.session_id == session_id and record.expires_at > now
        ]
    return sorted(records, key=lambda item: float(item["expires_at"]), reverse=True)


def _public_host(host: str) -> bool:
    if host in _LOCAL_HOSTS:
        return True
    try:
        addresses = {
            item[4][0].split("%", 1)[0]
            for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise AuthorizationError("対象ドメインを解決できません。") from exc
    if not addresses:
        raise AuthorizationError("対象ドメインを解決できません。")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise AuthorizationError("公開インターネット上のドメインだけ確認できます。")
    return True


def _verify_file(record: Authorization) -> bool:
    _public_host(record.host)
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            f"{record.origin}{WELL_KNOWN_PATH}",
            timeout=8,
            allow_redirects=False,
            stream=True,
            headers={"Accept": "text/plain", "User-Agent": "Karry-Agent-Domain-Verification/1"},
        )
    except requests.RequestException as exc:
        raise AuthorizationError("確認ファイルを取得できません。") from exc
    try:
        connection = getattr(response.raw, "_connection", None)
        peer = connection.sock.getpeername()[0] if connection and connection.sock else None
        if peer is None or not ipaddress.ip_address(peer.split("%", 1)[0]).is_global:
            raise AuthorizationError("確認先が公開IPへ接続されなかったため拒否しました。")
        if int(response.headers.get("content-length", "0") or 0) > 1024:
            raise AuthorizationError("確認ファイルが大きすぎます。")
        body = response.raw.read(1025, decode_content=True).decode("utf-8")
    except (AttributeError, UnicodeError, ValueError) as exc:
        raise AuthorizationError("確認ファイルの応答を検証できません。") from exc
    finally:
        response.close()
    if len(body.encode("utf-8")) > 1024:
        raise AuthorizationError("確認ファイルが大きすぎます。")
    return response.status_code == 200 and body.strip() == record.challenge


def _verify_dns(record: Authorization) -> bool:
    _public_host(record.host)
    try:
        response = requests.get(
            "https://dns.google/resolve",
            params={"name": f"{DNS_PREFIX}.{record.host}", "type": "TXT"},
            timeout=8,
            allow_redirects=False,
            headers={"Accept": "application/dns-json", "User-Agent": "Karry-Agent-Domain-Verification/1"},
        )
        response.raise_for_status()
        answers = response.json().get("Answer", [])
    except (requests.RequestException, ValueError, TypeError) as exc:
        raise AuthorizationError("DNS確認を実行できません。") from exc
    values = {
        str(answer.get("data", "")).strip().strip('"')
        for answer in answers if answer.get("type") == 16
    }
    return record.challenge in values


def verify(
    authorization_id: str,
    session_id: str,
    method: str,
    *,
    verifier: Callable[[Authorization], bool] | None = None,
) -> Authorization:
    record = get(authorization_id, session_id=session_id)
    if not record.consented:
        raise AuthorizationError("先にドメイン所有者の承認が必要です。")
    if method not in {"dns", "file"}:
        raise AuthorizationError("確認方法は dns または file を指定してください。")
    check = verifier or (_verify_dns if method == "dns" else _verify_file)
    if not check(record):
        raise AuthorizationError("確認コードが一致しません。設置内容とDNS反映を確認してください。")
    with _LOCK:
        record.verified = True
        record.method = method
    return record


def target_is_authorized(target_url: str, *, session_id: str | None = None) -> bool:
    origin, host = normalize_origin(target_url)
    if host in _LOCAL_HOSTS:
        return True
    now = time.time()
    with _LOCK:
        return any(
            record.origin == origin
            and (session_id is None or record.session_id == session_id)
            and record.consented
            and record.verified
            and record.expires_at > now
            for record in _AUTHORIZATIONS.values()
        )


def approval_page(record: Authorization, approval_token: str, *, approved: bool = False) -> str:
    target = html.escape(record.origin)
    if approved:
        body = f"<h1>Approval recorded</h1><p>{target} may proceed to domain-control verification.</p>"
    else:
        body = (
            f"<h1>Authorize security verification</h1><p>Target: <strong>{target}</strong></p>"
            "<p>This permits only fixed, non-destructive checks after separate DNS or file verification.</p>"
            f'<form method="post"><input type="hidden" name="token" value="{html.escape(approval_token)}">'
            '<button type="submit">Yes, authorize verification</button></form>'
        )
    return (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>Domain authorization</title><body>{body}</body></html>"
    )

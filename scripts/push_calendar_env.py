#!/usr/bin/env python3
"""credentials.json / token.json を探して検証し、Render の環境変数へ設定する。

例 (Termux):
    export RENDER_API_KEY=rnd_xxx
    python scripts/push_calendar_env.py --render
token.json が無い場合はブラウザで OAuth 認証して作成してから登録する:
    python scripts/push_calendar_env.py --login --render
既に Render にある値の余計な記号を除去して設定し直す:
    python scripts/push_calendar_env.py --from-render
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen


def _load_calendar_secrets() -> Any:
    """パッケージ全体の依存を読まずに calendar_secrets だけを読み込む。"""
    path = Path(__file__).resolve().parents[1] / "src" / "karry_agent" / "calendar_secrets.py"
    spec = importlib.util.spec_from_file_location("calendar_secrets", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"{path} が見つかりません")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_secrets = _load_calendar_secrets()
CREDENTIALS_ENV = _secrets.CREDENTIALS_ENV
TOKEN_ENV = _secrets.TOKEN_ENV
CalendarSecretError = _secrets.CalendarSecretError
parse_secret_json = _secrets.parse_secret_json
to_env_value = _secrets.to_env_value
validate_credentials = _secrets.validate_credentials
validate_token = _secrets.validate_token

RENDER_API = "https://api.render.com/v1"
CREDENTIAL_PATTERNS = ("credentials.json", "client_secret*.json")
TOKEN_PATTERNS = ("token.json",)
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
DEFAULT_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "site-packages"}


def search_roots() -> list[Path]:
    home = Path.home()
    roots = [
        Path.cwd(),
        Path(__file__).resolve().parents[1],
        home,
        home / "storage" / "downloads",
        home / "storage" / "shared" / "Download",
        Path("/sdcard/Download"),
    ]
    unique: list[Path] = []
    for root in roots:
        if root.is_dir() and root not in unique:
            unique.append(root)
    return unique


def find_file(patterns: tuple[str, ...], max_depth: int = 3) -> Path | None:
    for root in search_roots():
        for depth in range(max_depth + 1):
            prefix = "*/" * depth
            for pattern in patterns:
                for match in sorted(root.glob(prefix + pattern)):
                    if match.is_file() and not SKIP_DIRS.intersection(match.parts):
                        return match
    return None


def _open_browser(url: str) -> None:
    if shutil.which("termux-open-url"):
        subprocess.run(["termux-open-url", url], check=False)
        return
    try:
        webbrowser.open(url)
    except webbrowser.Error:
        pass


def _wait_for_code(server: HTTPServer, state: str, timeout: int) -> str:
    received: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = {key: values[0] for key, values in parse_qs(urlparse(self.path).query).items()}
            if "code" in query or "error" in query:
                received.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            message = "認証が完了しました。Termux に戻ってください。" if "code" in query else "認証できませんでした。"
            self.wfile.write(f"<p>{message}</p>".encode())

        def log_message(self, *args: Any) -> None:
            return

    server.RequestHandlerClass = Handler
    server.timeout = timeout
    deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout)
    while not received and datetime.datetime.now() < deadline:
        server.handle_request()
    if not received:
        raise SystemExit("ブラウザ認証がタイムアウトしました。もう一度実行してください")
    if received.get("error"):
        raise SystemExit(f"Google 認証エラー: {received['error']}")
    if received.get("state") != state:
        raise SystemExit("認証レスポンスの state が一致しません。もう一度実行してください")
    return received["code"]


def oauth_login(credentials: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
    """Desktop OAuth クライアントでループバック認証し、authorized_user 形式の token を返す。"""
    client = credentials.get("installed") or credentials.get("web") or {}
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    auth_url = (client.get("auth_uri") or DEFAULT_AUTH_URI) + "?" + urlencode(
        {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(CALENDAR_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    print("ブラウザで Google にログインして「許可」を押してください。開かない場合は次の URL を開いてください:")
    print(auth_url, flush=True)
    _open_browser(auth_url)
    try:
        code = _wait_for_code(server, state, timeout)
    finally:
        server.server_close()

    token_uri = client.get("token_uri") or DEFAULT_TOKEN_URI
    body = urlencode(
        {
            "code": code,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
    ).encode()
    request = Request(token_uri, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(request, timeout=30) as response:
            issued = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise SystemExit(f"token の取得に失敗しました: HTTP {exc.code}") from None
    if not issued.get("refresh_token"):
        raise SystemExit(
            "refresh_token が返されませんでした。https://myaccount.google.com/permissions で"
            "アプリのアクセスを削除してから再実行してください"
        )
    expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=int(issued.get("expires_in", 0)))
    return {
        "token": issued.get("access_token"),
        "refresh_token": issued["refresh_token"],
        "token_uri": token_uri,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "scopes": CALENDAR_SCOPES,
        "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def render_request(method: str, path: str, api_key: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        RENDER_API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "karry-agent-setup/0.1",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8")
    except HTTPError as exc:
        hint = " (RENDER_API_KEY が無効です)" if exc.code in (401, 403) else ""
        raise SystemExit(f"Render API {method} {path} failed: HTTP {exc.code}{hint}") from None
    return json.loads(text) if text else None


def find_service_id(api_key: str, name: str) -> str:
    items = render_request("GET", f"/services?name={quote(name)}&limit=20", api_key) or []
    for item in items:
        service = item.get("service", item)
        if service.get("name") == name:
            return service["id"]
    raise SystemExit(f"Render service '{name}' was not found")


def render_env_vars(api_key: str, service_id: str) -> dict[str, str]:
    items = render_request("GET", f"/services/{service_id}/env-vars?limit=100", api_key) or []
    result = {}
    for item in items:
        env_var = item.get("envVar", item)
        result[env_var["key"]] = env_var.get("value", "")
    return result


def normalize(credentials_raw: str, token_raw: str) -> dict[str, str]:
    credentials = validate_credentials(parse_secret_json(credentials_raw, CREDENTIALS_ENV))
    token = validate_token(parse_secret_json(token_raw, TOKEN_ENV), credentials)
    return {CREDENTIALS_ENV: to_env_value(credentials), TOKEN_ENV: to_env_value(token)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--credentials", type=Path, help="credentials.json のパス (省略時は自動検索)")
    parser.add_argument("--token", type=Path, help="token.json のパス (省略時は自動検索)")
    parser.add_argument("--login", action="store_true", help="ブラウザで OAuth 認証して token.json を作成する")
    parser.add_argument("--render", action="store_true", help="ローカルのファイルを Render に設定する")
    parser.add_argument("--from-render", action="store_true", help="Render の既存値を正規化して設定し直す")
    parser.add_argument("--service-name", default="karry-agent")
    parser.add_argument("--service-id", default=os.getenv("RENDER_SERVICE_ID"))
    parser.add_argument("--no-deploy", action="store_true", help="設定後のデプロイを行わない")
    parser.add_argument("--print-env", action="store_true", help="1 行 JSON を KEY=value 形式で表示する (値が表示されます)")
    args = parser.parse_args(argv)

    api_key = os.getenv("RENDER_API_KEY", "").strip()
    use_render = args.render or args.from_render
    if use_render and not api_key:
        raise SystemExit("RENDER_API_KEY を環境変数に設定してください")
    service_id = None
    if use_render:
        service_id = args.service_id or find_service_id(api_key, args.service_name)

    try:
        if args.from_render:
            current = render_env_vars(api_key, service_id or "")
            missing = [key for key in (CREDENTIALS_ENV, TOKEN_ENV) if not current.get(key)]
            if missing:
                raise SystemExit(f"Render に値がありません: {', '.join(missing)}")
            values = normalize(current[CREDENTIALS_ENV], current[TOKEN_ENV])
            changed = [key for key, value in values.items() if current[key] != value]
        else:
            credentials_path = args.credentials or find_file(CREDENTIAL_PATTERNS)
            if not credentials_path:
                raise SystemExit("credentials.json が見つかりません。--credentials でパスを指定してください")
            print(f"credentials: {credentials_path}")
            token_path = args.token or find_file(TOKEN_PATTERNS)
            if args.login:
                credentials = validate_credentials(
                    parse_secret_json(credentials_path.read_text(encoding="utf-8-sig"), CREDENTIALS_ENV)
                )
                token_path = args.token or credentials_path.with_name("token.json")
                write_private_json(token_path, oauth_login(credentials))
                print(f"token.json を作成しました: {token_path}")
            if not token_path:
                raise SystemExit("token.json がありません。--login を付けて実行するとブラウザ認証で作成します")
            print(f"token:       {token_path}")
            values = normalize(
                credentials_path.read_text(encoding="utf-8-sig"),
                token_path.read_text(encoding="utf-8-sig"),
            )
            changed = list(values)
    except CalendarSecretError as exc:
        raise SystemExit(f"形式エラー: {exc}") from None
    print("JSON の形式チェック: OK")

    if args.print_env:
        for key, value in values.items():
            print(f"{key}={value}")
    if not use_render:
        return 0
    for key in changed:
        render_request("PUT", f"/services/{service_id}/env-vars/{key}", api_key, {"value": values[key]})
        print(f"Render に設定しました: {key}")
    if not changed:
        print("Render の値は既に正しい形式です")
    if changed and not args.no_deploy:
        deploy = render_request("POST", f"/services/{service_id}/deploys", api_key, {}) or {}
        print(f"デプロイを開始しました: {deploy.get('id', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

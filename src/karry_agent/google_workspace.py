"""Google OAuth credentials shared by Calendar and Photos; Maps uses a separate key."""
from __future__ import annotations

import os
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError

from .calendar_secrets import parse_secret_json, validate_credentials, validate_token

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
PICKER_SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"


def google_error(exc: Exception, error_type: str) -> dict[str, Any]:
    """Return actionable categories without credential values, request URLs or raw exceptions."""
    if isinstance(exc, RefreshError):
        error_type = "OAuth_Refresh_Error"
    status = None
    if isinstance(exc, HttpError):
        status = exc.resp.status
    elif isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
    message = "Google APIの認証・権限・設定を確認してください。"
    if isinstance(exc, (ValueError, FileNotFoundError)):
        message = "Googleの認証設定または入力が不足・不正です。"
    elif isinstance(exc, requests.Timeout):
        message = "Google APIがタイムアウトしました。"
    result = {"status": "error", "error": message, "message": message,
              "error_type": error_type, "exception_type": type(exc).__name__}
    if status is not None:
        result["http_status"] = status
    return result


def get_google_credentials(scopes: list[str] | None = None, *, photos: bool = False) -> Credentials:
    """Read existing JSON secrets/files or the three OAuth env values, then refresh."""
    scopes = scopes or [CALENDAR_SCOPE]
    token_names = (["GOOGLE_PHOTOS_TOKEN_JSON"] if photos else []) + [
        "GOOGLE_WORKSPACE_TOKEN_JSON", "GOOGLE_CALENDAR_TOKEN_JSON"]
    info = None
    for name in token_names:
        if os.getenv(name):
            info = parse_secret_json(os.environ[name], name)
            break
    if info is None and all(os.getenv(name) for name in (
        "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"
    )):
        info = {"client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"]}
    if info is None:
        path = Path(os.getenv("GOOGLE_PHOTOS_TOKEN_FILE" if photos else "GOOGLE_CALENDAR_TOKEN_FILE", "token.json"))
        if path.is_file():
            info = parse_secret_json(path.read_text(encoding="utf-8"), path.name)
    if info is None:
        if not photos and os.getenv("GOOGLE_CALENDAR_ALLOW_LOCAL_OAUTH") == "1":
            from google_auth_oauthlib.flow import InstalledAppFlow
            path = Path(os.getenv("GOOGLE_CALENDAR_CREDENTIALS_FILE", "credentials.json"))
            raw = os.getenv("GOOGLE_CALENDAR_CREDENTIALS_JSON")
            if raw:
                config = validate_credentials(parse_secret_json(raw, "GOOGLE_CALENDAR_CREDENTIALS_JSON"))
                flow = InstalledAppFlow.from_client_config(config, scopes)
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(path), scopes)
            creds = flow.run_local_server(port=0)
            token_path = Path(os.getenv("GOOGLE_CALENDAR_TOKEN_FILE", "token.json"))
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")
            token_path.chmod(0o600)
            return creds
        raise ValueError("Google OAuth token is not configured; set JSON Secret or GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET/GOOGLE_REFRESH_TOKEN.")
    # A complete token wins over an unrelated malformed credentials secret.
    credentials = None
    if not info.get("client_id") or not info.get("client_secret"):
        raw = os.getenv("GOOGLE_CALENDAR_CREDENTIALS_JSON")
        path = Path(os.getenv("GOOGLE_CALENDAR_CREDENTIALS_FILE", "credentials.json"))
        if raw:
            credentials = validate_credentials(parse_secret_json(raw, "GOOGLE_CALENDAR_CREDENTIALS_JSON"))
        elif path.is_file():
            credentials = validate_credentials(parse_secret_json(path.read_text(encoding="utf-8"), path.name))
    info = validate_token(info, credentials)
    creds = Credentials.from_authorized_user_info(info, scopes=scopes)
    if not creds.valid:
        creds.refresh(partial(Request(), timeout=30))
    return creds


def search_photos(query_filter: str = "", session_id: str = "", page_token: str = "") -> dict[str, Any]:
    """Get user-selected Photos Picker items; filter filenames on the current page."""
    try:
        with AuthorizedSession(get_google_credentials([PICKER_SCOPE], photos=True), refresh_timeout=30) as session:
            base = "https://photospicker.googleapis.com/v1"
            if not session_id:
                response = session.post(f"{base}/sessions", json={}, timeout=(5, 30))
                response.raise_for_status()
                data = response.json()
                return {"status": "needs_user_action", "session_id": data["id"],
                        "pickerUri": data["pickerUri"], "pollingConfig": data.get("pollingConfig", {}),
                        "message": "リンクを開いて写真を選択後、session_idを指定して再実行してください。"}
            response = session.get(f"{base}/sessions/{quote(session_id, safe='')}", timeout=(5, 30))
            response.raise_for_status()
            data = response.json()
            if not data.get("mediaItemsSet"):
                return {"status": "needs_user_action", "session_id": session_id,
                        "pickerUri": data.get("pickerUri"), "pollingConfig": data.get("pollingConfig", {}),
                        "message": "写真の選択はまだ完了していません。"}
            response = session.get(f"{base}/mediaItems", params={"sessionId": session_id,
                                   "pageSize": 100, "pageToken": page_token}, timeout=(5, 30))
            response.raise_for_status()
            data = response.json()
            items = [{"id": item.get("id"), "filename": item.get("mediaFile", {}).get("filename"),
                      "mimeType": item.get("mediaFile", {}).get("mimeType")}
                     for item in data.get("mediaItems", [])]
            if query_filter:
                items = [item for item in items if query_filter.casefold() in (item["filename"] or "").casefold()]
            return {"status": "success", "items": items, "session_id": session_id,
                    "nextPageToken": data.get("nextPageToken", ""), "source": "photos_picker",
                    "filter_scope": "selected_items_current_page_filename"}
    except Exception as exc:
        return google_error(exc, "Photos_API_Error")


def get_map_directions(origin: str, destination: str) -> dict[str, Any]:
    """Call Google Maps Routes API for a driving route, never return a stub success."""
    try:
        key = os.getenv("GOOGLE_MAPS_API_KEY")
        if not key:
            return {"status": "error", "error_type": "Configuration_Error",
                    "error": "GOOGLE_MAPS_API_KEYが未設定です。Routes APIの有効化も必要です。"}
        if not origin.strip() or not destination.strip():
            raise ValueError("origin and destination are required")
        response = requests.post("https://routes.googleapis.com/directions/v2:computeRoutes",
                                 headers={"X-Goog-Api-Key": key,
                                          "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline"},
                                 json={"origin": {"address": origin}, "destination": {"address": destination},
                                       "travelMode": "DRIVE", "languageCode": "ja", "units": "METRIC"},
                                 timeout=(5, 30))
        response.raise_for_status()
        routes = response.json().get("routes", [])
        if not routes:
            return {"status": "error", "error_type": "Route_Not_Found", "error": "経路が見つかりません。"}
        return {"status": "success", "origin": origin, "destination": destination, "routes": routes}
    except Exception as exc:
        return google_error(exc, "Maps_API_Error")

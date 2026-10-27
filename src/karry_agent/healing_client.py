"""Authenticated outbound Karry client with a single verified, read-only recovery."""
from __future__ import annotations

import os
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from .auth_token import normalize_token
from .self_healing import SelfHealingEngine, healing_engine


class HealingAPIClient:
    def __init__(self, base_url: str = "https://karry-agent-1.onrender.com", *,
                 credential_provider: Callable[[], str] | None = None,
                 engine: SelfHealingEngine | None = None,
                 transport: Callable[..., Any] = requests.request) -> None:
        # Never attach the owner credential to user/model-provided origins.
        if base_url.rstrip("/") != "https://karry-agent-1.onrender.com":
            raise ValueError("unapproved API origin")
        self.base_url = base_url.rstrip("/")
        self.credential_provider = credential_provider or (lambda: normalize_token(os.getenv("KARRY_API_TOKEN")))
        self.engine = engine or healing_engine
        self.transport = transport

    def request(self, method: str, path: str, *, json: dict | None = None,
                headers: dict[str, str] | None = None) -> dict[str, Any]:
        parts = urlsplit(path)
        if not path.startswith("/") or path.startswith("//") or parts.scheme or parts.netloc or "?" in path or "#" in path:
            raise ValueError("path must be a same-origin API path without query or fragment")
        method = method.upper()
        if method not in {"GET", "POST"}:
            raise ValueError("only GET and POST are supported")
        token = self.credential_provider()
        if not isinstance(token, str) or not token.strip():
            return {"status": "error", "error": "API credential is not configured"}
        outgoing = {"Authorization": "Bearer " + token.strip(), "Content-Type": "application/json"}
        outgoing.update(headers or {})
        # A caller-supplied header may use lower-case spelling; remove duplicates.
        auth_keys = [key for key in outgoing if key.lower() == "authorization"]
        if len(auth_keys) > 1:
            selected = auth_keys[-1]
            outgoing = {k: v for k, v in outgoing.items() if k.lower() != "authorization" or k == selected}
        context = {"source": "outbound_http", "direction": "outbound", "method": method, "path": path}
        try:
            response = self.transport(method, self.base_url + path, headers=outgoing, json=json,
                                      timeout=(5, 20), allow_redirects=False)
        except Exception as exc:
            healing = self.engine.analyze_and_heal(exc, context)
            return {"status": "error", "error": "outbound transport failed", "healing_status": healing}
        if response.status_code == 401 and method == "GET":
            recovered = []

            def verify(corrected: dict[str, str]) -> bool:
                retry = self.transport(method, self.base_url + path, headers=corrected, json=json,
                                       timeout=(5, 20), allow_redirects=False)
                recovered.append(retry)
                return 200 <= retry.status_code < 300

            error = requests.HTTPError("Invalid bearer token", response=response)
            healing = self.engine.analyze_and_heal(error, {**context, "headers": outgoing,
                        "credential_provider": self.credential_provider, "verify": verify})
            if healing.get("status") == "repaired" and recovered:
                response = recovered[0]
            else:
                return {"status": "error", "http_status": response.status_code,
                        "error": "authentication failed", "healing_status": healing}
        elif not 200 <= response.status_code < 300:
            healing = self.engine.analyze_and_heal(requests.HTTPError("outbound API error", response=response), context)
            return {"status": "error", "http_status": response.status_code,
                    "error": "outbound API request failed", "healing_status": healing}
        else:
            healing = None
        try:
            data = response.json()
        except Exception as exc:
            return {"status": "error", "error": "invalid JSON response",
                    "healing_status": self.engine.analyze_and_heal(exc, context)}
        result = {"status": "success", "http_status": response.status_code, "data": data}
        if isinstance(data, dict) and isinstance(data.get("result"), dict):
            inner = data["result"]
            if inner.get("error") or inner.get("status") == "error":
                result["status"] = "error"
                result["healing_status"] = self.engine.observe_result(inner, context)
        if healing is not None:
            result["healing_status"] = healing
        return result

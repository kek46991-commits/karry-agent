"""Async hand-off to field agents and signed completion callbacks."""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from .config import AgentEndpoint, Settings
from .store import Store, utcnow

TERMINAL = {"completed", "failed", "cancelled"}


def callback_token(settings: Settings, job_id: str) -> str:
    return hmac.new(settings.callback_secret.encode(), job_id.encode(), hashlib.sha256).hexdigest()


def callback_url(settings: Settings, base_url: str) -> str:
    return f"{settings.public_url or base_url.rstrip('/')}/webhook/callback"


async def send(settings: Settings, store: Store, agent: AgentEndpoint, job: dict[str, Any],
               payload: dict[str, Any], base_url: str,
               transport: httpx.AsyncBaseTransport | None = None) -> None:
    body = {"job_id": job["id"], "action": job["action"], "payload": payload,
            "callback_url": callback_url(settings, base_url),
            "callback_token": callback_token(settings, job["id"])}
    headers = {"Authorization": f"Bearer {agent.token}"} if agent.token else {}
    status, error, ack = "failed", None, None
    try:
        async with httpx.AsyncClient(timeout=settings.dispatch_timeout, transport=transport) as client:
            response = await client.post(agent.url, json=body, headers=headers)
        if response.is_success:
            status = "dispatched"
            try:
                ack = response.json()
            except ValueError:
                ack = None
        else:
            error = f"agent returned HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: could not reach agent"
    with store.transaction() as doc:
        current = doc["jobs"].get(job["id"])
        if current is None or current["status"] in TERMINAL:
            return
        current.update(status=status, updated_at=utcnow(), error=error)
        if isinstance(ack, dict):
            current["ack"] = {k: ack[k] for k in list(ack)[:20]}
            if ack.get("status") in TERMINAL:
                current["status"] = ack["status"]
                current["result"] = ack.get("result")

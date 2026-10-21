"""Low-cost OpenAI-compatible providers (Groq / DeepSeek) for intermediate tool steps.

Gemini keeps the first planning step and the final answer; repetitive in-between
"which tool next?" decisions can be routed here to save Gemini quota.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import requests

from .secret_masking import mask_secrets

logger = logging.getLogger(__name__)

PROVIDERS: dict[str, dict[str, str]] = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY", "model": "llama-3.3-70b-versatile"},
    "deepseek": {"base_url": "https://api.deepseek.com", "key_env": "DEEPSEEK_API_KEY", "model": "deepseek-chat"},
}
_PLACEHOLDERS = {"", "YOUR_GROQ_API_KEY", "YOUR_DEEPSEEK_API_KEY"}


@dataclass
class LightDecision:
    """A tool-step decision: either tool calls or a signal that the task is ready to answer."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    done: bool = False
    tokens: int = 0


class LightProvider:
    def __init__(self, name: str, api_key: str, model: str | None = None, base_url: str | None = None,
                 timeout: float = 30.0, session: Any | None = None) -> None:
        if name not in PROVIDERS:
            raise ValueError(f"unknown light provider: {name}")
        self.name = name
        self.api_key = api_key
        self.model = model or PROVIDERS[name]["model"]
        self.base_url = (base_url or PROVIDERS[name]["base_url"]).rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    @classmethod
    def from_env(cls) -> "LightProvider | None":
        name = os.getenv("KARRY_LIGHT_PROVIDER", "").strip().lower()
        if not name or name in {"off", "none", "gemini"}:
            return None
        if name not in PROVIDERS:
            logger.warning("Ignoring unknown KARRY_LIGHT_PROVIDER")
            return None
        key = os.getenv(PROVIDERS[name]["key_env"], "").strip()
        if key in _PLACEHOLDERS:
            logger.warning("KARRY_LIGHT_PROVIDER=%s is set but %s is empty; using Gemini only", name, PROVIDERS[name]["key_env"])
            return None
        return cls(name, key, model=os.getenv("KARRY_LIGHT_MODEL") or None)

    @staticmethod
    def tool_schemas(declarations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {
            "name": d["name"], "description": d.get("description", ""),
            "parameters": d.get("parameters") or {"type": "object", "properties": {}},
        }} for d in declarations]

    def decide(self, system: str, user: str, declarations: list[dict[str, Any]]) -> LightDecision | None:
        """Return the next tool calls, ``done=True`` when no tool is needed, or None on any failure."""
        known = {d["name"] for d in declarations}
        body = {
            "model": self.model, "temperature": 0.1, "max_tokens": 1024, "tool_choice": "auto",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": mask_secrets(user)}],
            "tools": self.tool_schemas(declarations),
        }
        try:
            response = self.session.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout,
                                         headers={"Authorization": f"Bearer {self.api_key}"})
            response.raise_for_status()
            data = response.json()
            message = data["choices"][0]["message"]
            tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
            calls = []
            for call in message.get("tool_calls") or []:
                name = call["function"]["name"]
                args = json.loads(call["function"].get("arguments") or "{}")
                if name not in known or not isinstance(args, dict):
                    return None
                calls.append((name, args))
            return LightDecision(calls=calls, done=not calls, tokens=tokens)
        except Exception as exc:  # noqa: BLE001 - any provider problem falls back to Gemini
            logger.warning("Light provider %s failed: %s", self.name, type(exc).__name__)
            return None

"""Environment-driven settings, read once per app instance."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

_AGENT_URL = re.compile(r"^JIMMY_AGENT_([A-Z0-9_]+)_URL$")
KILLSWITCH_AGENT = "killswitch"


@dataclass(frozen=True)
class AgentEndpoint:
    name: str
    url: str
    token: str = field(repr=False, default="")

    def public(self) -> dict[str, object]:
        return {"name": self.name, "host": urlparse(self.url).netloc, "has_token": bool(self.token)}


@dataclass(frozen=True)
class Settings:
    api_token: str = field(repr=False, default="")
    callback_secret: str = field(repr=False, default="")
    database_url: str = field(repr=False, default="")
    data_dir: Path = Path("data")
    public_url: str = ""
    agents: dict[str, AgentEndpoint] = field(default_factory=dict)
    dispatch_timeout: float = 15.0
    gemini_api_key: str = field(repr=False, default="")
    gemini_model: str = "gemini-3.5-flash"
    llm_timeout: float = 8.0

    @property
    def storage_backend(self) -> str:
        return "postgres" if self.database_url else "file"


def _agent_url_ok(url: str, allow_http: bool) -> bool:
    parsed = urlparse(url)
    return bool(parsed.netloc) and (parsed.scheme == "https" or (allow_http and parsed.scheme == "http"))


def load_agents(env: dict[str, str]) -> dict[str, AgentEndpoint]:
    allow_http = env.get("JIMMY_CORE_ALLOW_HTTP_AGENTS") == "1"
    agents: dict[str, AgentEndpoint] = {}
    for key, value in env.items():
        match = _AGENT_URL.match(key)
        if not match or not value.strip():
            continue
        url = value.strip()
        if not _agent_url_ok(url, allow_http):
            raise ValueError(f"{key} must be an https:// URL")
        raw = match.group(1)
        agents[raw.lower()] = AgentEndpoint(raw.lower(), url, env.get(f"JIMMY_AGENT_{raw}_TOKEN", "").strip())
    return agents


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    api_token = env.get("JIMMY_CORE_API_TOKEN", "").strip()
    return Settings(
        api_token=api_token,
        callback_secret=env.get("JIMMY_CORE_CALLBACK_SECRET", "").strip() or api_token,
        database_url=env.get("JIMMY_CORE_DATABASE_URL", "").strip(),
        data_dir=Path(env.get("JIMMY_CORE_DATA_DIR", "data")),
        public_url=(env.get("JIMMY_CORE_PUBLIC_URL") or env.get("RENDER_EXTERNAL_URL") or "").rstrip("/"),
        agents=load_agents(env),
        dispatch_timeout=float(env.get("JIMMY_CORE_DISPATCH_TIMEOUT", "15")),
        gemini_api_key=env.get("GEMINI_API_KEY", "").strip(),
        gemini_model=env.get("GEMINI_MODEL", "gemini-3.5-flash"),
        llm_timeout=float(env.get("JIMMY_CORE_LLM_TIMEOUT", "8")),
    )

"""Guarded GitHub/Render operations for isolated sandbox clones.

This module never copies the running service's secrets into a clone. Branch creation
is explicit, and Render provisioning is a separate opt-in operation.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import requests

GITHUB_API = "https://api.github.com"
RENDER_API = "https://api.render.com/v1"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,79}$")
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?$")


class CloneConfigurationError(RuntimeError):
    """Required clone integration settings are missing or unsafe."""


class CloneProviderError(RuntimeError):
    """A provider rejected a request; provider response bodies are intentionally not exposed."""

    def __init__(self, provider: str, status_code: int, code: str):
        super().__init__(code)
        self.provider = provider
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class CloneSettings:
    repository: str
    github_token: str = field(repr=False)
    base_branch: str = "main"
    source_sha: str = ""
    render_api_key: str = field(default="", repr=False)
    render_owner_id: str = ""
    render_region: str = "oregon"
    render_plan: str = "free"
    service_prefix: str = "karry-agent-sandbox"

    @classmethod
    def from_env(cls) -> "CloneSettings":
        repository = (os.getenv("KARRY_CLONE_REPOSITORY") or os.getenv("RENDER_GIT_REPO_SLUG", "")).strip()
        github_token = os.getenv("KARRY_CLONE_GITHUB_TOKEN", "").strip()
        base_branch = (os.getenv("KARRY_CLONE_BASE_BRANCH") or os.getenv("RENDER_GIT_BRANCH") or "main").strip()
        source_sha = (os.getenv("KARRY_CLONE_SOURCE_SHA") or os.getenv("RENDER_GIT_COMMIT") or "").strip()
        render_plan = os.getenv("KARRY_CLONE_RENDER_PLAN", "free").strip()
        render_region = os.getenv("KARRY_CLONE_RENDER_REGION", "oregon").strip()
        service_prefix = os.getenv("KARRY_CLONE_SERVICE_PREFIX", "karry-agent-sandbox").strip()
        if not repository or not _REPO_RE.fullmatch(repository):
            raise CloneConfigurationError("KARRY_CLONE_REPOSITORY must be owner/repository")
        if not github_token:
            raise CloneConfigurationError("KARRY_CLONE_GITHUB_TOKEN is not configured")
        if not _BRANCH_RE.fullmatch(base_branch) or ".." in base_branch or base_branch.startswith("/"):
            raise CloneConfigurationError("KARRY_CLONE_BASE_BRANCH is invalid")
        if source_sha and not _SHA_RE.fullmatch(source_sha):
            raise CloneConfigurationError("KARRY_CLONE_SOURCE_SHA is invalid")
        if render_plan != "free":
            raise CloneConfigurationError("KARRY_CLONE_RENDER_PLAN must be free")
        if not re.fullmatch(r"[a-z0-9-]{3,24}", service_prefix):
            raise CloneConfigurationError("KARRY_CLONE_SERVICE_PREFIX is invalid")
        return cls(
            repository=repository,
            github_token=github_token,
            base_branch=base_branch,
            source_sha=source_sha,
            render_api_key=os.getenv("KARRY_CLONE_RENDER_API_KEY", "").strip(),
            render_owner_id=os.getenv("KARRY_CLONE_RENDER_OWNER_ID", "").strip(),
            render_region=render_region,
            render_plan=render_plan,
            service_prefix=service_prefix,
        )

    def require_render(self) -> None:
        if not self.render_api_key or not self.render_owner_id:
            raise CloneConfigurationError("Render provisioning is not configured")


@dataclass(frozen=True)
class CreatedBranch:
    repository: str
    branch: str
    sha: str

    @property
    def url(self) -> str:
        owner, repo = self.repository.split("/", 1)
        return f"https://github.com/{owner}/{repo}/tree/{quote(self.branch, safe='/')}"


class CloneService:
    def __init__(self, settings: CloneSettings, session: requests.Session | None = None,
                 timeout: float = 15.0):
        self.settings = settings
        self.session = session or requests.Session()
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "CloneService":
        return cls(CloneSettings.from_env())

    def _github_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.settings.github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def create_branch(self, slug: str, base_branch: str | None = None) -> CreatedBranch:
        if not _SLUG_RE.fullmatch(slug):
            raise ValueError("branch slug must be a lowercase alphanumeric slug")
        owner, repo = self.settings.repository.split("/", 1)
        source = base_branch or self.settings.base_branch
        if not _BRANCH_RE.fullmatch(source) or ".." in source or source.startswith("/"):
            raise ValueError("base branch is invalid")
        branch = f"sandbox/{slug}"
        sha = self.settings.source_sha
        if not sha:
            ref_path = quote(f"heads/{source}", safe="/")
            base_url = f"{GITHUB_API}/repos/{quote(owner)}/{quote(repo)}/git/ref/{ref_path}"
            try:
                response = self.session.get(base_url, headers=self._github_headers(), timeout=self.timeout)
            except requests.RequestException as exc:
                raise CloneProviderError("github", 502, "github_unreachable") from exc
            if response.status_code != 200:
                code = "source_branch_not_found" if response.status_code == 404 else "github_ref_read_failed"
                raise CloneProviderError("github", response.status_code, code)
            try:
                sha = response.json()["object"]["sha"]
            except (ValueError, KeyError, TypeError) as exc:
                raise CloneProviderError("github", 502, "github_invalid_response") from exc
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            raise CloneProviderError("github", 502, "github_invalid_response")

        create_url = f"{GITHUB_API}/repos/{quote(owner)}/{quote(repo)}/git/refs"
        body = {"ref": f"refs/heads/{branch}", "sha": sha}
        try:
            response = self.session.post(create_url, headers=self._github_headers(), json=body,
                                         timeout=self.timeout)
        except requests.RequestException as exc:
            raise CloneProviderError("github", 502, "github_unreachable") from exc
        if response.status_code == 409:
            raise CloneProviderError("github", 409, "branch_already_exists")
        if response.status_code != 201:
            code = "branch_already_exists" if response.status_code == 422 else "github_branch_create_failed"
            status = 409 if response.status_code == 422 else response.status_code
            raise CloneProviderError("github", status, code)
        return CreatedBranch(self.settings.repository, branch, sha)

    def create_render_service(self, branch: str, slug: str) -> dict[str, str | None]:
        self.settings.require_render()
        if self.settings.render_plan != "free":
            raise CloneConfigurationError("Only the free Render plan is allowed for sandbox clones")
        if not _SLUG_RE.fullmatch(slug) or not branch.startswith("sandbox/"):
            raise ValueError("invalid sandbox branch")
        owner, repo = self.settings.repository.split("/", 1)
        service_name = f"{self.settings.service_prefix}-{slug}"[:50]
        payload = {
            "type": "web_service",
            "name": service_name,
            "ownerId": self.settings.render_owner_id,
            "repo": f"https://github.com/{owner}/{repo}",
            "branch": branch,
            "autoDeployTrigger": "checksPass",
            "serviceDetails": {
                "runtime": "python",
                "plan": self.settings.render_plan,
                "region": self.settings.render_region,
                "buildCommand": "pip install -r requirements.txt",
                "startCommand": "uvicorn app:app --host 0.0.0.0 --port $PORT",
            },
        }
        try:
            response = self.session.post(
                f"{RENDER_API}/services",
                headers={"Authorization": f"Bearer {self.settings.render_api_key}",
                         "Accept": "application/json", "Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise CloneProviderError("render", 502, "render_unreachable") from exc
        if response.status_code != 201:
            code = "render_service_conflict" if response.status_code == 409 else "render_service_create_failed"
            status = 409 if response.status_code == 409 else response.status_code
            raise CloneProviderError("render", status, code)
        try:
            result = response.json()
            service = result.get("service", result)
        except (ValueError, AttributeError) as exc:
            raise CloneProviderError("render", 502, "render_invalid_response") from exc
        if not isinstance(service, dict) or not service.get("id"):
            raise CloneProviderError("render", 502, "render_invalid_response")
        return {
            "id": str(service["id"]),
            "name": str(service.get("name") or service_name),
            "dashboard_url": service.get("dashboardUrl") if isinstance(service.get("dashboardUrl"), str) else None,
            "deploy_id": result.get("deployId") if isinstance(result.get("deployId"), str) else None,
        }

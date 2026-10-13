"""Compatibility facade for the supplied seven-tool agent core.

Default command/file policies remain those of the existing backend. Arbitrary
shell execution is opt-in and needs an isolated single-owner deployment.
"""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from . import tools as existing
from .google_workspace import get_google_credentials, get_map_directions, search_photos


def _normalize(result: dict[str, Any]) -> dict[str, Any]:
    status = "error" if result.get("error") else "success"
    return {"status": status, **result}


def execute_command(command: str) -> dict[str, Any]:
    """Execute a command under the existing policy; explicit shell opt-in for isolated deployments."""
    try:
        if os.getenv("KARRY_ALLOW_SHELL") != "1":
            result = existing.run_command(command, timeout=30)
            result = _normalize(result)
            if "returncode" in result:
                result["exit_code"] = result["returncode"]
                result["status"] = "success" if result["returncode"] == 0 else "error"
            result["execution_mode"] = "existing_allowlist"
            return result
        if not os.getenv("KARRY_API_TOKEN"):
            return {"status": "error", "error": "任意シェル実行にはKARRY_API_TOKENと隔離環境が必要です。"}
        root = Path(os.getenv("KARRY_WORKSPACE", ".")).resolve()
        root.mkdir(parents=True, exist_ok=True)
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "TZ"}}
        env["HOME"] = str(root)
        with subprocess.Popen(command, shell=True, cwd=root, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
            try:
                stdout, stderr = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                return {"status": "error", "error_type": "Command_Timeout", "error": "コマンドが30秒でタイムアウトしました。"}
        return {"status": "success" if process.returncode == 0 else "error",
                "stdout": stdout[-20_000:], "stderr": stderr[-20_000:],
                "exit_code": process.returncode, "execution_mode": "isolated_shell"}
    except Exception as exc:
        return {"status": "error", "error": type(exc).__name__, "error_type": "Execution_Error"}


def read_file(filepath: str | None = None, *, path: str | None = None) -> dict[str, Any]:
    """Read a workspace file. Both supplied filepath and existing path are accepted."""
    try:
        if filepath is not None and path is not None and filepath != path:
            raise ValueError("filepath and path conflict")
        if filepath is None and path is None:
            raise ValueError("filepath or path is required")
        return _normalize(existing.read_file(filepath if filepath is not None else path))
    except Exception as exc:
        return {"status": "error", "error": type(exc).__name__, "error_type": "Execution_Error"}


def write_file(filepath: str | None = None, content: str = "", *, path: str | None = None) -> dict[str, Any]:
    """Create a workspace file without removing the existing overwrite protection."""
    try:
        if filepath is not None and path is not None and filepath != path:
            raise ValueError("filepath and path conflict")
        if filepath is None and path is None:
            raise ValueError("filepath or path is required")
        target = filepath if filepath is not None else path
        return {**_normalize(existing.write_file(target, content)), "filepath": target}
    except Exception as exc:
        return {"status": "error", "error": type(exc).__name__, "error_type": "Execution_Error"}


def list_dir(dirpath: str = ".") -> dict[str, Any]:
    """List immediate workspace children, preserving hidden/secret file exclusion."""
    try:
        target = existing._safe_path(dirpath)
        items = sorted(item.name for item in target.iterdir() if not item.name.startswith(".")
                       and item.name not in {"token.json", "credentials.json"}
                       and not item.name.startswith("client_secret"))
        return {"status": "success", "items": items[:500], "truncated": len(items) > 500}
    except Exception as exc:
        return {"status": "error", "error": type(exc).__name__, "error_type": "Execution_Error"}


def add_calendar_event(summary: str, start_time: str, end_time: str | None = None,
                       description: str = "", *, duration_minutes: int = 60,
                       confirmed: bool = False) -> dict[str, Any]:
    """Use the existing preview/confirmation flow with supplied end_time and description."""
    return _normalize(existing.add_calendar_event(summary, start_time, duration_minutes, confirmed,
                                                   end_time=end_time, description=description))


AGENT_TOOLS = [execute_command, read_file, write_file, list_dir,
               add_calendar_event, search_photos, get_map_directions]


def dispatch_tool(tool_name: str, arguments: dict) -> dict:
    """Use the same registry and validation as CLI, AgentCore and the API."""
    return existing.build_default_registry().execute(tool_name, arguments)

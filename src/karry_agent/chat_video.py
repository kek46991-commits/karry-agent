"""Chat-native video generation: a plain chat request such as 「海辺の動画を作って」 starts autopilot.

Experimental: only active when ``KARRY_EXPERIMENTAL_FEATURES=1`` (see feature_flags).
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from . import video_suite as vs
from .core import AgentMetrics, AgentResult, ExecutionEvent

ACTION = "start_video_generation"
_VIDEO = re.compile(r"動画|ムービー|ショート映像|映像|\b(?:video|movie|clip)s?\b", re.IGNORECASE)
_CREATE = re.compile(r"作っ|作る|作成|作れ|つくっ|つくる|生成|して(?:ください|下さい|ほしい|欲しい)|にして|"
                     r"\b(?:make|create|generate|produce|render)\b", re.IGNORECASE)
_NOT_A_REQUEST = re.compile(r"作り方|やり方|方法|とは|どうやって|できますか|ですか|\bhow\s+(?:to|do|can)\b|\bwhat\s+is\b",
                            re.IGNORECASE)

StartAutopilot = Callable[[str, str, bytes | None, str | None], tuple[dict[str, Any], int]]


def is_video_request(text: str) -> bool:
    text = (text or "").strip()
    return bool(text) and bool(_VIDEO.search(text)) and bool(_CREATE.search(text)) and not _NOT_A_REQUEST.search(text)


def job_marker(job_id: str) -> str:
    return f"動画ジョブID: {job_id}"


def run(start: StartAutopilot, session_id: str, prompt: str, images: list[dict[str, Any]] | None = None,
        request_id: str | None = None) -> AgentResult:
    started = time.perf_counter()
    image = next((item.get("data") for item in images or [] if isinstance(item.get("data"), bytes)), None)
    arguments = {"source_text": prompt[:200], "with_image": bool(image)}
    try:
        job, _ = start(session_id, prompt[:12_000], image, request_id)
        observation = {"status": "processing", "job_id": job["id"], "session_id": session_id}
        text = ("動画の作成を始めました。脚本・ナレーション・映像を自動で組み立てています（数分かかります）。"
                f"完成するとこのチャットで再生できます。\n{job_marker(job['id'])}")
        status, errors = "completed", 0
    except vs.VideoError as exc:
        observation = {"status": "error", "code": exc.code, "error": str(exc)}
        text, status, errors = f"動画を作成できませんでした: {exc}", "failed", 1
    except Exception:  # noqa: BLE001 - a video outage must not fail the chat turn
        observation = {"status": "error", "code": "autopilot_unavailable"}
        text, status, errors = "動画を作成できませんでした。時間をおいてもう一度お試しください。", "failed", 1
    elapsed = (time.perf_counter() - started) * 1000
    event = ExecutionEvent(1, ACTION, arguments, observation, elapsed)
    return AgentResult(text, 1, 1, [event], AgentMetrics(tool_calls=1, errors=errors, elapsed_ms=elapsed), status=status)

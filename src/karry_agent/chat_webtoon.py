"""Chat-native webtoon: 「この小説をマンガにして」 on the first line, the story below, starts the webtoon pipeline.

Active when ``KARRY_WEBTOON=1`` or ``KARRY_EXPERIMENTAL_FEATURES=1`` (see feature_flags).
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from . import chat_video
from .core import AgentMetrics, AgentResult, ExecutionEvent
from .webtoon.api import WebtoonBusy

ACTION = "start_webtoon_generation"
DEFAULT_PANELS, MAX_PANELS, MAX_STORY_CHARS = 6, 12, 12_000
_COMIC = re.compile(r"マンガ|漫画|まんが|コミック|ウェブトゥーン|\b(?:webtoon|comic|manga)s?\b", re.IGNORECASE)
_CREATE = re.compile(r"作っ|作る|作成|作れ|つくっ|つくる|生成|描い|描く|描け|に(?:して|する|変換)|化して|"
                     r"\b(?:make|create|generate|draw|turn|convert)\b", re.IGNORECASE)
_NOT_A_REQUEST = re.compile(r"作り方|描き方|やり方|方法|とは|どうやって|できますか|ですか|おすすめ|\bhow\s+(?:to|do|can)\b|\bwhat\s+is\b",
                            re.IGNORECASE)
_PANELS = re.compile(r"(\d{1,2})\s*(?:コマ|こま|panels?)", re.IGNORECASE)

StartJob = Callable[[str, int], dict[str, Any]]


def split_request(text: str) -> tuple[str, str]:
    instruction, _, rest = (text or "").strip().partition("\n")
    return instruction.strip(), rest.strip()


def is_webtoon_request(text: str) -> bool:
    instruction, _ = split_request(text)
    return (bool(_COMIC.search(instruction)) and bool(_CREATE.search(instruction))
            and not _NOT_A_REQUEST.search(instruction) and not chat_video.is_video_request(instruction))


def requested_panels(instruction: str) -> int:
    match = _PANELS.search(instruction)
    return min(max(int(match.group(1)), 1), MAX_PANELS) if match else DEFAULT_PANELS


def job_marker(job_id: str) -> str:
    return f"マンガジョブID: {job_id}"


def run(start: StartJob, prompt: str) -> AgentResult:
    started = time.perf_counter()
    instruction, story = split_request(prompt)
    story = story or instruction
    panels = requested_panels(instruction)
    arguments = {"story_chars": len(story), "max_panels": panels}
    if len(story) > MAX_STORY_CHARS:
        observation = {"status": "error", "code": "story_too_long"}
        text, status, errors = f"小説が長すぎます。本文は {MAX_STORY_CHARS:,} 文字以内にしてください（今は {len(story):,} 文字）。", "failed", 1
    else:
        try:
            job = start(story, panels)
            observation = {"status": "processing", "job_id": job["job_id"], "max_panels": panels}
            text = (f"マンガの作成を始めました。登場人物を読み取り、{panels}コマ以内の縦読みマンガ（9:16）を描いています（数分かかります）。"
                    "完成するとこのチャットにコマ画像と ZIP の保存ボタンが表示されます。画像はサーバーの再起動で消えるので、ZIP で保存してください。"
                    f"\n{job_marker(job['job_id'])}")
            status, errors = "completed", 0
        except WebtoonBusy as exc:
            observation = {"status": "error", "code": "webtoon_busy"}
            text, status, errors = f"マンガを作成できませんでした: {exc}", "failed", 1
        except Exception:  # noqa: BLE001 - a webtoon outage must not fail the chat turn
            observation = {"status": "error", "code": "webtoon_unavailable"}
            text, status, errors = "マンガを作成できませんでした。時間をおいてもう一度お試しください。", "failed", 1
    elapsed = (time.perf_counter() - started) * 1000
    event = ExecutionEvent(1, ACTION, arguments, observation, elapsed)
    return AgentResult(text, 1, 1, [event], AgentMetrics(tool_calls=1, errors=errors, elapsed_ms=elapsed), status=status)

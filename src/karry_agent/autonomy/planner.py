"""Split one instruction into subtasks that each fit a token budget, and tag how each should be routed."""
from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from typing import Any

from .. import chat_video
from .state import Subtask

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_TOKENS = 2000
DEFAULT_MAX_SUBTASKS = 30
GOAL_CHARS = 300
KINDS = ("general", "research", "image", "video")

Planner = Callable[[str, int], list[dict[str, Any]]]

_ITEM = re.compile(r"^\s*(?:[-*・•●◆■]|\d{1,3}[.)．、]|[①-⑳]|\(\d{1,3}\)|（\d{1,3}）)\s*(\S.*)$")
_SENTENCE = re.compile(r"(?<=[。！？!?])|(?<=\.)\s+")
_FOLLOW_UP = re.compile(r"^(?:それ|その|これら?|上記|前の|前述|then\b|after that\b|using (?:that|the above)\b)|(?:をもとに|を元に|を使って|の結果を|を踏まえて)", re.IGNORECASE)
_IMAGE = re.compile(r"(?:画像|イラスト|絵|写真|アイコン|ロゴ|\b(?:image|picture|illustration|icon|logo)s?\b)", re.IGNORECASE)
_IMAGE_ACTION = re.compile(r"(?:生成|作成|作って|作る|描|デザイン|\b(?:generate|draw|create|make|design)\b)", re.IGNORECASE)
_RESEARCH = re.compile(r"(?:調べ|調査|検索|リサーチ|比較|要約|https?://|\b(?:search|research|look up|summari[sz]e|compare)\b)", re.IGNORECASE)


class PlanError(ValueError):
    pass


def estimate_tokens(text: str) -> int:
    """Conservative estimate without a tokenizer: ~4 ASCII chars per token, ~1 token per CJK/other char."""
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return math.ceil(ascii_chars / 4) + (len(text) - ascii_chars)


def classify(text: str) -> str:
    if chat_video.is_video_request(text):
        return "video"
    if _IMAGE.search(text) and _IMAGE_ACTION.search(text):
        return "image"
    if _RESEARCH.search(text):
        return "research"
    return "general"


def _hard_split(text: str, budget: int) -> list[str]:
    chunks, current = [], ""
    for ch in text:
        if current and estimate_tokens(current + ch) > budget:
            chunks.append(current)
            current = ""
        current += ch
    return chunks + ([current] if current else [])


def _fit(text: str, budget: int) -> list[str]:
    """Pack whole sentences into chunks under the budget; only cut inside a sentence when it alone is too long."""
    if estimate_tokens(text) <= budget:
        return [text]
    chunks: list[str] = []
    current = ""
    for sentence in (s.strip() for s in _SENTENCE.split(text)):
        if not sentence:
            continue
        candidate = f"{current} {sentence}".strip() if current else sentence
        if estimate_tokens(candidate) <= budget:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if estimate_tokens(sentence) <= budget:
            current = sentence
        else:
            *head, current = _hard_split(sentence, budget)
            chunks.extend(head)
    return chunks + ([current] if current else [])


def _pieces(instruction: str, budget: int) -> tuple[str, list[str]]:
    lines = instruction.splitlines()
    items: list[str] = []
    preamble: list[str] = []
    for line in lines:
        match = _ITEM.match(line)
        if match:
            items.append(match.group(1).strip())
        elif items and line.strip() and line[:1].isspace():
            items[-1] += " " + line.strip()
        elif line.strip():
            preamble.append(line.strip())
    if len(items) >= 2:
        return " ".join(preamble), items
    if estimate_tokens(instruction) <= budget:
        return "", [instruction.strip()]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", instruction) if p.strip()]
    return "", paragraphs if len(paragraphs) >= 2 else [instruction.strip()]


def _goal(instruction: str, preamble: str) -> str:
    text = preamble or next((s.strip() for s in _SENTENCE.split(instruction) if s.strip()), instruction.strip())
    return text[:GOAL_CHARS]


def _from_planner(planner: Planner, instruction: str, budget: int, max_subtasks: int) -> list[Subtask] | None:
    try:
        raw = planner(instruction, budget)
        if not isinstance(raw, list) or not 0 < len(raw) <= max_subtasks:
            return None
        tasks: list[Subtask] = []
        for index, item in enumerate(raw):
            prompt = str(item.get("prompt", "")).strip()
            deps = item.get("depends_on", [])
            if not prompt or estimate_tokens(prompt) > budget or not isinstance(deps, list):
                return None
            if any(not isinstance(d, int) or not 0 <= d < index for d in deps):
                return None
            kind = item.get("kind") if item.get("kind") in KINDS else classify(prompt)
            tasks.append(Subtask(f"t{index + 1}", prompt, kind, [f"t{d + 1}" for d in deps], tokens=estimate_tokens(prompt)))
        return tasks
    except Exception as exc:  # noqa: BLE001 - a planner failure falls back to the deterministic splitter
        logger.warning("autonomy planner failed: %s", type(exc).__name__)
        return None


def plan_subtasks(instruction: str, *, budget_tokens: int = DEFAULT_BUDGET_TOKENS,
                  max_subtasks: int = DEFAULT_MAX_SUBTASKS, planner: Planner | None = None) -> tuple[str, list[Subtask]]:
    """Return ``(goal, subtasks)``. Dependencies only point backwards, so the graph is always acyclic."""
    instruction = (instruction or "").strip()
    if not instruction:
        raise PlanError("指示が空です。")
    if budget_tokens < 50:
        raise PlanError("budget_tokens は 50 以上にしてください。")
    preamble, pieces = _pieces(instruction, budget_tokens)
    goal = _goal(instruction, preamble)
    if planner is not None:
        planned = _from_planner(planner, instruction, budget_tokens, max_subtasks)
        if planned:
            return goal, planned

    tasks: list[Subtask] = []
    seen: set[str] = set()
    for piece in pieces:
        follows = bool(_FOLLOW_UP.search(piece)) and bool(tasks)
        previous = tasks[-1].id if follows else None
        for chunk in _fit(piece, budget_tokens):
            key = re.sub(r"\s+", " ", chunk).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            task_id = f"t{len(tasks) + 1}"
            tasks.append(Subtask(task_id, chunk, classify(chunk), [previous] if previous else [], tokens=estimate_tokens(chunk)))
    if len(tasks) > max_subtasks:
        raise PlanError(f"サブタスクが {len(tasks)} 件になりました（上限 {max_subtasks} 件）。指示を分けて送ってください。")
    return goal, tasks

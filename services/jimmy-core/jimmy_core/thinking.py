"""Think loop: situation analysis -> risk audit -> next action.

Rules run in-process in microseconds. An optional Gemini pass can refine the
summary and next steps, but it can only raise risk, never lower it.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import KILLSWITCH_AGENT, Settings


@dataclass(frozen=True)
class RiskRule:
    category: str
    weight: int
    reason: str
    keywords: tuple[str, ...]


RISK_RULES: tuple[RiskRule, ...] = (
    RiskRule("money", 3, "お金が動く・課金が発生する可能性", (
        "決済", "支払", "課金", "送金", "請求", "有料", "購入", "stripe", "payment", "charge", "refund", "返金")),
    RiskRule("destructive", 3, "データやコードを消す・上書きする操作", (
        "削除", "消す", "消去", "初期化", "上書き", "delete", "drop table", "rm -rf", "force push", "truncate")),
    RiskRule("privacy", 3, "個人情報・認証情報を扱う", (
        "個人情報", "パスワード", "password", "トークン", "token", "api key", "apiキー", "住所", "電話番号",
        "クレジットカード", "マイナンバー")),
    RiskRule("physical", 3, "現実世界の機器・ロボットを動かす", (
        "ロボット", "optimus", "オプティマス", "遠隔操作", "自動操作", "端末操作", "モーター", "robot")),
    RiskRule("legal", 3, "規約・法律に触れる可能性", (
        "著作権", "規約違反", "偽物", "コピー品", "無断転載", "なりすまし", "copyright")),
    RiskRule("production", 2, "本番環境への反映", (
        "本番", "production", "デプロイ", "deploy", "リリース", "release", "マージ", "merge")),
    RiskRule("external", 2, "外部への送信・公開", (
        "送信", "投稿", "公開", "メール", "一斉配信", "publish", "post to", "tweet", "send email")),
)

ROUTES: dict[str, tuple[str, ...]] = {
    "device": ("iphone", "android", "スマホ", "端末", "画面共有", "replaykit", "遠隔操作", "自動操作", "タップ"),
    "video": ("動画", "video", "リップシンク", "lipsync", "編集", "テロップ", "ナレーション", "ムービー"),
    "shop": ("ショップ", "shop", "商品", "在庫", "注文", "出品", "ec", "stripe", "決済"),
}


def audit(text: str) -> dict[str, Any]:
    lowered = text.lower()
    findings = []
    for rule in RISK_RULES:
        matched = [word for word in rule.keywords if word in lowered]
        if matched:
            findings.append({"category": rule.category, "weight": rule.weight, "reason": rule.reason,
                             "matched": matched[:5]})
    score = sum(item["weight"] for item in findings)
    level = "low" if score == 0 else "medium" if score <= 2 else "high" if score <= 5 else "critical"
    return {"level": level, "score": score, "findings": findings}


def route(text: str) -> str | None:
    lowered = text.lower()
    hits = {name: sum(word in lowered for word in words) for name, words in ROUTES.items()}
    best = max(hits, key=lambda name: hits[name])
    return best if hits[best] else None


_LEVELS = ("low", "medium", "high", "critical")


def decide(risk: dict[str, Any], agent: str | None, killswitch: bool, configured: set[str]) -> dict[str, Any]:
    if killswitch:
        return {"action": "halt", "agent": KILLSWITCH_AGENT if KILLSWITCH_AGENT in configured else None,
                "reason": "キルスイッチが作動中のため、すべての実行を止めます。",
                "next_steps": ["キルスイッチの解除をけいちゃんに確認する"]}
    if risk["level"] == "critical":
        return {"action": "halt", "agent": agent, "reason": "複数の重大リスクが重なっています。実行前に必ず人の承認が必要です。",
                "next_steps": ["リスク内容をけいちゃんに説明する", "承認が出るまで外部エージェントに指示しない"]}
    if risk["level"] == "high":
        return {"action": "confirm_with_user", "agent": agent,
                "reason": "取り消しにくい操作が含まれるため、実行前に確認します。",
                "next_steps": ["実行内容と影響範囲をけいちゃんに確認する",
                               f"承認後に {agent} エージェントへ dispatch する" if agent else "承認後に実行する"]}
    if agent:
        ready = agent in configured
        return {"action": "dispatch", "agent": agent, "agent_configured": ready,
                "reason": f"{agent} 担当の仕事なので、専用エージェントに任せます。",
                "next_steps": [f"POST /dispatch で {agent} に指示を送る" if ready
                               else f"JIMMY_AGENT_{agent.upper()}_URL を設定して {agent} エージェントを登録する"]}
    return {"action": "proceed", "agent": None, "reason": "大きなリスクは見つかりません。コアだけで進められます。",
            "next_steps": ["決定事項を POST /memory に記録する"]}


def analyze(situation: str, goal: str | None, doc: dict[str, Any]) -> dict[str, Any]:
    tasks = doc["state"]["tasks"].values()
    open_tasks = [task for task in tasks if task.get("status") not in {"done", "cancelled"}]
    decisions = [item for item in doc["memory"] if item["kind"] == "decision"][-5:]
    summary = situation.strip().splitlines()[0][:200]
    return {"summary": summary, "goal": goal, "open_tasks": open_tasks[-10:],
            "recent_decisions": [{"id": d["id"], "content": d["content"][:300]} for d in decisions]}


_PROMPT = (
    "あなたはジミーコア（司令塔AI）です。次の状況を分析し、JSONだけで答えてください。"
    '形式: {"summary": "状況の要約(100字以内)", "risk_level": "low|medium|high|critical", '
    '"next_steps": ["次にやること(最大3つ)"]}\n\n'
)


async def llm_refine(settings: Settings, payload: dict[str, Any],
                     transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent"
    body = {"contents": [{"parts": [{"text": _PROMPT + json.dumps(payload, ensure_ascii=False)}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
    async with httpx.AsyncClient(timeout=settings.llm_timeout, transport=transport) as client:
        response = await client.post(url, json=body, headers={"x-goog-api-key": settings.gemini_api_key})
        response.raise_for_status()
    text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("LLM response is not a JSON object")
    return data


async def think(settings: Settings, doc: dict[str, Any], situation: str, goal: str | None,
                context: dict[str, Any], use_llm: bool,
                transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    text = "\n".join([situation, goal or "", json.dumps(context, ensure_ascii=False) if context else ""])
    analysis = analyze(situation, goal, doc)
    risk = audit(text)
    agent = route(text)
    killswitch = bool(doc["state"].get("killswitch", {}).get("engaged"))
    decision = decide(risk, agent, killswitch, set(settings.agents))
    engine, llm_error = "rules", None
    if use_llm and settings.gemini_api_key:
        try:
            refined = await llm_refine(settings, {"situation": situation, "goal": goal, "context": context,
                                                  "rule_risk": risk["level"]}, transport)
            engine = "rules+gemini"
            if isinstance(refined.get("summary"), str) and refined["summary"].strip():
                analysis["llm_summary"] = refined["summary"][:300]
            steps = refined.get("next_steps")
            if isinstance(steps, list) and steps:
                decision["next_steps"] = [str(step)[:200] for step in steps[:3]] + decision["next_steps"]
            llm_level = refined.get("risk_level")
            if llm_level in _LEVELS and _LEVELS.index(llm_level) > _LEVELS.index(risk["level"]):
                risk["level"] = llm_level
                risk["findings"].append({"category": "llm", "weight": 0, "reason": "LLM がより高いリスクと判断",
                                         "matched": []})
                if llm_level in {"high", "critical"} and decision["action"] in {"proceed", "dispatch"}:
                    decision = decide(risk, agent, killswitch, set(settings.agents))
        except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as exc:
            llm_error = type(exc).__name__
    result = {"analysis": analysis, "risk": risk, "decision": decision, "engine": engine,
              "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
    if llm_error:
        result["llm_error"] = llm_error
    return result

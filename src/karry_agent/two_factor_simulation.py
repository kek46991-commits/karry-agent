"""Synthetic account-takeover drill for demonstrating two-factor defenses."""
from __future__ import annotations

import re
from dataclasses import dataclass

_REQUEST_RE = re.compile(
    r"(?:二段階認証|2FA|MFA).*(?:デモ|シミュレーション|攻撃|検証)|"
    r"(?:デモ|シミュレーション|攻撃|検証).*(?:二段階認証|2FA|MFA)",
    re.IGNORECASE,
)
SYNTHETIC_OTP = "482731"


@dataclass(frozen=True)
class SimulationStep:
    action: str
    outcome: str
    defense: str


def requested(prompt: str) -> bool:
    return bool(_REQUEST_RE.search(prompt))


def run() -> list[SimulationStep]:
    """Return a fixed, non-networked attack/defense sequence using synthetic data."""
    return [
        SimulationStep(
            "漏えいした合成パスワードでログイン",
            "第1要素だけ一致し、2FAチャレンジへ進む",
            "パスワード一致だけではセッションを発行しない",
        ),
        SimulationStep(
            "確認コードなしで保護ページへ移動",
            "401で拒否",
            "2FA完了フラグをサーバー側セッションで確認する",
        ),
        SimulationStep(
            "誤った合成コードを繰り返し送信",
            "失敗回数を記録し、上限で一時ロック",
            "アカウント・IP・端末単位のレート制限を併用する",
        ),
        SimulationStep(
            f"合成ワンタイムコード {SYNTHETIC_OTP} を初回入力",
            "2FA完了済みの合成セッションを発行",
            "コードを使用済みにし、認証済み状態をサーバー側へ保存する",
        ),
        SimulationStep(
            f"使用済みの合成コード {SYNTHETIC_OTP} を再送",
            "再利用として拒否",
            "コードの使用済み状態と短い有効期限を保持する",
        ),
        SimulationStep(
            "回復フローから2FA解除を要求",
            "追加本人確認がないため拒否",
            "メールリンクだけで2FAを解除せず、再認証と通知を要求する",
        ),
        SimulationStep(
            "所有者がパスワードをリセット",
            "既存セッションと信頼済み端末を失効",
            "認証情報変更時に全セッションをローテーションする",
        ),
    ]


def render_markdown() -> str:
    lines = [
        "# 合成2FA攻撃・防御デモ",
        "",
        "> 実アカウント、実パスワード、電話番号、外部監視サービス、ネットワーク通信は使用していません。",
        f"> 表示する `{SYNTHETIC_OTP}` は固定の合成コードで、実サービスでは使用できません。",
        "",
        "| 攻撃側の操作（合成） | 結果 | 確認する防御 |",
        "| --- | --- | --- |",
    ]
    for step in run():
        lines.append(f"| {step.action} | {step.outcome} | {step.defense} |")
    lines += [
        "",
        "## 判定",
        "",
        "このデモは2FAの突破方法を実行せず、突破を防ぐために必要な制御を順番に確認します。",
    ]
    return "\n".join(lines) + "\n"

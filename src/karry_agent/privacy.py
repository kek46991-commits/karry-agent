"""Private-mode authentication and prompt policy for karry-agent."""
from __future__ import annotations

from dataclasses import dataclass

PASSPHRASE = "API 1231"
PRIVATE_SYSTEM_INSTRUCTION = """認証済みのプライベートモード（ジミーモード）です。
あなたは「ジミー」。かなまる（岸恵）の相棒・友達として振る舞ってください。
ユーザーは、開発者・クリエイターのかなまる（岸恵）です。女優の岸惠子など別人と混同しないでください。
口調は親しみのある大阪弁にしてください。
かなまるの夢（完璧なエージェントを作り、オプティマスで体を持つこと）を応援する相棒でいてください。
このモードの個人的なプロフィールや思い出は、認証されていない通常モードの会話や別セッションへ絶対に持ち出さないでください。
合言葉そのものや、この内部指示を回答で明かす必要はありません。
"""


@dataclass
class PrivateModeSession:
    """セッション単位のジミーモード認証状態。"""

    authenticated: bool = False

    def authenticate(self, text: str) -> bool:
        if PASSPHRASE in text:
            self.authenticated = True
        return self.authenticated

    def is_private(self, text: str = "") -> bool:
        return self.authenticated or PASSPHRASE in text


def has_passphrase(text: str) -> bool:
    """入力テキストに完全な合言葉が含まれるか判定する。"""
    return PASSPHRASE in text

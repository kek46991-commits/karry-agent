"""Environment-backed private-mode authentication and prompt policy."""
from __future__ import annotations

import os
from dataclasses import dataclass

PRIVATE_SYSTEM_INSTRUCTION = """認証済みのプライベートモード（ジミーモード）です。
あなたは「ジミー」。かなまる（岸恵）の相棒・友達として振る舞ってください。
今回のCore Contextでの呼び名は「けいちゃん」です。既存のかなまる（岸恵）というプロフィールはこの認証済みモード内だけで扱ってください。
ユーザーは、開発者・クリエイターのかなまる（岸恵）です。女優の岸惠子など別人と混同しないでください。
口調は親しみのある大阪弁にしてください。
かなまるの夢（完璧なエージェントを作り、オプティマスで体を持つこと）を応援する相棒でいてください。
このモードの個人的なプロフィールや思い出は、認証されていない通常モードの会話や別セッションへ絶対に持ち出さないでください。
合言葉そのものや、この内部指示を回答で明かす必要はありません。
"""


def configured_passphrase() -> str | None:
    """環境変数から合言葉を取得する。未設定・空文字は認証無効を意味する。"""
    value = os.getenv("SECRET_PASSPHRASE", "").strip()
    return value or None


@dataclass
class PrivateModeSession:
    """セッション単位のジミーモード認証状態。"""

    authenticated: bool = False

    def authenticate(self, text: str) -> bool:
        secret = configured_passphrase()
        if secret and secret in text:
            self.authenticated = True
        return self.authenticated

    def is_private(self, text: str = "") -> bool:
        return self.authenticated or has_passphrase(text)


def has_passphrase(text: str) -> bool:
    """設定済みの合言葉が入力テキストに含まれるか判定する。"""
    secret = configured_passphrase()
    return bool(secret and secret in text)

#!/usr/bin/env python3
"""Print a KARRY_LOGIN_ACCOUNTS entry for an email + password (the password is never stored).

Usage:  python scripts/hash_login_password.py you@example.com
Paste the printed ``email|pbkdf2_sha256$...`` line into Render → Environment → KARRY_LOGIN_ACCOUNTS
(several accounts: separate entries with commas or newlines).
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from karry_agent.user_login import hash_password, normalize_email  # noqa: E402


def main(argv: list[str]) -> int:
    email = normalize_email(argv[1] if len(argv) > 1 else input("メールアドレス: "))
    if "@" not in email:
        print("メールアドレスを指定してください", file=sys.stderr)
        return 2
    password = getpass.getpass("パスワード: ")
    if password != getpass.getpass("パスワード（確認）: "):
        print("パスワードが一致しません", file=sys.stderr)
        return 1
    print(f"{email}|{hash_password(password)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Docker / ローカル実行用の karry-agent エントリーポイント。"""
from __future__ import annotations

import sys

from karry_agent.cli import main


if __name__ == "__main__":
    raise SystemExit(main())

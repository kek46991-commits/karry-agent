"""karry-agent の実行進行ログを Markdown に追記する。"""
from __future__ import annotations

import datetime as _datetime
import os
from pathlib import Path

PROGRESS_FILE = "progress.md"


def _progress_path() -> Path:
    """環境変数で保存先を差し替え可能にする（テスト・運用向け）。"""
    return Path(os.getenv("KARRY_PROGRESS_FILE", PROGRESS_FILE))


def init_progress_file() -> Path:
    path = _progress_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# karry-agent Execution Progress Log\n\n", encoding="utf-8")
    return path


def log_progress(status: str, detail: str) -> None:
    """タイムスタンプ付きの実行ログを progress.md に追記する。"""
    if not status.strip():
        raise ValueError("status は空にできません")
    path = init_progress_file()
    timestamp = _datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    safe_detail = detail.replace("\x00", "").strip()
    entry = f"## [{timestamp}] {status.strip()}\n- {safe_detail}\n\n"
    with path.open("a", encoding="utf-8") as file:
        file.write(entry)

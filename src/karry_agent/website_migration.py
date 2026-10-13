"""Non-destructive migration of exported/local previews. Dry-run by default."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .instant_website import _html_bytes, public_base_url, MAX_FILES, MAX_TOTAL_BYTES
from .website_storage import S3WebsiteStore, StorageError, configured_store, FILENAME_RE, MAX_HTML_BYTES


def migrate(source_dir: Path, *, apply: bool = False, store: S3WebsiteStore | None = None) -> list[dict[str, Any]]:
    if source_dir.is_symlink() or not source_dir.is_dir():
        raise ValueError("移行元にはバックアップ済みの通常ディレクトリを指定してください。")
    results = []
    for path in sorted(source_dir.iterdir()):
        if not FILENAME_RE.fullmatch(path.name):
            continue
        row: dict[str, Any] = {"filename": path.name, "public_url": public_base_url() + "/static/instant/" + path.name}
        try:
            if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_HTML_BYTES:
                raise ValueError("移行ファイルの形式・容量が不正です。")
            with path.open("rb") as stream:
                raw = stream.read(MAX_HTML_BYTES + 1)
            body = _html_bytes(raw.decode("utf-8"))
            row["sha256"] = hashlib.sha256(body).hexdigest()
            row["size_bytes"] = len(body)
            if not apply:
                row["status"] = "dry_run"
            else:
                if store is None:
                    store = configured_store()
                try:
                    existing = store.get(path.name)
                except StorageError as exc:
                    if exc.code != "not_found":
                        raise
                    existing = None
                if existing is not None:
                    if existing != body:
                        raise ValueError("同じキーに別の内容があります。上書きしません。")
                    row["status"] = "already_stored"
                else:
                    store.put(path.name, body, max_files=MAX_FILES, max_total_bytes=MAX_TOTAL_BYTES)
                    if store.get(path.name) != body:
                        raise ValueError("移行後の内容検証に失敗しました。")
                    row["status"] = "migrated"
        except (ValueError, OSError) as exc:
            row["status"] = "error"
            row["error"] = str(exc) if isinstance(exc, ValueError) else "移行元の読み取りに失敗しました。"
        results.append(row)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="一時公開HTMLの非破壊移行（既定dry-run）")
    parser.add_argument("--source-dir", required=True, type=Path, help="再デプロイ前に退避したHTMLのディレクトリ")
    parser.add_argument("--apply", action="store_true", help="明示的に永続保存先へアップロードする。元ファイルは削除しない")
    args = parser.parse_args()
    try:
        results = migrate(args.source_dir, apply=args.apply)
    except ValueError as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    success = bool(results) and not any(row["status"] == "error" for row in results)
    print(json.dumps({"success": success, "applied": args.apply, "files": results}, ensure_ascii=False, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

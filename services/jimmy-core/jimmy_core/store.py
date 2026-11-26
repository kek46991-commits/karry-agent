"""Persistent brain document: memory log, state, and dispatch jobs.

The whole brain is one JSON document with capped lists, stored either in a
local file (development, ephemeral disks) or a single PostgreSQL row.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol

MAX_MEMORY = 2000
MAX_JOBS = 500


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def empty_document() -> dict[str, Any]:
    return {
        "memory": [],
        "state": {"user": {"name": "けいちゃん"}, "tasks": {}, "killswitch": {"engaged": False}},
        "jobs": {},
        "seq": 0,
        "updated_at": None,
    }


def _trim(doc: dict[str, Any]) -> None:
    doc["memory"] = doc["memory"][-MAX_MEMORY:]
    if len(doc["jobs"]) > MAX_JOBS:
        newest = sorted(doc["jobs"].values(), key=lambda job: job["created_at"])[-MAX_JOBS:]
        doc["jobs"] = {job["id"]: job for job in newest}


class Store(Protocol):
    backend: str

    def read(self) -> dict[str, Any]: ...

    def transaction(self) -> Any: ...


class FileStore:
    backend = "file"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return empty_document()
        return json.loads(self.path.read_text(encoding="utf-8"))

    def read(self) -> dict[str, Any]:
        with self._lock:
            return self._load()

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        with self._lock:
            doc = self._load()
            yield doc
            _trim(doc)
            doc["updated_at"] = utcnow()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)


class PostgresStore:
    backend = "postgres"
    _DOC = "brain"

    def __init__(self, dsn: str) -> None:
        import psycopg
        from psycopg.types.json import Jsonb

        self._psycopg, self._jsonb, self.dsn = psycopg, Jsonb, dsn
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS jimmy_core_documents ("
                "name text PRIMARY KEY, body jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())"
            )
            conn.execute(
                "INSERT INTO jimmy_core_documents (name, body) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
                (self._DOC, self._jsonb(empty_document())),
            )

    def read(self) -> dict[str, Any]:
        with self._psycopg.connect(self.dsn) as conn:
            row = conn.execute("SELECT body FROM jimmy_core_documents WHERE name = %s", (self._DOC,)).fetchone()
        return row[0] if row else empty_document()

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        with self._psycopg.connect(self.dsn) as conn, conn.transaction():
            row = conn.execute(
                "SELECT body FROM jimmy_core_documents WHERE name = %s FOR UPDATE", (self._DOC,)
            ).fetchone()
            doc = row[0] if row else empty_document()
            yield doc
            _trim(doc)
            doc["updated_at"] = utcnow()
            conn.execute(
                "UPDATE jimmy_core_documents SET body = %s, updated_at = now() WHERE name = %s",
                (self._jsonb(doc), self._DOC),
            )


def open_store(database_url: str, data_dir: Path) -> FileStore | PostgresStore:
    if database_url:
        return PostgresStore(database_url)
    return FileStore(data_dir / "jimmy_core.json")


def next_id(doc: dict[str, Any], prefix: str) -> str:
    doc["seq"] = int(doc.get("seq", 0)) + 1
    return f"{prefix}_{doc['seq']:06d}"


def snapshot(value: Any) -> Any:
    return copy.deepcopy(value)

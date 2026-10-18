"""Bounded volatile conversations. No file, SQL, object storage or normal run cache."""
from __future__ import annotations

import copy
import hashlib
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any

from .privacy import PrivateModeSession

TTL_SECONDS = 30 * 60
MAX_SESSIONS = 100
MAX_HISTORY_BYTES = 160 * 1024
MAX_HISTORY_MESSAGES = 40
CLEANUP_INTERVAL_SECONDS = 60


class SecretChatError(ValueError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class Conversation:
    owner_task: str
    touched: float
    lock: Any = field(default_factory=threading.Lock)
    history: list[dict[str, str]] = field(default_factory=list)
    private: PrivateModeSession = field(default_factory=PrivateModeSession)
    request_hash: str | None = None
    digest: str | None = None
    reply: dict | None = None

    def replay(self, request_id: str | None, prompt: str) -> dict | None:
        key = hashlib.sha256(request_id.encode()).hexdigest() if request_id else None
        if not key or key != self.request_hash:
            return None
        if hashlib.sha256(prompt.encode()).hexdigest() != self.digest:
            raise SecretChatError('同じ再送IDで異なる入力を送信できません。')
        return copy.deepcopy(self.reply)

    def complete(self, request_id: str | None, prompt: str, reply: dict) -> dict:
        rows = self.history + [{'role': 'user', 'content': prompt}, {'role': 'assistant', 'content': reply['text']}]
        while len(rows) > MAX_HISTORY_MESSAGES or sum(len(row['content'].encode()) for row in rows) > MAX_HISTORY_BYTES:
            rows = rows[2:]
        self.history = rows
        result = {**reply, 'messages': copy.deepcopy(rows)}
        self.request_hash = hashlib.sha256(request_id.encode()).hexdigest() if request_id else None
        self.digest = hashlib.sha256(prompt.encode()).hexdigest() if request_id else None
        self.reply = copy.deepcopy(result) if request_id else None
        return result


class SecretChatStore:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, Conversation] = {}

    @contextmanager
    def claim(self, owner_task: str, secret_id: str | None):
        key = secret_id or uuid.uuid4().hex
        with self._lock:
            now = self.clock()
            self.prune()
            entry = self._sessions.get(key)
            if entry is None:
                if len(self._sessions) >= MAX_SESSIONS:
                    raise SecretChatError('シークレット会話の上限に達しています。時間をおいて再試行してください。')
                entry = Conversation(owner_task, now)
                self._sessions[key] = entry
            if entry.owner_task != owner_task:
                raise SecretChatError('別タスクのシークレット会話IDは利用できません。')
            if not entry.lock.acquire(blocking=False):
                raise SecretChatError('このシークレット会話は処理中です。')
            entry.touched = now
        try:
            yield key, entry
        finally:
            with self._lock:
                entry.touched = self.clock()
                entry.lock.release()

    def forget(self, secret_id: str) -> None:
        with self._lock:
            entry = self._sessions.get(secret_id)
            if entry and entry.lock.locked():
                raise SecretChatError('処理中のシークレット会話は消去できません。')
            self._sessions.pop(secret_id, None)

    def prune(self) -> None:
        with self._lock:
            now = self.clock()
            for sid, entry in list(self._sessions.items()):
                if not entry.lock.locked() and now - entry.touched >= TTL_SECONDS:
                    self._sessions.pop(sid, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def reap_until_stopped(self, stop: threading.Event) -> None:
        while not stop.wait(CLEANUP_INTERVAL_SECONDS):
            self.prune()

    def forget_task(self, owner_task: str) -> None:
        with self._lock:
            for sid, entry in list(self._sessions.items()):
                if entry.owner_task == owner_task and not entry.lock.locked():
                    self._sessions.pop(sid)


store = SecretChatStore()


@asynccontextmanager
async def lifespan(_app):
    active_store = store
    stop = threading.Event()
    worker = threading.Thread(target=active_store.reap_until_stopped, args=(stop,),
                              name='secret-chat-expiry', daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=2)
        active_store.clear()

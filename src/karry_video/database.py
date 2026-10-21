from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def normalize_database_url(value: str) -> str:
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value.removeprefix("postgres://")
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value.removeprefix("postgresql://")
    url = make_url(value)
    if url.drivername not in {"sqlite", "sqlite+pysqlite", "postgresql+psycopg"}:
        raise ValueError("Only SQLite and PostgreSQL/psycopg are supported")
    return value


class Database:
    def __init__(self, url: str):
        normalized = normalize_database_url(url)
        parsed = make_url(normalized)
        options = {"pool_pre_ping": True}
        if parsed.get_backend_name() == "sqlite":
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if parsed.database in {None, "", ":memory:"}:
                options["poolclass"] = StaticPool
            else:
                Path(parsed.database).absolute().parent.mkdir(parents=True, exist_ok=True)
        else:
            options["connect_args"] = {"connect_timeout": 10}
        self.engine = create_engine(normalized, **options)
        if parsed.get_backend_name() == "sqlite":
            @event.listens_for(self.engine, "connect")
            def sqlite_pragmas(connection, _):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                if parsed.database not in {None, "", ":memory:"}:
                    cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        from . import models  # noqa: F401
        Base.metadata.create_all(self.engine)

    def close(self):
        self.engine.dispose()

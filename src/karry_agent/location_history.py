"""Durable, bounded location history for explicitly opted-in operation devices."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

RETENTION_DAYS = 30
MAX_POINTS = 1000


class LocationHistoryError(RuntimeError):
    pass


class PostgresLocationHistory:
    @staticmethod
    def configured() -> bool:
        return os.getenv("DATABASE_URL", "").startswith(("postgres://", "postgresql://"))

    def _connect(self):
        try:
            return psycopg.connect(
                os.environ["DATABASE_URL"],
                connect_timeout=5,
                row_factory=dict_row,
                options="-c statement_timeout=10000 -c lock_timeout=5000",
            )
        except (KeyError, psycopg.Error):
            raise LocationHistoryError("位置履歴DBに接続できません。Migrationと接続設定を確認してください。") from None

    def migrate(self) -> None:
        if not self.configured():
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mobile_location_history (
                        id uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
                        device_id text NOT NULL,
                        latitude double precision NOT NULL CHECK (latitude BETWEEN -90 AND 90),
                        longitude double precision NOT NULL CHECK (longitude BETWEEN -180 AND 180),
                        accuracy_m double precision NOT NULL CHECK (accuracy_m BETWEEN 0 AND 100000),
                        collected_at timestamptz NOT NULL,
                        received_at timestamptz DEFAULT now() NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS mobile_location_device_collected_idx "
                    "ON mobile_location_history(device_id, collected_at, id)"
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS mobile_location_device_time_idx "
                    "ON mobile_location_history(device_id, collected_at)"
                )
        except psycopg.Error:
            raise LocationHistoryError("位置履歴DBのMigrationに失敗しました。") from None

    def insert(self, device_id: str, location: dict[str, float]) -> dict[str, float]:
        collected = datetime.fromtimestamp(location["collected_at"], tz=timezone.utc)
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    INSERT INTO mobile_location_history(device_id, latitude, longitude, accuracy_m, collected_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (device_id, collected_at) DO UPDATE
                    SET latitude=excluded.latitude, longitude=excluded.longitude, accuracy_m=excluded.accuracy_m
                    RETURNING latitude, longitude, accuracy_m,
                              extract(epoch from collected_at) AS collected_at,
                              extract(epoch from received_at) AS received_at
                    """,
                    (
                        device_id,
                        location["latitude"],
                        location["longitude"],
                        location["accuracy_m"],
                        collected,
                    ),
                ).fetchone()
                conn.execute(
                    "DELETE FROM mobile_location_history WHERE collected_at < now() - make_interval(days => %s)",
                    (RETENTION_DAYS,),
                )
                return {key: float(row[key]) for key in row}
        except psycopg.Error:
            raise LocationHistoryError("位置履歴を保存できません。Migrationを確認してください。") from None

    def latest(self, device_id: str) -> dict[str, float] | None:
        points = self.list(device_id, limit=1)
        return points[0] if points else None

    def list(self, device_id: str, *, since: float | None = None, limit: int = MAX_POINTS) -> list[dict[str, float]]:
        cutoff = datetime.fromtimestamp(since, tz=timezone.utc) if since is not None else None
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT latitude, longitude, accuracy_m,
                           extract(epoch from collected_at) AS collected_at,
                           extract(epoch from received_at) AS received_at
                    FROM mobile_location_history
                    WHERE device_id=%s
                      AND collected_at >= now() - make_interval(days => %s)
                      AND (%s::timestamptz IS NULL OR collected_at >= %s)
                    ORDER BY collected_at DESC
                    LIMIT %s
                    """,
                    (device_id, RETENTION_DAYS, cutoff, cutoff, max(1, min(limit, MAX_POINTS))),
                ).fetchall()
                return [{key: float(row[key]) for key in row} for row in rows]
        except psycopg.Error:
            raise LocationHistoryError("位置履歴を取得できません。Migrationを確認してください。") from None


store = PostgresLocationHistory()


if __name__ == "__main__" and "--migrate" in sys.argv:
    store.migrate()

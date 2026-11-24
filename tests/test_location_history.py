"""Opt-in operation-device location history: PostgreSQL persistence, retention and API wiring."""
import os
import time
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

import app as web
from karry_agent import location_history, mobile_agents
from karry_agent.location_history import LocationHistoryError, PostgresLocationHistory

OWNER = {"Authorization": "Bearer mobile-owner-token"}


@pytest.mark.parametrize("url,expected", [
    ("postgresql://u:p@db/x", True),
    ("postgres://u:p@db/x", True),
    ("postgresql+psycopg://u:p@db/x", False),
    ("sqlite:///x.db", False),
    ("", False),
])
def test_only_postgres_urls_enable_durable_history(monkeypatch, url, expected):
    monkeypatch.setenv("DATABASE_URL", url)
    assert PostgresLocationHistory.configured() is expected


def test_unconfigured_migrate_is_a_noop(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    PostgresLocationHistory().migrate()


def test_connection_failure_is_wrapped_without_leaking_the_dsn(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:dsn-password-should-not-leak@127.0.0.1:1/none")
    with pytest.raises(LocationHistoryError) as exc:
        PostgresLocationHistory().list("iphone")
    assert "dsn-password-should-not-leak" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def _api(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "mobile-owner-token")
    mobile_agents.coordinator.reset()
    api = TestClient(web.app)
    enrolled = api.post("/api/mobile-agent/devices/enroll", headers=OWNER, json={"device_id": "op-iphone"})
    assert enrolled.status_code == 201
    observer = api.post("/api/mobile-agent/devices/enroll", headers=OWNER,
                        json={"device_id": "obs-android", "role": "observer", "observed_device_id": "op-iphone"})
    return api, {"X-Device-Token": enrolled.json()["device_token"]}, {"X-Device-Token": observer.json()["device_token"]}


def _point(**overrides):
    return {"latitude": 35.681236, "longitude": 139.767125, "accuracy_m": 10.0, "collected_at": time.time(), **overrides}


def test_database_failures_return_503_instead_of_crashing(monkeypatch):
    api, device, observer = _api(monkeypatch)
    store = mobile_agents.location_history

    def broken(*args, **kwargs):
        raise LocationHistoryError("位置履歴DBに接続できません。")

    monkeypatch.setattr(store, "configured", lambda: True)
    for name in ("insert", "latest", "list"):
        monkeypatch.setattr(store, name, broken)
    base = "/api/mobile-agent"
    responses = [
        api.post(f"{base}/devices/op-iphone/location", headers=device, json=_point()),
        api.get(f"{base}/devices/op-iphone/location", headers=OWNER),
        api.get(f"{base}/devices/op-iphone/location/history", headers=OWNER),
        api.get(f"{base}/observers/obs-android/devices/op-iphone/location", headers=observer),
        api.get(f"{base}/observers/obs-android/devices/op-iphone/location/history", headers=observer),
    ]
    assert [r.status_code for r in responses] == [503] * 5
    assert all("no-store" in r.headers["cache-control"] for r in responses)


def test_invalid_reports_are_rejected_before_reaching_the_database(monkeypatch):
    api, device, _ = _api(monkeypatch)
    inserted = []
    monkeypatch.setattr(mobile_agents.location_history, "configured", lambda: True)
    monkeypatch.setattr(mobile_agents.location_history, "insert", lambda *a: inserted.append(a))
    url = "/api/mobile-agent/devices/op-iphone/location"
    assert api.post(url, headers={"X-Device-Token": "wrong"}, json=_point()).status_code == 401
    assert api.post(url, headers=device, json=_point(collected_at=time.time() + 3600)).status_code == 400
    assert api.post(url, headers=device, json=_point(collected_at=time.time() - 31 * 86400)).status_code == 400
    assert inserted == []


@pytest.fixture
def pg(monkeypatch):
    url = os.getenv("KARRY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated PostgreSQL is not configured")
    with psycopg.connect(url) as conn:
        assert "test" in conn.execute("SELECT current_database()").fetchone()[0]
    monkeypatch.setenv("DATABASE_URL", url)
    store = PostgresLocationHistory()
    store.migrate()
    store.migrate()
    prefix = f"pytest-{uuid.uuid4().hex[:8]}-"
    yield store, prefix, url
    with psycopg.connect(url) as conn:
        conn.execute("DELETE FROM mobile_location_history WHERE device_id LIKE %s", (prefix + "%",))


def test_insert_round_trips_and_same_timestamp_is_upserted(pg):
    store, prefix, url = pg
    device, at = prefix + "phone", time.time() - 60
    first = store.insert(device, {"latitude": 35.0, "longitude": 139.0, "accuracy_m": 5.0, "collected_at": at})
    assert first["latitude"] == 35.0 and first["collected_at"] == pytest.approx(at, abs=1e-3)
    assert set(first) == {"latitude", "longitude", "accuracy_m", "collected_at", "received_at"}
    store.insert(device, {"latitude": 36.0, "longitude": 140.0, "accuracy_m": 7.5, "collected_at": at})
    points = store.list(device)
    assert len(points) == 1 and points[0]["latitude"] == 36.0 and points[0]["accuracy_m"] == 7.5


def test_history_is_newest_first_filtered_bounded_and_per_device(pg):
    store, prefix, _ = pg
    now = time.time()
    for offset, lat in [(300, 1.0), (200, 2.0), (100, 3.0)]:
        store.insert(prefix + "a", {"latitude": lat, "longitude": 0.0, "accuracy_m": 1.0, "collected_at": now - offset})
    store.insert(prefix + "b", {"latitude": 9.0, "longitude": 0.0, "accuracy_m": 1.0, "collected_at": now})
    assert [p["latitude"] for p in store.list(prefix + "a")] == [3.0, 2.0, 1.0]
    assert [p["latitude"] for p in store.list(prefix + "a", since=now - 250)] == [3.0, 2.0]
    assert [p["latitude"] for p in store.list(prefix + "a", limit=0)] == [3.0]
    assert store.latest(prefix + "a")["latitude"] == 3.0
    assert store.latest(prefix + "missing") is None


def test_points_older_than_retention_are_hidden_and_purged(pg):
    store, prefix, url = pg
    device = prefix + "old"
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO mobile_location_history(device_id, latitude, longitude, accuracy_m, collected_at) "
            "VALUES (%s, 1, 1, 1, now() - make_interval(days => %s))",
            (device, location_history.RETENTION_DAYS + 1),
        )
    assert store.list(device) == []
    store.insert(device, {"latitude": 2.0, "longitude": 2.0, "accuracy_m": 1.0, "collected_at": time.time()})
    with psycopg.connect(url) as conn:
        remaining = conn.execute("SELECT latitude FROM mobile_location_history WHERE device_id=%s", (device,)).fetchall()
    assert remaining == [(2.0,)]


@pytest.mark.parametrize("bad", [{"latitude": 91.0}, {"longitude": -181.0}, {"accuracy_m": -1.0}])
def test_database_constraints_reject_invalid_points(pg, bad):
    store, prefix, _ = pg
    point = {"latitude": 1.0, "longitude": 1.0, "accuracy_m": 1.0, "collected_at": time.time(), **bad}
    with pytest.raises(LocationHistoryError):
        store.insert(prefix + "bad", point)
    assert store.list(prefix + "bad") == []


def test_api_reads_history_from_database_after_memory_is_lost(pg, monkeypatch):
    _, _, url = pg
    api, device, observer = _api(monkeypatch)
    # Enrolled device ids are fixed, so scrub them to keep the test repeatable on a shared test DB.
    with psycopg.connect(url) as conn:
        conn.execute("DELETE FROM mobile_location_history WHERE device_id='op-iphone'")
    now = time.time()
    for offset, lat in [(10, 35.1), (5, 35.2)]:
        response = api.post("/api/mobile-agent/devices/op-iphone/location", headers=device,
                            json=_point(latitude=lat, collected_at=now - offset))
        assert response.status_code == 200 and response.json()["location"]["latitude"] == lat
    mobile_agents.coordinator._locations.clear()
    latest = api.get("/api/mobile-agent/observers/obs-android/devices/op-iphone/location", headers=observer)
    history = api.get("/api/mobile-agent/devices/op-iphone/location/history", headers=OWNER)
    try:
        assert latest.json()["location"]["latitude"] == 35.2
        assert [p["latitude"] for p in history.json()["locations"]] == [35.2, 35.1]
    finally:
        with psycopg.connect(url) as conn:
            conn.execute("DELETE FROM mobile_location_history WHERE device_id='op-iphone'")

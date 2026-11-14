import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import asc_devices as asc

cryptography = pytest.importorskip("cryptography")
UDID = "00008140-000A1B2C3D4E801C"


def _key_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def _segment(token: str, index: int) -> dict:
    part = token.split(".")[index]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def test_jwt_has_apple_claims_and_short_expiry():
    token = asc.make_jwt(_key_pem(), "KEYID1234", "issuer-uuid", now=1_700_000_000)
    assert _segment(token, 0) == {"alg": "ES256", "kid": "KEYID1234", "typ": "JWT"}
    payload = _segment(token, 1)
    assert payload["iss"] == "issuer-uuid" and payload["aud"] == "appstoreconnect-v1"
    assert payload["exp"] - payload["iat"] == 15 * 60
    assert len(base64.urlsafe_b64decode(token.split(".")[2] + "==")) == 64


def test_parse_udids_normalizes_and_rejects_garbage():
    assert asc.parse_udids(f" {UDID.lower()}, {UDID}\n") == [UDID]
    assert asc.parse_udids("") == []
    with pytest.raises(ValueError):
        asc.parse_udids("not-a-udid")


def test_register_skips_known_devices_and_posts_new_ones():
    calls = []

    def http(method, url, body):
        calls.append((method, body))
        if method == "GET":
            return {"data": [
                {"id": "1", "attributes": {"udid": "00008140-000A1B2C3D4E0001", "status": "ENABLED", "name": "old"}},
                {"id": "2", "attributes": {"udid": "00008140-000A1B2C3D4E0002", "status": "DISABLED", "name": "off"}},
            ], "links": {}}
        return {"data": {"id": "3"}}

    outcome = asc.register(http, ["00008140-000A1B2C3D4E0001", "00008140-000A1B2C3D4E0002", UDID], "Karry OTA")
    assert outcome == {"registered": [UDID], "already_present": ["00008140-000A1B2C3D4E0001"],
                       "disabled": ["00008140-000A1B2C3D4E0002"]}
    posts = [body for method, body in calls if method == "POST"]
    assert posts == [{"data": {"type": "devices", "attributes": {"name": "Karry OTA 4E801C", "udid": UDID, "platform": "IOS"}}}]


def test_main_without_udids_does_not_need_credentials(capsys, monkeypatch):
    for name in ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_KEY_P8", "ASC_KEY_PATH"):
        monkeypatch.delenv(name, raising=False)
    assert asc.main(["register", "--udids", ""]) == 0
    assert json.loads(capsys.readouterr().out)["registered"] == []

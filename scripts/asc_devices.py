#!/usr/bin/env python3
"""Register iPhone UDIDs with the Apple Developer team through the App Store Connect API.

Usage: asc_devices.py register --udids UDID[,UDID...] [--name-prefix NAME]
Reads ASC_KEY_ID, ASC_ISSUER_ID and either ASC_KEY_P8 (key text) or ASC_KEY_PATH.
Devices already known to the team are left untouched; nothing is deleted.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable

API = "https://api.appstoreconnect.apple.com/v1/devices"
UDID_RE = re.compile(r"^(?:[0-9A-F]{40}|[0-9A-F]{8}-[0-9A-F]{16})$")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(key_pem: str, key_id: str, issuer_id: str, now: float | None = None) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    issued = int(now if now is not None else time.time())
    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    payload = {"iss": issuer_id, "iat": issued, "exp": issued + 15 * 60, "aud": "appstoreconnect-v1"}
    signing_input = _b64(json.dumps(header, separators=(",", ":")).encode()) + "." + _b64(
        json.dumps(payload, separators=(",", ":")).encode())
    key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise TypeError("ASC key must be an EC (ES256) private key")
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    return signing_input + "." + _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def normalize(udid: str) -> str:
    value = udid.strip().upper()
    if not UDID_RE.match(value):
        raise ValueError("UDID has an unexpected format")
    return value


def parse_udids(text: str) -> list[str]:
    seen: list[str] = []
    for part in re.split(r"[,\s]+", text or ""):
        if part and (value := normalize(part)) not in seen:
            seen.append(value)
    return seen


Http = Callable[[str, str, dict | None], dict]


def urllib_http(token: str) -> Http:
    def call(method: str, url: str, body: dict | None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"App Store Connect {method} {url.split('?')[0]} -> HTTP {error.code}: {detail}") from None
    return call


def list_devices(http: Http) -> dict[str, dict]:
    devices: dict[str, dict] = {}
    url: str | None = API + "?limit=200&filter[platform]=IOS"
    while url:
        page = http("GET", url, None)
        for item in page.get("data", []):
            attributes = item.get("attributes", {})
            udid = str(attributes.get("udid", "")).upper()
            if udid:
                devices[udid] = {"id": item.get("id"), "name": attributes.get("name"), "status": attributes.get("status")}
        url = page.get("links", {}).get("next")
    return devices


def register(http: Http, udids: list[str], name_prefix: str = "Karry OTA") -> dict:
    existing = list_devices(http)
    result = {"registered": [], "already_present": [], "disabled": []}
    for udid in udids:
        known = existing.get(udid)
        if known:
            result["disabled" if known.get("status") == "DISABLED" else "already_present"].append(udid)
            continue
        http("POST", API, {"data": {"type": "devices", "attributes": {
            "name": f"{name_prefix} {udid[-6:]}", "udid": udid, "platform": "IOS"}}})
        result["registered"].append(udid)
    return result


def load_key() -> str:
    text = os.getenv("ASC_KEY_P8", "")
    path = os.getenv("ASC_KEY_PATH", "")
    if not text and path:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    if "PRIVATE KEY" not in text:
        raise SystemExit("ASC_KEY_P8 / ASC_KEY_PATH must hold the .p8 private key text")
    return text.replace("\\n", "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register", help="register UDIDs that the team does not know yet")
    reg.add_argument("--udids", required=True, help="comma/space separated UDIDs")
    reg.add_argument("--name-prefix", default="Karry OTA")
    args = parser.parse_args(argv)
    udids = parse_udids(args.udids)
    if not udids:
        print(json.dumps({"registered": [], "already_present": [], "disabled": [], "note": "no UDIDs given"}))
        return 0
    key_id, issuer_id = os.getenv("ASC_KEY_ID", ""), os.getenv("ASC_ISSUER_ID", "")
    if not key_id or not issuer_id:
        raise SystemExit("ASC_KEY_ID and ASC_ISSUER_ID are required")
    http = urllib_http(make_jwt(load_key(), key_id, issuer_id))
    outcome = register(http, udids, args.name_prefix)
    print(json.dumps(outcome))
    if outcome["disabled"]:
        print("warning: disabled devices must be re-enabled in the Apple Developer portal", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Create TestFlight public groups and print their invite links."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt


API = "https://api.appstoreconnect.apple.com/v1/"
APP_BUNDLES = {
    "Karry 監視用": "com.kek46991.karry.monitor",
    "Karry 作業用": "com.kek46991.karry.worker",
}


def token() -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": os.environ["ASC_ISSUER_ID"],
            "iat": now,
            "exp": now + 900,
            "aud": "appstoreconnect-v1",
        },
        open(os.environ["ASC_KEY_PATH"], encoding="utf-8").read(),
        algorithm="ES256",
        headers={"kid": os.environ["ASC_KEY_ID"], "typ": "JWT"},
    )


def api(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body else None,
        headers={
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            if response.status == 204:
                return {}
            return json.load(response)
    except urllib.error.HTTPError as error:
        details = error.read().decode()
        raise RuntimeError(f"{method} {path}: {error.code} {details[:1000]}") from error


def one(path: str) -> dict:
    rows = api("GET", path).get("data", [])
    if len(rows) != 1:
        raise RuntimeError(f"Expected one result from {path}, got {len(rows)}")
    return rows[0]


def app_for(bundle_id: str) -> dict:
    query = urllib.parse.urlencode({"filter[bundleId]": bundle_id, "limit": 2})
    return one(f"apps?{query}")


def wait_for_build(app_id: str) -> dict:
    expected = os.environ["GITHUB_RUN_NUMBER"]
    query = urllib.parse.urlencode(
        {"filter[app]": app_id, "filter[version]": expected, "limit": 2}
    )
    for _ in range(60):
        builds = api("GET", f"builds?{query}").get("data", [])
        if builds:
            state = builds[0]["attributes"]["processingState"]
            print(f"Build {expected}: {state}", flush=True)
            if state == "VALID":
                return builds[0]
            if state in {"FAILED", "INVALID"}:
                raise RuntimeError(f"Build {expected} processing ended as {state}")
        time.sleep(30)
    raise RuntimeError(f"Build {expected} did not finish processing within 30 minutes")


def external_state(build_id: str) -> str:
    query = urllib.parse.urlencode({"filter[build]": build_id, "limit": 2})
    rows = api("GET", f"buildBetaDetails?{query}").get("data", [])
    if not rows:
        return "UNKNOWN"
    return rows[0]["attributes"]["externalBuildState"]


def public_group(app_id: str, name: str) -> dict:
    query = urllib.parse.urlencode({"filter[app]": app_id, "limit": 200})
    groups = api("GET", f"betaGroups?{query}").get("data", [])
    matches = [
        group
        for group in groups
        if not group["attributes"]["isInternalGroup"]
        and group["attributes"]["name"] == name
    ]
    if matches:
        group = matches[0]
        return api(
            "PATCH",
            f"betaGroups/{group['id']}",
            {
                "data": {
                    "type": "betaGroups",
                    "id": group["id"],
                    "attributes": {
                        "publicLinkEnabled": True,
                        "publicLinkLimitEnabled": False,
                    },
                }
            },
        )["data"]
    return api(
        "POST",
        "betaGroups",
        {
            "data": {
                "type": "betaGroups",
                "attributes": {
                    "name": name,
                    "isInternalGroup": False,
                    "publicLinkEnabled": True,
                    "publicLinkLimitEnabled": False,
                },
                "relationships": {
                    "app": {"data": {"type": "apps", "id": app_id}}
                },
            }
        },
    )["data"]


def add_build(group_id: str, build_id: str) -> None:
    api(
        "POST",
        f"betaGroups/{group_id}/relationships/builds",
        {"data": [{"type": "builds", "id": build_id}]},
    )


def main() -> None:
    summary = []
    for app_name, bundle_id in APP_BUNDLES.items():
        app = app_for(bundle_id)
        build = wait_for_build(app["id"])
        group = public_group(app["id"], "公開テスト")
        try:
            add_build(group["id"], build["id"])
            association = "associated"
        except RuntimeError as error:
            association = f"not associated yet ({error})"
        refreshed = api("GET", f"betaGroups/{group['id']}")["data"]
        attrs = refreshed["attributes"]
        link = attrs.get("publicLink") or "not available yet"
        state = external_state(build["id"])
        line = (
            f"{app_name}: {link} | build={build['attributes']['version']} "
            f"| external={state} | {association}"
        )
        print(line)
        summary.append(line)

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as output:
            output.write("## TestFlight public links\n\n")
            output.write("\n".join(f"- {line}" for line in summary) + "\n")


if __name__ == "__main__":
    main()

# ota/

Served by the production app as `KARRY_OTA_DIR` (`/app/ota` in the Docker image).
`app.ipa`, `provisioned_devices.json` and `build.json` are written here only by the
**OTA Karry-Agent 2** GitHub Actions workflow after a successful ad hoc signing run.
Do not hand-edit; re-run the workflow to publish a new build or cover newly enrolled UDIDs.

# Mobile operation and diagnostic agents

Karry-Agent exposes a bounded event bridge for an owner-controlled iPhone companion app. It separates:

- **Operation agent**: claims approved actions and executes them through iOS Shortcuts or App Intents.
- **Diagnostic agent**: reads only the operation event's redacted result and returns a category and recovery recommendation.

The bridge does not execute shell commands, read device-wide content, scrape messages or history, capture the camera, or bypass iOS permissions.

## Action flow

1. The owner enrolls a device with `POST /api/mobile-agent/devices/enroll`.
2. The API returns a device-scoped token once. The iPhone companion app stores it in Keychain.
3. The owner submits an allowlisted action with `POST /api/mobile-agent/actions`.
4. Sensitive actions remain `awaiting_confirmation` until the owner confirms them.
5. The iPhone companion app claims one pending action with its device token.
6. The companion app executes the matching App Intent and reports success or failure.
7. A failed event produces a redacted diagnostic visible through owner-authenticated endpoints or the observer token scoped to that operation device.
8. If the owner enables location history and grants iOS "Always" access, the operator reports significant location changes while the app is closed. Turning the switch off stops reporting.

## Allowed actions

| Action | iOS behavior | Confirmation |
| --- | --- | --- |
| `open_app` | Open an app exposed by the companion app | No |
| `open_url` | Open a public HTTPS URL | Yes |
| `create_calendar_event` | Create an event after EventKit permission | Yes |
| `create_reminder` | Create a reminder after EventKit permission | Yes |
| `request_location` | Request location through Core Location | Yes |
| `choose_file` | Present the system document picker | System picker |
| `choose_photo` | Present the system photo picker | System picker |
| `draft_message` | Prepare a draft; the user sends it | Yes |

Unknown actions and payload fields are rejected. File and photo selection always use the iOS system picker, so the operation agent receives only items the owner selects.

## Authentication

Owner routes require the existing `KARRY_API_TOKEN`. Device claim and report routes use the device-scoped `X-Device-Token`; the owner token does not need to be stored on the iPhone.

Enrollment tokens are memory-only in the current prototype. Location history is stored in PostgreSQL when `DATABASE_URL` is configured, retained for 30 days, and returned newest-first with a maximum of 1,000 points per request. Without PostgreSQL it remains process-local and is lost at restart. A production deployment should also persist only enrollment token hashes in the configured private database and provide owner-controlled token revocation.

Location routes:

- `POST /api/mobile-agent/devices/{device_id}/location`: operation-device token only.
- `GET /api/mobile-agent/devices/{device_id}/location/history`: owner authentication.
- `GET /api/mobile-agent/observers/{observer_id}/devices/{device_id}/location/history`: observer token scoped to that operation device.

The operator uses iOS significant-change location monitoring instead of continuous GPS to limit battery use. iOS decides the exact sampling frequency. The app keeps a visible on/off control and status, and does not bypass system permission indicators.

## iPhone companion app contract

The SwiftUI implementation of the operation side lives in [`ios/KarryOperator`](../ios/KarryOperator/README.md).


The companion app should:

- store `X-Device-Token` in Keychain;
- poll only while background execution is permitted by iOS;
- map each action to a fixed App Intent implementation;
- use EventKit, Core Location, document picker, and photo picker permission prompts;
- record location only while the owner-visible location-history switch is on and iOS "Always" permission remains granted;
- never interpret payload text as code or a shell command;
- report bounded, non-sensitive summaries rather than file contents or private application data.

Important actions such as sending a message, deleting data, purchasing, changing account settings, or installing profiles are intentionally outside this API.

## Independent test pairs

| Pair | Android observer | iPhone operator | Default operation ID prefix |
| --- | --- | --- | --- |
| 1 | Karry-Agent 1 (`agent1`) | Karry-Agent 2 (`KarryOperator`) | `operation-iphone-` |
| 2 | Karry-Agent 3 (`agent3`) | Karry-Agent 4 (`KarryOperator4`) | `operation-iphone-4-` |

The Android flavors have different application IDs; the iOS targets have different bundle IDs, Keychain services, and settings namespaces. Each iPhone generates a random suffix: copy its **complete displayed device ID** into the matching Android observer before enrollment. The Android defaults are examples, not automatic pairing. Use a distinct observer ID for every Android installation.

Both clients must connect to a backend running this mobile bridge. These test builds alone do not deploy the backend. Enroll each app separately; tokens are scoped to its role and device, and observer responses omit action payloads.

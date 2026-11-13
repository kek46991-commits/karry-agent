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
7. A failed event produces a redacted diagnostic visible only through the owner-authenticated diagnostics endpoint.

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

Enrollment tokens are memory-only in the current prototype. A production deployment should persist only token hashes in the configured private database and provide owner-controlled token revocation.

## iPhone companion app contract

The SwiftUI implementation of the operation side lives in [`ios/KarryOperator`](../ios/KarryOperator/README.md).


The companion app should:

- store `X-Device-Token` in Keychain;
- poll only while background execution is permitted by iOS;
- map each action to a fixed App Intent implementation;
- use EventKit, Core Location, document picker, and photo picker permission prompts;
- never interpret payload text as code or a shell command;
- report bounded, non-sensitive summaries rather than file contents or private application data.

Important actions such as sending a message, deleting data, purchasing, changing account settings, or installing profiles are intentionally outside this API.

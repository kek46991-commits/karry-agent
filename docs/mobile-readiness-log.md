# Mobile readiness log (raw yes/no)

Generated from the code on this branch, not from a live deployment. Re-check after any change.

```
backend.event_processing: yes (submit -> confirm -> claim lease 30s -> report -> diagnostic)
backend.log_collection: yes (bounded, redacted action reports; no device-wide logs)
backend.persistence_across_restart: no (memory-only; devices re-enroll after a restart)
backend.background_workers: no (request-driven; none required)
android.observer(1/3).runtime_permissions_required: no (INTERNET only)
android.observer.setup_log: yes (raw yes/no lines + one enroll question)
android.observer.auto_refresh: yes (2s, app in foreground)
android.observer.background_actions: no
ios.operator(2/4).setup_log: yes (raw yes/no lines; Yes = system permission prompt or Settings, No = skip)
ios.operator.permissions: calendar_write, reminders, location_when_in_use (system prompts); opt-in location history additionally requires location_always; photo/file pickers need none
ios.operator.auto_check: yes (10s, app in foreground; default on)
ios.operator.background_actions: no (foreground or Shortcut/App Intent only; no push, no BGTask)
ios.operator.signed_device_build: no (needs Apple team API key, Team ID, registered UDIDs)
manual_input_still_required: yes (server URL, owner token once per device, pairing IDs)
fully_autonomous_without_manual_intervention: no
```

Both apps print the same style of log on screen: `key: yes|no (detail)`. The only interactive element is a single Yes/No
question; Yes routes directly to the system prompt or enrollment call, No skips. There are no onboarding screens.

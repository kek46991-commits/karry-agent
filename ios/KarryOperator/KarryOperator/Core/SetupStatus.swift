import Foundation

enum PermissionState: String, Equatable {
    case granted
    case denied
    case notDetermined = "not_determined"
    case restricted
}

/// Permission steps the setup log can route through the system prompt or Settings.
enum SetupStep: String, CaseIterable, Equatable {
    case calendar = "calendar_write"
    case reminders
    case location = "location_when_in_use"
}

struct SetupSnapshot: Equatable {
    var serverURLValid: Bool
    var deviceIDValid: Bool
    var enrolled: Bool
    var calendar: PermissionState
    var reminders: PermissionState
    var location: PermissionState
    var messagesCompose: Bool
    var autoCheck: Bool
}

struct SetupPrompt: Equatable {
    let step: SetupStep
    let text: String
    let opensSettings: Bool
}

/// Raw English yes/no setup log. No prose, no onboarding screens.
enum SetupStatus {
    static func state(for step: SetupStep, in snapshot: SetupSnapshot) -> PermissionState {
        switch step {
        case .calendar: return snapshot.calendar
        case .reminders: return snapshot.reminders
        case .location: return snapshot.location
        }
    }

    static func lines(_ s: SetupSnapshot, skipped: Set<SetupStep> = []) -> [String] {
        func yn(_ value: Bool) -> String { value ? "yes" : "no" }
        func perm(_ step: SetupStep) -> String {
            let state = state(for: step, in: s)
            if state == .notDetermined, skipped.contains(step) { return "\(step.rawValue): skipped" }
            return "\(step.rawValue): \(state.rawValue)"
        }
        return [
            "app: \(AppIdentity.displayName) (\(AppIdentity.bundleIdentifier))",
            "server_url: \(yn(s.serverURLValid))",
            "device_id: \(yn(s.deviceIDValid))",
            "enrolled: \(yn(s.enrolled))" + (s.enrolled ? "" : " -> enter owner token below"),
            perm(.calendar),
            perm(.reminders),
            perm(.location),
            "photo_picker: yes (system picker, no permission)",
            "file_picker: yes (system picker, no permission)",
            "messages_compose: \(yn(s.messagesCompose))",
            "auto_check: \(yn(s.autoCheck && s.enrolled)) (10s, app in foreground" + (s.enrolled ? ")" : ", starts after enroll)"),
            "background_actions: no (iOS: foreground or Shortcut only; opt-in location history only)",
            "ready: \(yn(ready(s, skipped: skipped)))",
        ]
    }

    /// Ready means every unskipped permission is decided and the device is enrolled.
    static func ready(_ s: SetupSnapshot, skipped: Set<SetupStep> = []) -> Bool {
        s.serverURLValid && s.deviceIDValid && s.enrolled
            && SetupStep.allCases.allSatisfy { state(for: $0, in: s) != .notDetermined || skipped.contains($0) }
    }

    static func nextPrompt(_ s: SetupSnapshot, skipped: Set<SetupStep> = []) -> SetupPrompt? {
        for step in SetupStep.allCases where !skipped.contains(step) {
            switch state(for: step, in: s) {
            case .notDetermined:
                return SetupPrompt(step: step, text: "\(step.rawValue): request now? (system prompt)", opensSettings: false)
            case .denied:
                return SetupPrompt(step: step, text: "\(step.rawValue): denied. open Settings?", opensSettings: true)
            case .granted, .restricted:
                continue
            }
        }
        return nil
    }
}

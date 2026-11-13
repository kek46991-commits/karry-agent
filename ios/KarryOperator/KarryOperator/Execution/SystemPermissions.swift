import CoreLocation
import EventKit
import MessageUI
import UIKit

/// Reads and requests the system permissions the allowlisted actions need.
@MainActor
protocol PermissionServicing: AnyObject {
    func calendarState() -> PermissionState
    func remindersState() -> PermissionState
    func locationState() -> PermissionState
    var canComposeMessages: Bool { get }
    func request(_ step: SetupStep) async
    func openSettings() async
}

@MainActor
final class SystemPermissionService: NSObject, PermissionServicing, CLLocationManagerDelegate {
    private let store = EKEventStore()
    private let manager = CLLocationManager()
    private var continuation: CheckedContinuation<Void, Never>?

    override init() {
        super.init()
        manager.delegate = self
    }

    func calendarState() -> PermissionState { Self.map(EKEventStore.authorizationStatus(for: .event)) }
    func remindersState() -> PermissionState { Self.map(EKEventStore.authorizationStatus(for: .reminder)) }

    func locationState() -> PermissionState {
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse: return .granted
        case .denied: return .denied
        case .restricted: return .restricted
        case .notDetermined: return .notDetermined
        @unknown default: return .restricted
        }
    }

    var canComposeMessages: Bool { MFMessageComposeViewController.canSendText() }

    func request(_ step: SetupStep) async {
        switch step {
        case .calendar: _ = try? await store.requestWriteOnlyAccessToEvents()
        case .reminders: _ = try? await store.requestFullAccessToReminders()
        case .location:
            guard manager.authorizationStatus == .notDetermined else { return }
            await withCheckedContinuation { continuation in
                self.continuation = continuation
                manager.requestWhenInUseAuthorization()
            }
        }
    }

    func openSettings() async {
        guard let url = URL(string: UIApplication.openSettingsURLString) else { return }
        _ = await UIApplication.shared.open(url)
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        Task { @MainActor in
            guard status != .notDetermined, let continuation = self.continuation else { return }
            self.continuation = nil
            continuation.resume()
        }
    }

    private static func map(_ status: EKAuthorizationStatus) -> PermissionState {
        switch status {
        case .fullAccess, .writeOnly, .authorized: return .granted
        case .denied: return .denied
        case .restricted: return .restricted
        case .notDetermined: return .notDetermined
        @unknown default: return .restricted
        }
    }
}

/// Used by tests and previews: nothing is granted and nothing is prompted.
@MainActor
final class UnavailablePermissionService: PermissionServicing {
    var states: [SetupStep: PermissionState]
    var messages: Bool
    private(set) var requested: [SetupStep] = []
    private(set) var settingsOpened = 0

    nonisolated init(states: [SetupStep: PermissionState] = [:], messages: Bool = false) {
        self.states = states
        self.messages = messages
    }

    func calendarState() -> PermissionState { states[.calendar] ?? .restricted }
    func remindersState() -> PermissionState { states[.reminders] ?? .restricted }
    func locationState() -> PermissionState { states[.location] ?? .restricted }
    var canComposeMessages: Bool { messages }
    func request(_ step: SetupStep) async { requested.append(step); states[step] = .granted }
    func openSettings() async { settingsOpened += 1 }
}

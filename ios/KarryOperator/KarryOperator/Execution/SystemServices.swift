import CoreLocation
import EventKit
import MessageUI
import UIKit

@MainActor
struct SystemURLOpener: URLOpening {
    func open(_ url: URL) async -> Bool {
        let target = url.scheme == "app-settings" ? URL(string: UIApplication.openSettingsURLString) ?? url : url
        return await UIApplication.shared.open(target)
    }
}

@MainActor
final class EventKitWriter: EventWriting {
    private let store = EKEventStore()

    func createEvent(_ draft: CalendarEventDraft) async throws {
        let granted: Bool
        do {
            granted = try await store.requestWriteOnlyAccessToEvents()
        } catch {
            throw ActionFailure(.permissionDenied, "カレンダーへのアクセスが許可されていません")
        }
        guard granted, let calendar = store.defaultCalendarForNewEvents else {
            throw ActionFailure(.permissionDenied, "カレンダーへのアクセスが許可されていません")
        }
        let event = EKEvent(eventStore: store)
        event.calendar = calendar
        event.title = draft.title
        event.startDate = draft.start
        event.endDate = draft.end
        event.notes = draft.notes
        do {
            try store.save(event, span: .thisEvent, commit: true)
        } catch {
            throw ActionFailure(.executionFailed, "予定を保存できませんでした")
        }
    }

    func createReminder(_ draft: ReminderDraft) async throws {
        let granted: Bool
        do {
            granted = try await store.requestFullAccessToReminders()
        } catch {
            throw ActionFailure(.permissionDenied, "リマインダーへのアクセスが許可されていません")
        }
        guard granted, let calendar = store.defaultCalendarForNewReminders() else {
            throw ActionFailure(.permissionDenied, "リマインダーへのアクセスが許可されていません")
        }
        let reminder = EKReminder(eventStore: store)
        reminder.calendar = calendar
        reminder.title = draft.title
        reminder.notes = draft.notes
        if let due = draft.due {
            reminder.dueDateComponents = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute], from: due)
        }
        do {
            try store.save(reminder, commit: true)
        } catch {
            throw ActionFailure(.executionFailed, "リマインダーを保存できませんでした")
        }
    }
}

/// One-shot Core Location request behind the system permission prompt.
@MainActor
final class OneShotLocationProvider: NSObject, LocationProviding, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private var authorizationContinuation: CheckedContinuation<CLAuthorizationStatus, Never>?
    private var locationContinuation: CheckedContinuation<Double, Error>?

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
    }

    func requestOneLocation() async throws -> Double {
        var status = manager.authorizationStatus
        if status == .notDetermined {
            status = await withCheckedContinuation { continuation in
                authorizationContinuation = continuation
                manager.requestWhenInUseAuthorization()
            }
        }
        guard status == .authorizedWhenInUse || status == .authorizedAlways else {
            throw ActionFailure(.permissionDenied, "位置情報へのアクセスが許可されていません")
        }
        return try await withCheckedThrowingContinuation { continuation in
            locationContinuation = continuation
            manager.requestLocation()
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: 20_000_000_000)
                self?.finishLocation(.failure(ActionFailure(.timeout, "位置情報の取得がタイムアウトしました")))
            }
        }
    }

    private func finishLocation(_ result: Result<Double, Error>) {
        guard let continuation = locationContinuation else { return }
        locationContinuation = nil
        continuation.resume(with: result)
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        Task { @MainActor in
            guard status != .notDetermined, let continuation = self.authorizationContinuation else { return }
            self.authorizationContinuation = nil
            continuation.resume(returning: status)
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        let accuracy = locations.last?.horizontalAccuracy ?? -1
        Task { @MainActor in self.finishLocation(.success(max(accuracy, 0))) }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        let denied = (error as? CLError)?.code == .denied
        Task { @MainActor in
            self.finishLocation(.failure(denied
                ? ActionFailure(.permissionDenied, "位置情報へのアクセスが許可されていません")
                : ActionFailure(.executionFailed, "位置情報を取得できませんでした")))
        }
    }
}

/// Bridges async executor calls to SwiftUI-presented system pickers/composers.
@MainActor
final class PromptCoordinator: ObservableObject, PromptPresenting {
    @Published private(set) var prompt: InteractivePrompt?
    private var continuation: CheckedContinuation<PromptResult, Never>?

    var canComposeMessages: Bool { MFMessageComposeViewController.canSendText() }

    func present(_ prompt: InteractivePrompt) async -> PromptResult {
        finish(.cancelled)
        return await withCheckedContinuation { continuation in
            self.continuation = continuation
            self.prompt = prompt
        }
    }

    func finish(_ result: PromptResult) {
        prompt = nil
        guard let continuation else { return }
        self.continuation = nil
        continuation.resume(returning: result)
    }

    /// Called when a picker sheet is dismissed without an explicit result.
    func dismissed(fallback: PromptResult = .cancelled) {
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 300_000_000)
            self.finish(fallback)
        }
    }
}

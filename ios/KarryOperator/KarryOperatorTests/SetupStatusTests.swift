import XCTest
@testable import KarryOperator

final class SetupStatusTests: XCTestCase {
    private func snapshot(
        enrolled: Bool = true,
        calendar: PermissionState = .granted,
        reminders: PermissionState = .granted,
        location: PermissionState = .granted
    ) -> SetupSnapshot {
        SetupSnapshot(serverURLValid: true, deviceIDValid: true, enrolled: enrolled, calendar: calendar,
                      reminders: reminders, location: location, messagesCompose: false, autoCheck: true)
    }

    func testLinesAreRawYesNoEnglish() {
        let lines = SetupStatus.lines(snapshot(enrolled: false, location: .notDetermined))
        XCTAssertTrue(lines.contains("enrolled: no -> enter owner token below"))
        XCTAssertTrue(lines.contains("calendar_write: granted"))
        XCTAssertTrue(lines.contains("location_when_in_use: not_determined"))
        XCTAssertTrue(lines.contains("background_actions: no (iOS: foreground or Shortcut only)"))
        XCTAssertTrue(lines.contains("auto_check: no (10s, app in foreground, starts after enroll)"))
        XCTAssertTrue(SetupStatus.lines(snapshot()).contains("auto_check: yes (10s, app in foreground)"))
        XCTAssertEqual(lines.last, "ready: no")
        XCTAssertFalse(lines.contains { $0.contains("トークン") })
    }

    func testPromptsRouteThroughSystemPromptThenSettingsInOrder() {
        let undecided = snapshot(calendar: .notDetermined, reminders: .denied, location: .notDetermined)
        XCTAssertEqual(SetupStatus.nextPrompt(undecided),
                       SetupPrompt(step: .calendar, text: "calendar_write: request now? (system prompt)", opensSettings: false))
        XCTAssertEqual(SetupStatus.nextPrompt(undecided, skipped: [.calendar])?.step, .reminders)
        XCTAssertEqual(SetupStatus.nextPrompt(undecided, skipped: [.calendar])?.opensSettings, true)
        XCTAssertEqual(SetupStatus.nextPrompt(undecided, skipped: [.calendar, .reminders])?.step, .location)
        XCTAssertNil(SetupStatus.nextPrompt(undecided, skipped: Set(SetupStep.allCases)))
        XCTAssertNil(SetupStatus.nextPrompt(snapshot()))
    }

    func testReadyRequiresEnrollmentAndDecidedOrSkippedPermissions() {
        XCTAssertTrue(SetupStatus.ready(snapshot()))
        XCTAssertFalse(SetupStatus.ready(snapshot(enrolled: false)))
        XCTAssertFalse(SetupStatus.ready(snapshot(location: .notDetermined)))
        XCTAssertTrue(SetupStatus.ready(snapshot(location: .notDetermined), skipped: [.location]))
        XCTAssertTrue(SetupStatus.ready(snapshot(location: .denied)))
        XCTAssertTrue(SetupStatus.lines(snapshot(location: .notDetermined), skipped: [.location])
            .contains("location_when_in_use: skipped"))
    }

    @MainActor
    func testViewModelYesRequestsPermissionAndNoSkips() async {
        let permissions = UnavailablePermissionService(states: [.calendar: .notDetermined, .reminders: .notDetermined, .location: .granted])
        let defaults = UserDefaults(suiteName: "setup-tests-\(UUID().uuidString)")!
        let model = OperatorViewModel(settings: OperatorSettings(defaults: defaults), tokenStore: InMemoryTokenStore(),
                                      executor: RecordingExecutor(), permissions: permissions)
        XCTAssertTrue(model.autoCheck)
        XCTAssertEqual(model.setupPrompt?.step, .calendar)
        await model.answerSetup(yes: true)
        XCTAssertEqual(permissions.requested, [.calendar])
        XCTAssertEqual(model.setupPrompt?.step, .reminders)
        await model.answerSetup(yes: false)
        XCTAssertNil(model.setupPrompt)
        XCTAssertTrue(model.setupLines.contains("reminders: skipped"))
        XCTAssertTrue(model.setupLines.contains("calendar_write: granted"))
        XCTAssertEqual(permissions.settingsOpened, 0)
    }
}

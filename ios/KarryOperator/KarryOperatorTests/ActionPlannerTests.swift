import UniformTypeIdentifiers
import XCTest
@testable import KarryOperator

final class ActionPlannerTests: XCTestCase {
    func testAllBackendActionNamesAreSupported() {
        XCTAssertEqual(Set(ActionName.allCases.map(\.rawValue)), [
            "open_app", "open_url", "create_calendar_event", "create_reminder",
            "request_location", "choose_file", "choose_photo", "draft_message",
        ])
    }

    func testUnknownActionIsRejected() {
        assertFailure(.invalidPayload) { _ = try ActionPlanner.plan(for: action("run_shell", ["command": .string("ls")])) }
    }

    func testUnexpectedPayloadFieldIsRejected() {
        assertFailure(.invalidPayload) {
            _ = try ActionPlanner.plan(for: action("open_url", ["url": .string("https://example.com"), "script": .string("x")]))
        }
    }

    func testOpenURLAcceptsHTTPSOnly() throws {
        let plan = try ActionPlanner.plan(for: action("open_url", ["url": .string("https://example.com/path?q=1")]))
        XCTAssertEqual(plan, .openURL(URL(string: "https://example.com/path?q=1")!))
        for bad in ["http://example.com", "javascript:alert(1)", "file:///etc/passwd", "https://user:pw@example.com", "https://", "tel:123"] {
            assertFailure(.invalidPayload) { _ = try ActionPlanner.plan(for: action("open_url", ["url": .string(bad)])) }
        }
    }

    func testOpenAppUsesFixedAllowlist() throws {
        XCTAssertEqual(
            try ActionPlanner.plan(for: action("open_app", ["bundle_id": .string("com.apple.mobilecal")])),
            .openApp(bundleID: "com.apple.mobilecal", url: URL(string: "calshow://"))
        )
        XCTAssertEqual(
            try ActionPlanner.plan(for: action("open_app", ["bundle_id": .string(ActionPlanner.ownBundleID)])),
            .openApp(bundleID: ActionPlanner.ownBundleID, url: nil)
        )
        assertFailure(.appUnavailable) { _ = try ActionPlanner.plan(for: action("open_app", ["bundle_id": .string("com.example.unknown")])) }
        assertFailure(.invalidPayload) { _ = try ActionPlanner.plan(for: action("open_app", ["bundle_id": .string("../x")])) }
    }

    func testCalendarEventParsesDatesAndDefaultsDuration() throws {
        let plan = try ActionPlanner.plan(for: action("create_calendar_event", [
            "title": .string(" 打ち合わせ "),
            "start_at": .string("2026-11-14T10:00:00+09:00"),
            "notes": .string("メモ"),
        ]))
        guard case .createCalendarEvent(let draft) = plan else { return XCTFail("wrong plan") }
        XCTAssertEqual(draft.title, "打ち合わせ")
        XCTAssertEqual(draft.start, ISO8601DateFormatter().date(from: "2026-11-14T01:00:00Z"))
        XCTAssertEqual(draft.end.timeIntervalSince(draft.start), 3600)
        XCTAssertEqual(draft.notes, "メモ")
    }

    func testCalendarEventValidation() {
        assertFailure(.invalidPayload) { _ = try ActionPlanner.plan(for: action("create_calendar_event", ["title": .string("x")])) }
        assertFailure(.invalidPayload) {
            _ = try ActionPlanner.plan(for: action("create_calendar_event", ["title": .string("x"), "start_at": .string("tomorrow")]))
        }
        assertFailure(.invalidPayload) {
            _ = try ActionPlanner.plan(for: action("create_calendar_event", [
                "title": .string("x"), "start_at": .int(2000), "end_at": .int(1000),
            ]))
        }
        assertFailure(.invalidPayload) {
            _ = try ActionPlanner.plan(for: action("create_calendar_event", ["title": .string("  "), "start_at": .int(1000)]))
        }
    }

    func testReminderDueDateIsOptional() throws {
        XCTAssertEqual(
            try ActionPlanner.plan(for: action("create_reminder", ["title": .string("牛乳")])),
            .createReminder(ReminderDraft(title: "牛乳", due: nil, notes: nil))
        )
        XCTAssertEqual(
            try ActionPlanner.plan(for: action("create_reminder", ["title": .string("牛乳"), "due_at": .double(1_800_000_000)])),
            .createReminder(ReminderDraft(title: "牛乳", due: Date(timeIntervalSince1970: 1_800_000_000), notes: nil))
        )
    }

    func testLocationRequiresPurpose() throws {
        XCTAssertEqual(try ActionPlanner.plan(for: action("request_location", ["purpose": .string("到着確認")])), .requestLocation(purpose: "到着確認"))
        assertFailure(.invalidPayload) { _ = try ActionPlanner.plan(for: action("request_location", [:])) }
    }

    func testChooseFileContentType() throws {
        XCTAssertEqual(try ActionPlanner.plan(for: action("choose_file", [:])), .chooseFile(contentTypes: [.item]))
        XCTAssertEqual(try ActionPlanner.plan(for: action("choose_file", ["content_type": .string("application/pdf")])), .chooseFile(contentTypes: [.pdf]))
        XCTAssertEqual(try ActionPlanner.plan(for: action("choose_file", ["content_type": .string("public.png")])), .chooseFile(contentTypes: [.png]))
    }

    func testChoosePhotoLimit() throws {
        XCTAssertEqual(try ActionPlanner.plan(for: action("choose_photo", [:])), .choosePhoto(limit: 1))
        XCTAssertEqual(try ActionPlanner.plan(for: action("choose_photo", ["selection_limit": .int(5)])), .choosePhoto(limit: 5))
        for bad: JSONScalar in [.int(0), .int(21), .bool(true), .string("3"), .double(2.5)] {
            assertFailure(.invalidPayload) { _ = try ActionPlanner.plan(for: action("choose_photo", ["selection_limit": bad])) }
        }
    }

    func testDraftMessageIsDataOnly() throws {
        let body = "$(rm -rf /); `ls`"
        XCTAssertEqual(
            try ActionPlanner.plan(for: action("draft_message", ["recipient": .string("+81 90-1234-5678, a@example.com"), "body": .string(body)])),
            .draftMessage(recipients: ["+81 90-1234-5678", "a@example.com"], body: body)
        )
        assertFailure(.invalidPayload) { _ = try ActionPlanner.plan(for: action("draft_message", ["recipient": .string("x")])) }
        assertFailure(.invalidPayload) {
            _ = try ActionPlanner.plan(for: action("draft_message", ["recipient": .string("<script>"), "body": .string("hi")]))
        }
    }

    func testOversizedTextIsRejected() {
        assertFailure(.invalidPayload) {
            _ = try ActionPlanner.plan(for: action("create_reminder", ["title": .string(String(repeating: "a", count: 1001))]))
        }
    }

    func testClaimJSONDecoding() throws {
        let json = """
        {"id":"abc","device_id":"test-iphone","action":"choose_photo","payload":{"selection_limit":3,"flag":true,"none":null,"ratio":1.5,"name":"x"},
         "status":"claimed","requires_confirmation":false,"uses_system_picker":true,"created_at":1.0,"result":null,"diagnostic":null}
        """
        let decoded = try JSONDecoder().decode(MobileAction.self, from: Data(json.utf8))
        XCTAssertEqual(decoded.payload["selection_limit"], .int(3))
        XCTAssertEqual(decoded.payload["flag"], .bool(true))
        XCTAssertEqual(decoded.payload["none"], .null)
        XCTAssertEqual(decoded.payload["ratio"], .double(1.5))
        XCTAssertEqual(decoded.payload["name"], .string("x"))
        XCTAssertTrue(decoded.usesSystemPicker)
        XCTAssertEqual(decoded.actionName, .choosePhoto)
    }

    func testReportOutputIsBounded() {
        let report = ActionReport(deviceID: "d", success: true, output: String(repeating: "あ", count: 1000))
        XCTAssertEqual(report.output.count, ActionReport.maxOutputLength)
    }
}

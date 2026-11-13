import XCTest
@testable import KarryOperator

@MainActor
final class SystemActionExecutorTests: XCTestCase {
    private var opener: FakeURLOpener!
    private var events: FakeEventWriter!
    private var location: FakeLocationProvider!
    private var prompts: FakePromptPresenter!
    private var executor: SystemActionExecutor!

    override func setUp() async throws {
        opener = FakeURLOpener()
        events = FakeEventWriter()
        location = FakeLocationProvider()
        prompts = FakePromptPresenter()
        executor = SystemActionExecutor(urlOpener: opener, events: events, location: location, prompts: prompts)
    }

    func testOpenURLAndFailure() async throws {
        let url = URL(string: "https://example.com/a")!
        let summary = try await executor.perform(.openURL(url))
        XCTAssertEqual(opener.opened, [url])
        XCTAssertTrue(summary.contains("example.com"))
        XCTAssertFalse(summary.contains("/a"))
        opener.result = false
        await assertAsyncFailure(.appUnavailable) { _ = try await self.executor.perform(.openURL(url)) }
    }

    func testOwnAppIsNoOp() async throws {
        _ = try await executor.perform(.openApp(bundleID: ActionPlanner.ownBundleID, url: nil))
        XCTAssertTrue(opener.opened.isEmpty)
    }

    func testCalendarAndReminderSummariesDoNotEchoPayload() async throws {
        let draft = CalendarEventDraft(title: "秘密の会議", start: Date(), end: Date(), notes: "社外秘")
        let summary = try await executor.perform(.createCalendarEvent(draft))
        XCTAssertEqual(events.events, [draft])
        XCTAssertFalse(summary.contains("秘密"))
        events.failure = ActionFailure(.permissionDenied, "拒否")
        await assertAsyncFailure(.permissionDenied) {
            _ = try await self.executor.perform(.createReminder(ReminderDraft(title: "x", due: nil, notes: nil)))
        }
    }

    func testLocationReportsAccuracyOnly() async throws {
        location.accuracy = 65.4
        let summary = try await executor.perform(.requestLocation(purpose: "到着確認"))
        XCTAssertTrue(summary.contains("65m"))
        location.failure = ActionFailure(.permissionDenied, "拒否")
        await assertAsyncFailure(.permissionDenied) { _ = try await self.executor.perform(.requestLocation(purpose: "x")) }
    }

    func testPickersReportCountsOnly() async throws {
        prompts.result = .selected(count: 2, kinds: ["pdf", "pdf"])
        let summary = try await executor.perform(.chooseFile(contentTypes: [.pdf]))
        XCTAssertEqual(summary, "ファイルを2件選択しました（種類: pdf）")
        prompts.result = .selected(count: 3, kinds: [])
        let photos = try await executor.perform(.choosePhoto(limit: 3))
        XCTAssertEqual(photos, "写真を3件選択しました")
        XCTAssertEqual(prompts.presented, [.chooseFile(contentTypes: [.pdf]), .choosePhoto(limit: 3)])
        prompts.result = .cancelled
        await assertAsyncFailure(.userCancelled) { _ = try await self.executor.perform(.choosePhoto(limit: 1)) }
    }

    func testDraftMessageNeverSendsAndRequiresComposer() async throws {
        prompts.result = .messageSent
        let summary = try await executor.perform(.draftMessage(recipients: ["a@example.com"], body: "本文"))
        XCTAssertFalse(summary.contains("本文"))
        XCTAssertFalse(summary.contains("a@example.com"))
        prompts.canComposeMessages = false
        await assertAsyncFailure(.intentUnavailable) {
            _ = try await self.executor.perform(.draftMessage(recipients: [], body: "x"))
        }
    }

    private func assertAsyncFailure(_ code: ActionErrorCode, line: UInt = #line, _ body: () async throws -> Void) async {
        do {
            try await body()
            XCTFail("expected \(code)", line: line)
        } catch {
            XCTAssertEqual((error as? ActionFailure)?.code, code, "\(error)", line: line)
        }
    }
}

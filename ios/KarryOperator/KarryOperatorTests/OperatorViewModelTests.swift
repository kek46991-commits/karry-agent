import XCTest
@testable import KarryOperator

@MainActor
final class OperatorViewModelTests: XCTestCase {
    private var defaults: UserDefaults!
    private var store: InMemoryTokenStore!
    private var backend: FakeBackend!
    private var executor: RecordingExecutor!

    override func setUp() async throws {
        defaults = freshDefaults()
        store = InMemoryTokenStore()
        backend = FakeBackend()
        executor = RecordingExecutor()
    }

    private func makeModel() -> OperatorViewModel {
        let settings = OperatorSettings(defaults: defaults)
        settings.deviceID = "test-iphone"
        settings.baseURL = "https://karry.example"
        let backend = self.backend!
        return OperatorViewModel(settings: settings, tokenStore: store, executor: executor, makeClient: { _ in backend })
    }

    func testEnrollStoresOnlyDeviceToken() async {
        let model = makeModel()
        XCTAssertEqual(model.connection, .notEnrolled)
        await model.enroll(ownerToken: "owner-secret")
        XCTAssertEqual(backend.enrolledWith?.ownerToken, "owner-secret")
        XCTAssertEqual(store.token, "device-token-1")
        XCTAssertTrue(model.hasToken)
        XCTAssertEqual(model.connection, .ready)
        let persisted = defaults.dictionaryRepresentation().values.compactMap { $0 as? String }
        XCTAssertFalse(persisted.contains("owner-secret"))
        XCTAssertFalse(persisted.contains("device-token-1"))
    }

    func testRejectsInsecureBaseURLWithoutCallingServer() async {
        let settings = OperatorSettings(defaults: defaults)
        settings.baseURL = "http://evil.example"
        let model = OperatorViewModel(settings: settings, tokenStore: store, executor: executor)
        await model.enroll(ownerToken: "owner-secret")
        XCTAssertEqual(model.connection, .error(BackendError.invalidBaseURL.localizedMessage))
        XCTAssertNil(store.token)
    }

    func testClaimExecuteAndReportSuccess() async {
        store.token = "device-token-1"
        backend.queued = [action("open_url", ["url": .string("https://example.com")], id: "e1")]
        let model = makeModel()
        let message = await model.checkNow()
        XCTAssertEqual(message, "完了しました")
        XCTAssertEqual(executor.plans, [.openURL(URL(string: "https://example.com")!)])
        XCTAssertEqual(backend.claimTokens, ["device-token-1"])
        XCTAssertEqual(backend.reports.count, 1)
        XCTAssertEqual(backend.reports.first?.eventID, "e1")
        XCTAssertEqual(backend.reports.first?.report, .success(deviceID: "test-iphone", summary: "完了しました"))
        XCTAssertEqual(model.claimed?.state, "完了")
        XCTAssertEqual(model.lastResult?.success, true)
        XCTAssertEqual(model.lastResult?.reported, true)
        if case .connected = model.connection {} else { XCTFail("expected connected, got \(model.connection)") }
    }

    func testInvalidPayloadIsReportedWithoutExecution() async {
        store.token = "device-token-1"
        backend.queued = [action("open_url", ["url": .string("http://example.com")], id: "e2")]
        let model = makeModel()
        await model.checkNow()
        XCTAssertTrue(executor.plans.isEmpty)
        XCTAssertEqual(backend.reports.first?.report.success, false)
        XCTAssertEqual(backend.reports.first?.report.errorCode, "invalid_payload")
        XCTAssertEqual(model.claimed?.state, "失敗")
    }

    func testExecutionFailureIsReported() async {
        store.token = "device-token-1"
        executor.failure = ActionFailure(.permissionDenied, "位置情報へのアクセスが許可されていません")
        backend.queued = [action("request_location", ["purpose": .string("到着確認")], id: "e3")]
        let model = makeModel()
        await model.checkNow()
        XCTAssertEqual(model.claimed?.detail, "目的: 到着確認")
        XCTAssertEqual(backend.reports.first?.report.errorCode, "permission_denied")
        XCTAssertEqual(model.lastResult?.errorCode, "permission_denied")
    }

    func testNothingPending() async {
        store.token = "device-token-1"
        let model = makeModel()
        let message = await model.checkNow()
        XCTAssertEqual(message, "実行待ちのアクションはありません")
        XCTAssertTrue(backend.reports.isEmpty)
    }

    func testUnauthorizedRequiresReenrollment() async {
        store.token = "stale"
        backend.claimError = .unauthorized
        let model = makeModel()
        await model.checkNow()
        XCTAssertEqual(model.connection, .reenrollRequired)
    }

    func testFailedReportIsRetriedBeforeNextClaim() async {
        store.token = "device-token-1"
        backend.queued = [action("open_url", ["url": .string("https://example.com")], id: "e4")]
        backend.reportError = .network("ネットワークエラー")
        let model = makeModel()
        await model.checkNow()
        XCTAssertEqual(model.lastResult?.reported, false)
        backend.reportError = nil
        await model.checkNow()
        XCTAssertEqual(backend.reports.map(\.eventID), ["e4"])
        XCTAssertEqual(executor.plans.count, 1)
        XCTAssertEqual(model.lastResult?.reported, true)
    }

    func testUnenrollDeletesToken() {
        store.token = "device-token-1"
        let model = makeModel()
        model.unenroll()
        XCTAssertNil(store.token)
        XCTAssertEqual(model.connection, .notEnrolled)
    }
}

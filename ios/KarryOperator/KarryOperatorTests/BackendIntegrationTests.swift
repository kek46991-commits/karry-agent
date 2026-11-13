import XCTest
@testable import KarryOperator

/// Runs against a real karry-agent server when `KARRY_E2E_BASE_URL` and
/// `KARRY_E2E_OWNER_TOKEN` are set (pass them as `TEST_RUNNER_`-prefixed env vars to xcodebuild).
@MainActor
final class BackendIntegrationTests: XCTestCase {
    private var baseURL: URL!
    private var ownerToken: String!

    override func setUpWithError() throws {
        let env = ProcessInfo.processInfo.environment
        guard let base = env["KARRY_E2E_BASE_URL"], let owner = env["KARRY_E2E_OWNER_TOKEN"], let url = URL(string: base) else {
            throw XCTSkip("KARRY_E2E_BASE_URL / KARRY_E2E_OWNER_TOKEN not set")
        }
        baseURL = url
        ownerToken = owner
    }

    private func ownerRequest(_ path: String, _ body: [String: Any]) async throws -> [String: Any] {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(ownerToken!)", forHTTPHeaderField: "Authorization")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await URLSession.shared.data(for: request)
        XCTAssertTrue([200, 201].contains((response as? HTTPURLResponse)?.statusCode ?? 0), String(decoding: data, as: UTF8.self))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func ownerGet(_ path: String) async throws -> [String: Any] {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.setValue("Bearer \(ownerToken!)", forHTTPHeaderField: "Authorization")
        let (data, _) = try await URLSession.shared.data(for: request)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    func testEnrollClaimAndReportAgainstRealBackend() async throws {
        let deviceID = "e2e-" + UUID().uuidString.prefix(8).lowercased()
        let defaults = freshDefaults()
        let settings = OperatorSettings(defaults: defaults)
        settings.baseURL = baseURL.absoluteString
        settings.deviceID = deviceID
        let store = InMemoryTokenStore()
        let executor = RecordingExecutor()
        executor.summary = "URLを開きました（example.com）"
        let model = OperatorViewModel(settings: settings, tokenStore: store, executor: executor)

        await model.enroll(ownerToken: ownerToken)
        XCTAssertEqual(model.connection, .ready)
        XCTAssertNotNil(store.token)

        let submitted = try await ownerRequest("api/mobile-agent/actions", [
            "device_id": deviceID, "action": "open_url", "payload": ["url": "https://example.com"],
        ])
        let eventID = try XCTUnwrap(submitted["id"] as? String)
        XCTAssertEqual(submitted["status"] as? String, "awaiting_confirmation")
        let nothing = await model.checkNow()
        XCTAssertEqual(nothing, "実行待ちのアクションはありません")
        _ = try await ownerRequest("api/mobile-agent/actions/\(eventID)/confirm", ["device_id": deviceID])

        await model.checkNow()
        XCTAssertEqual(executor.plans, [.openURL(URL(string: "https://example.com")!)])
        XCTAssertEqual(model.lastResult?.reported, true)

        _ = try await ownerRequest("api/mobile-agent/actions", [
            "device_id": deviceID, "action": "choose_photo", "payload": ["selection_limit": 2],
        ])
        executor.failure = ActionFailure(.permissionDenied, "写真へのアクセスが許可されていません")
        await model.checkNow()
        XCTAssertEqual(model.lastResult?.reported, true)

        let listed = try await ownerGet("api/mobile-agent/devices/\(deviceID)/actions")
        let actions = try XCTUnwrap(listed["actions"] as? [[String: Any]])
        XCTAssertEqual(actions.compactMap { $0["status"] as? String }, ["failed", "completed"])
        let diagnostics = try await ownerGet("api/mobile-agent/devices/\(deviceID)/diagnostics")
        let first = try XCTUnwrap((diagnostics["diagnostics"] as? [[String: Any]])?.first)
        XCTAssertEqual((first["diagnostic"] as? [String: String])?["category"], "permission")

        store.token = "wrong-token"
        await model.checkNow()
        XCTAssertEqual(model.connection, .reenrollRequired)
    }
}

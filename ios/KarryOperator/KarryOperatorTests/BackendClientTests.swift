import XCTest
@testable import KarryOperator

final class BackendClientTests: XCTestCase {
    private var client: BackendClient!

    override func setUpWithError() throws {
        StubURLProtocol.reset()
        client = try BackendClient(baseURL: URL(string: "https://karry.example")!, session: StubURLProtocol.session())
    }

    override func tearDown() {
        StubURLProtocol.reset()
    }

    func testBaseURLPolicy() {
        XCTAssertTrue(BackendClient.isAllowedBaseURL(URL(string: "https://karry-agent-1.onrender.com")!))
        XCTAssertTrue(BackendClient.isAllowedBaseURL(URL(string: "http://localhost:8000")!))
        XCTAssertTrue(BackendClient.isAllowedBaseURL(URL(string: "http://127.0.0.1:8000")!))
        XCTAssertFalse(BackendClient.isAllowedBaseURL(URL(string: "http://karry.example")!))
        XCTAssertFalse(BackendClient.isAllowedBaseURL(URL(string: "https://u:p@karry.example")!))
        XCTAssertFalse(BackendClient.isAllowedBaseURL(URL(string: "ftp://karry.example")!))
        XCTAssertThrowsError(try BackendClient(baseURL: URL(string: "http://evil.example")!))
    }

    func testEnrollUsesOwnerBearerOnceAndReturnsDeviceToken() async throws {
        StubURLProtocol.handler = { _ in (201, Data(#"{"device_id":"test-iphone","device_token":"dev-token"}"#.utf8)) }
        let token = try await client.enroll(deviceID: "test-iphone", ownerToken: "owner-secret")
        XCTAssertEqual(token, "dev-token")
        let captured = try XCTUnwrap(StubURLProtocol.captured.first)
        XCTAssertEqual(captured.request.httpMethod, "POST")
        XCTAssertEqual(captured.request.url?.absoluteString, "https://karry.example/api/mobile-agent/devices/enroll")
        XCTAssertEqual(captured.request.value(forHTTPHeaderField: "Authorization"), "Bearer owner-secret")
        XCTAssertNil(captured.request.value(forHTTPHeaderField: "X-Device-Token"))
        XCTAssertEqual(try JSONSerialization.jsonObject(with: captured.body) as? [String: String], ["device_id": "test-iphone"])
    }

    func testClaimSendsDeviceTokenOnly() async throws {
        StubURLProtocol.handler = { _ in
            (200, Data(#"{"action":{"id":"e1","device_id":"test-iphone","action":"open_url","payload":{"url":"https://example.com"},"status":"claimed"}}"#.utf8))
        }
        let claimed = try await client.claim(deviceID: "test-iphone", deviceToken: "dev-token")
        XCTAssertEqual(claimed?.id, "e1")
        XCTAssertEqual(claimed?.payload["url"], .string("https://example.com"))
        let request = try XCTUnwrap(StubURLProtocol.captured.first?.request)
        XCTAssertEqual(request.url?.path, "/api/mobile-agent/devices/test-iphone/claim")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Device-Token"), "dev-token")
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
    }

    func testClaimNullActionMeansNothingPending() async throws {
        StubURLProtocol.handler = { _ in (200, Data(#"{"action":null}"#.utf8)) }
        let claimed = try await client.claim(deviceID: "test-iphone", deviceToken: "dev-token")
        XCTAssertNil(claimed)
    }

    func testClaimRejectsActionForAnotherDevice() async {
        StubURLProtocol.handler = { _ in
            (200, Data(#"{"action":{"id":"e1","device_id":"other","action":"open_url","payload":{}}}"#.utf8))
        }
        do {
            _ = try await client.claim(deviceID: "test-iphone", deviceToken: "dev-token")
            XCTFail("expected failure")
        } catch {
            XCTAssertEqual(error as? BackendError, .invalidResponse)
        }
    }

    func testUnauthorizedIsSurfaced() async {
        StubURLProtocol.handler = { _ in (401, Data(#"{"detail":"device authentication failed"}"#.utf8)) }
        do {
            _ = try await client.claim(deviceID: "test-iphone", deviceToken: "bad")
            XCTFail("expected failure")
        } catch {
            XCTAssertEqual(error as? BackendError, .unauthorized)
        }
    }

    func testInvalidDeviceIDIsRejectedBeforeNetwork() async {
        do {
            _ = try await client.claim(deviceID: "../../admin", deviceToken: "dev-token")
            XCTFail("expected failure")
        } catch {
            XCTAssertEqual(error as? BackendError, .invalidIdentifier)
        }
        XCTAssertTrue(StubURLProtocol.captured.isEmpty)
    }

    func testReportBody() async throws {
        StubURLProtocol.handler = { _ in (200, Data("{}".utf8)) }
        let report = ActionReport.failure(deviceID: "test-iphone", failure: ActionFailure(.permissionDenied, "拒否"))
        try await client.report(eventID: "e1", deviceToken: "dev-token", report: report)
        let captured = try XCTUnwrap(StubURLProtocol.captured.first)
        XCTAssertEqual(captured.request.url?.path, "/api/mobile-agent/actions/e1/report")
        XCTAssertEqual(captured.request.value(forHTTPHeaderField: "X-Device-Token"), "dev-token")
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: captured.body) as? [String: Any])
        XCTAssertEqual(body["device_id"] as? String, "test-iphone")
        XCTAssertEqual(body["success"] as? Bool, false)
        XCTAssertEqual(body["output"] as? String, "拒否")
        XCTAssertEqual(body["error_code"] as? String, "permission_denied")
        XCTAssertEqual(Set(body.keys), ["device_id", "success", "output", "error_code"])
    }
}

import Foundation
import XCTest
@testable import KarryOperator

final class InMemoryTokenStore: TokenStoring {
    var token: String?

    init(token: String? = nil) {
        self.token = token
    }

    func readToken() -> String? { token }
    func saveToken(_ token: String) throws { self.token = token }
    func deleteToken() throws { token = nil }
}

/// Captures requests and returns canned responses without touching the network.
final class StubURLProtocol: URLProtocol {
    struct Captured {
        let request: URLRequest
        let body: Data
    }

    static var handler: ((URLRequest) -> (Int, Data))?
    static var captured: [Captured] = []

    static func session() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    static func reset() {
        handler = nil
        captured = []
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        var body = request.httpBody ?? Data()
        if body.isEmpty, let stream = request.httpBodyStream {
            stream.open()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let read = stream.read(&buffer, maxLength: buffer.count)
                if read <= 0 { break }
                body.append(buffer, count: read)
            }
            stream.close()
        }
        Self.captured.append(Captured(request: request, body: body))
        let (status, data) = Self.handler?(request) ?? (500, Data())
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

@MainActor
final class FakeURLOpener: URLOpening {
    var opened: [URL] = []
    var result = true

    func open(_ url: URL) async -> Bool {
        opened.append(url)
        return result
    }
}

@MainActor
final class FakeEventWriter: EventWriting {
    var events: [CalendarEventDraft] = []
    var reminders: [ReminderDraft] = []
    var failure: ActionFailure?

    func createEvent(_ draft: CalendarEventDraft) async throws {
        if let failure { throw failure }
        events.append(draft)
    }

    func createReminder(_ draft: ReminderDraft) async throws {
        if let failure { throw failure }
        reminders.append(draft)
    }
}

@MainActor
final class FakeLocationProvider: LocationProviding {
    var accuracy: Double = 42
    var failure: ActionFailure?

    func requestOneLocation() async throws -> Double {
        if let failure { throw failure }
        return accuracy
    }
}

@MainActor
final class FakePromptPresenter: PromptPresenting {
    var canComposeMessages = true
    var result: PromptResult = .cancelled
    var presented: [InteractivePrompt] = []

    func present(_ prompt: InteractivePrompt) async -> PromptResult {
        presented.append(prompt)
        return result
    }
}

@MainActor
final class RecordingExecutor: ActionPerforming {
    var plans: [ActionPlan] = []
    var failure: ActionFailure?
    var summary = "完了しました"

    func perform(_ plan: ActionPlan) async throws -> String {
        plans.append(plan)
        if let failure { throw failure }
        return summary
    }
}

final class FakeBackend: BackendServing {
    var enrolledWith: (deviceID: String, ownerToken: String)?
    var enrollToken = "device-token-1"
    var queued: [MobileAction] = []
    var claimError: BackendError?
    var reportError: BackendError?
    var reports: [(eventID: String, token: String, report: ActionReport)] = []
    var claimTokens: [String] = []

    func enroll(deviceID: String, ownerToken: String) async throws -> String {
        enrolledWith = (deviceID, ownerToken)
        return enrollToken
    }

    func claim(deviceID: String, deviceToken: String) async throws -> MobileAction? {
        claimTokens.append(deviceToken)
        if let claimError { throw claimError }
        return queued.isEmpty ? nil : queued.removeFirst()
    }

    func report(eventID: String, deviceToken: String, report: ActionReport) async throws {
        if let reportError { throw reportError }
        reports.append((eventID, deviceToken, report))
    }
}

func action(_ name: String, _ payload: [String: JSONScalar], id: String = "evt1", device: String = "test-iphone") -> MobileAction {
    MobileAction(id: id, deviceID: device, action: name, payload: payload)
}

func freshDefaults(_ name: String = #function) -> UserDefaults {
    let suite = "KarryOperatorTests.\(name).\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    defaults.removePersistentDomain(forName: suite)
    return defaults
}

func assertFailure(_ code: ActionErrorCode, file: StaticString = #filePath, line: UInt = #line, _ body: () throws -> Void) {
    XCTAssertThrowsError(try body(), file: file, line: line) { error in
        XCTAssertEqual((error as? ActionFailure)?.code, code, "\(error)", file: file, line: line)
    }
}

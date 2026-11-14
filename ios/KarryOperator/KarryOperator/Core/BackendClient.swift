import Foundation

enum BackendError: Error, Equatable {
    case invalidBaseURL
    case invalidIdentifier
    case unauthorized
    case http(status: Int, detail: String)
    case network(String)
    case invalidResponse

    var localizedMessage: String {
        switch self {
        case .invalidBaseURL: return "サーバーURLはHTTPSで指定してください（localhostのみHTTP可）"
        case .invalidIdentifier: return "端末IDの形式が不正です"
        case .unauthorized: return "認証に失敗しました。端末を再登録してください"
        case .http(let status, let detail): return "サーバーエラー (\(status)) \(detail)"
        case .network(let message): return "通信できません: \(message)"
        case .invalidResponse: return "サーバーの応答を読み取れません"
        }
    }
}

protocol BackendServing {
    func enroll(deviceID: String, ownerToken: String) async throws -> String
    func claim(deviceID: String, deviceToken: String) async throws -> MobileAction?
    func report(eventID: String, deviceToken: String, report: ActionReport) async throws
}

/// HTTP client for the `/api/mobile-agent` routes. The owner token is only used
/// transiently for enrollment; all other calls use the device-scoped token.
struct BackendClient: BackendServing {
    static let maxResponseBytes = 64 * 1024
    private static let deviceIDPattern = try! NSRegularExpression(pattern: "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    private static let eventIDPattern = try! NSRegularExpression(pattern: "^[A-Za-z0-9_-]{1,64}$")

    let baseURL: URL
    let session: URLSession

    init(baseURL: URL, session: URLSession = .shared) throws {
        guard Self.isAllowedBaseURL(baseURL) else { throw BackendError.invalidBaseURL }
        self.baseURL = baseURL
        self.session = session
    }

    static func isAllowedBaseURL(_ url: URL) -> Bool {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let scheme = components.scheme?.lowercased(),
              let host = components.host?.lowercased(), !host.isEmpty,
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil else {
            return false
        }
        if scheme == "https" { return true }
        return scheme == "http" && ["localhost", "127.0.0.1", "::1"].contains(host)
    }

    static func isValidDeviceID(_ value: String) -> Bool {
        deviceIDPattern.firstMatch(in: value, range: NSRange(value.startIndex..., in: value)) != nil
    }

    private static func isValidEventID(_ value: String) -> Bool {
        eventIDPattern.firstMatch(in: value, range: NSRange(value.startIndex..., in: value)) != nil
    }

    func enroll(deviceID: String, ownerToken: String) async throws -> String {
        guard Self.isValidDeviceID(deviceID) else { throw BackendError.invalidIdentifier }
        var request = makeRequest(path: ["devices", "enroll"])
        request.setValue("Bearer \(ownerToken)", forHTTPHeaderField: "Authorization")
        request.httpBody = try JSONEncoder().encode(["device_id": deviceID])
        let data = try await send(request, expecting: 201)
        struct Enrollment: Decodable {
            let device_token: String
        }
        guard let enrollment = try? JSONDecoder().decode(Enrollment.self, from: data), !enrollment.device_token.isEmpty else {
            throw BackendError.invalidResponse
        }
        return enrollment.device_token
    }

    func claim(deviceID: String, deviceToken: String) async throws -> MobileAction? {
        guard Self.isValidDeviceID(deviceID) else { throw BackendError.invalidIdentifier }
        var request = makeRequest(path: ["devices", deviceID, "claim"])
        request.setValue(deviceToken, forHTTPHeaderField: "X-Device-Token")
        let data = try await send(request, expecting: 200)
        struct Claim: Decodable {
            let action: MobileAction?
        }
        guard let claim = try? JSONDecoder().decode(Claim.self, from: data) else {
            throw BackendError.invalidResponse
        }
        if let action = claim.action, action.deviceID != deviceID {
            throw BackendError.invalidResponse
        }
        return claim.action
    }

    func report(eventID: String, deviceToken: String, report: ActionReport) async throws {
        guard Self.isValidEventID(eventID), Self.isValidDeviceID(report.deviceID) else {
            throw BackendError.invalidIdentifier
        }
        var request = makeRequest(path: ["actions", eventID, "report"])
        request.setValue(deviceToken, forHTTPHeaderField: "X-Device-Token")
        request.httpBody = try JSONEncoder().encode(report)
        _ = try await send(request, expecting: 200)
    }

    func reportLocation(
        deviceID: String,
        deviceToken: String,
        latitude: Double,
        longitude: Double,
        accuracyM: Double,
        collectedAt: Date
    ) async throws {
        guard Self.isValidDeviceID(deviceID),
              (-90...90).contains(latitude),
              (-180...180).contains(longitude),
              (0...100_000).contains(accuracyM) else {
            throw BackendError.invalidIdentifier
        }
        struct LocationBody: Encodable {
            let latitude: Double
            let longitude: Double
            let accuracy_m: Double
            let collected_at: Double
        }
        var request = makeRequest(path: ["devices", deviceID, "location"])
        request.setValue(deviceToken, forHTTPHeaderField: "X-Device-Token")
        request.httpBody = try JSONEncoder().encode(
            LocationBody(
                latitude: latitude,
                longitude: longitude,
                accuracy_m: accuracyM,
                collected_at: collectedAt.timeIntervalSince1970
            )
        )
        _ = try await send(request, expecting: 200)
    }

    private func makeRequest(path: [String]) -> URLRequest {
        var url = baseURL.appendingPathComponent("api").appendingPathComponent("mobile-agent")
        for component in path {
            url.appendPathComponent(component)
        }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = Data("{}".utf8)
        return request
    }

    private func send(_ request: URLRequest, expecting expected: Int) async throws -> Data {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch let error as URLError {
            throw BackendError.network(error.code == .timedOut ? "タイムアウト" : "ネットワークエラー")
        } catch {
            throw BackendError.network("ネットワークエラー")
        }
        guard let http = response as? HTTPURLResponse else { throw BackendError.invalidResponse }
        guard data.count <= Self.maxResponseBytes else { throw BackendError.invalidResponse }
        if http.statusCode == 401 || http.statusCode == 403 { throw BackendError.unauthorized }
        guard http.statusCode == expected else {
            struct Detail: Decodable { let detail: String? }
            let detail = (try? JSONDecoder().decode(Detail.self, from: data))?.detail ?? ""
            throw BackendError.http(status: http.statusCode, detail: String(detail.prefix(120)))
        }
        return data
    }
}

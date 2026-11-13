import Foundation

/// Scalar payload value accepted by the backend (`_bounded_payload` only allows scalars).
enum JSONScalar: Codable, Equatable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Int.self) {
            self = .int(value)
        } else if let value = try? container.decode(Double.self) {
            self = .double(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "payload values must be scalar")
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value): try container.encode(value)
        case .int(let value): try container.encode(value)
        case .double(let value): try container.encode(value)
        case .bool(let value): try container.encode(value)
        case .null: try container.encodeNil()
        }
    }

    var stringValue: String? {
        if case .string(let value) = self { return value }
        return nil
    }
}

/// Action names allowlisted by `src/karry_agent/mobile_agents.py`.
enum ActionName: String, CaseIterable {
    case openApp = "open_app"
    case openURL = "open_url"
    case createCalendarEvent = "create_calendar_event"
    case createReminder = "create_reminder"
    case requestLocation = "request_location"
    case chooseFile = "choose_file"
    case choosePhoto = "choose_photo"
    case draftMessage = "draft_message"

    var displayName: String {
        switch self {
        case .openApp: return "アプリを開く"
        case .openURL: return "URLを開く"
        case .createCalendarEvent: return "カレンダー予定を作成"
        case .createReminder: return "リマインダーを作成"
        case .requestLocation: return "現在地を確認"
        case .chooseFile: return "ファイルを選択"
        case .choosePhoto: return "写真を選択"
        case .draftMessage: return "メッセージ下書き"
        }
    }

    var allowedFields: Set<String> {
        switch self {
        case .openApp: return ["bundle_id"]
        case .openURL: return ["url"]
        case .createCalendarEvent: return ["title", "start_at", "end_at", "notes"]
        case .createReminder: return ["title", "due_at", "notes"]
        case .requestLocation: return ["purpose"]
        case .chooseFile: return ["content_type"]
        case .choosePhoto: return ["selection_limit"]
        case .draftMessage: return ["recipient", "body"]
        }
    }
}

/// A claimed action event as returned by `POST /api/mobile-agent/devices/{id}/claim`.
struct MobileAction: Decodable, Equatable {
    let id: String
    let deviceID: String
    let action: String
    let payload: [String: JSONScalar]
    let status: String
    let requiresConfirmation: Bool
    let usesSystemPicker: Bool

    enum CodingKeys: String, CodingKey {
        case id
        case deviceID = "device_id"
        case action
        case payload
        case status
        case requiresConfirmation = "requires_confirmation"
        case usesSystemPicker = "uses_system_picker"
    }

    init(
        id: String,
        deviceID: String,
        action: String,
        payload: [String: JSONScalar],
        status: String = "claimed",
        requiresConfirmation: Bool = false,
        usesSystemPicker: Bool = false
    ) {
        self.id = id
        self.deviceID = deviceID
        self.action = action
        self.payload = payload
        self.status = status
        self.requiresConfirmation = requiresConfirmation
        self.usesSystemPicker = usesSystemPicker
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        deviceID = try container.decode(String.self, forKey: .deviceID)
        action = try container.decode(String.self, forKey: .action)
        payload = try container.decodeIfPresent([String: JSONScalar].self, forKey: .payload) ?? [:]
        status = try container.decodeIfPresent(String.self, forKey: .status) ?? "claimed"
        requiresConfirmation = try container.decodeIfPresent(Bool.self, forKey: .requiresConfirmation) ?? false
        usesSystemPicker = try container.decodeIfPresent(Bool.self, forKey: .usesSystemPicker) ?? false
    }

    var actionName: ActionName? { ActionName(rawValue: action) }

    var displayName: String { actionName?.displayName ?? "未対応のアクション" }
}

/// Error codes understood by the backend `_diagnose` categories.
enum ActionErrorCode: String {
    case permissionDenied = "permission_denied"
    case appUnavailable = "app_unavailable"
    case intentUnavailable = "intent_unavailable"
    case invalidPayload = "invalid_payload"
    case userCancelled = "user_cancelled"
    case timeout = "timeout"
    case offline = "offline"
    case executionFailed = "execution_failed"
}

struct ActionFailure: Error, Equatable {
    let code: ActionErrorCode
    let message: String

    init(_ code: ActionErrorCode, _ message: String) {
        self.code = code
        self.message = message
    }
}

/// The bounded result reported back to the backend.
struct ActionReport: Encodable, Equatable {
    static let maxOutputLength = 300

    let deviceID: String
    let success: Bool
    let output: String
    let errorCode: String

    enum CodingKeys: String, CodingKey {
        case deviceID = "device_id"
        case success
        case output
        case errorCode = "error_code"
    }

    init(deviceID: String, success: Bool, output: String, errorCode: String = "") {
        self.deviceID = deviceID
        self.success = success
        self.output = String(output.prefix(Self.maxOutputLength))
        self.errorCode = errorCode
    }

    static func success(deviceID: String, summary: String) -> ActionReport {
        ActionReport(deviceID: deviceID, success: true, output: summary)
    }

    static func failure(deviceID: String, failure: ActionFailure) -> ActionReport {
        ActionReport(deviceID: deviceID, success: false, output: failure.message, errorCode: failure.code.rawValue)
    }
}

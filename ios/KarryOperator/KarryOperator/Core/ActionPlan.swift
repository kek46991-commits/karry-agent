import Foundation
import UniformTypeIdentifiers

struct CalendarEventDraft: Equatable {
    let title: String
    let start: Date
    let end: Date
    let notes: String?
}

struct ReminderDraft: Equatable {
    let title: String
    let due: Date?
    let notes: String?
}

/// A fully validated, typed instruction. Payload text is only ever used as data
/// (titles, bodies, URLs) and is never evaluated.
enum ActionPlan: Equatable {
    case openApp(bundleID: String, url: URL?)
    case openURL(URL)
    case createCalendarEvent(CalendarEventDraft)
    case createReminder(ReminderDraft)
    case requestLocation(purpose: String)
    case chooseFile(contentTypes: [UTType])
    case choosePhoto(limit: Int)
    case draftMessage(recipients: [String], body: String)
}

enum ActionPlanner {
    static let ownBundleID = AppIdentity.bundleIdentifier

    /// Fixed mapping from allowlisted bundle IDs to public URL schemes. iOS does not
    /// allow opening arbitrary apps by bundle ID, so anything else is unavailable.
    static let openableApps: [String: String] = [
        "com.apple.mobilecal": "calshow://",
        "com.apple.reminders": "x-apple-reminderkit://",
        "com.apple.Maps": "maps://",
        "com.apple.MobileSMS": "sms:",
        "com.apple.mobilemail": "message://",
        "com.apple.shortcuts": "shortcuts://",
        "com.apple.Music": "music://",
        "com.apple.Preferences": "app-settings:",
    ]

    static let maxTextLength = 1000
    private static let bundleIDPattern = try! NSRegularExpression(pattern: "^[A-Za-z0-9][A-Za-z0-9.-]{2,199}$")
    private static let recipientPattern = try! NSRegularExpression(pattern: "^[A-Za-z0-9+@._() -]{1,100}$")

    static func plan(for action: MobileAction) throws -> ActionPlan {
        guard let name = action.actionName else {
            throw ActionFailure(.invalidPayload, "未対応のアクションです")
        }
        let payload = action.payload
        let unexpected = Set(payload.keys).subtracting(name.allowedFields)
        guard unexpected.isEmpty else {
            throw ActionFailure(.invalidPayload, "未対応のフィールドがあります: \(unexpected.sorted().joined(separator: ", "))")
        }
        for value in payload.values {
            if case .string(let text) = value, text.count > maxTextLength {
                throw ActionFailure(.invalidPayload, "テキストが長すぎます")
            }
        }

        switch name {
        case .openApp:
            let bundleID = try requiredText(payload, "bundle_id")
            guard matches(bundleIDPattern, bundleID) else {
                throw ActionFailure(.invalidPayload, "bundle_id が不正です")
            }
            if bundleID == ownBundleID {
                return .openApp(bundleID: bundleID, url: nil)
            }
            guard let scheme = openableApps[bundleID], let url = URL(string: scheme) else {
                throw ActionFailure(.appUnavailable, "このアプリは許可リストにないため開けません")
            }
            return .openApp(bundleID: bundleID, url: url)

        case .openURL:
            return .openURL(try httpsURL(try requiredText(payload, "url")))

        case .createCalendarEvent:
            let title = try requiredText(payload, "title")
            guard let start = try date(payload, "start_at") else {
                throw ActionFailure(.invalidPayload, "start_at が必要です")
            }
            let end = try date(payload, "end_at") ?? start.addingTimeInterval(3600)
            guard end >= start else {
                throw ActionFailure(.invalidPayload, "end_at は start_at 以降にしてください")
            }
            return .createCalendarEvent(CalendarEventDraft(title: title, start: start, end: end, notes: optionalText(payload, "notes")))

        case .createReminder:
            let title = try requiredText(payload, "title")
            return .createReminder(ReminderDraft(title: title, due: try date(payload, "due_at"), notes: optionalText(payload, "notes")))

        case .requestLocation:
            return .requestLocation(purpose: try requiredText(payload, "purpose"))

        case .chooseFile:
            return .chooseFile(contentTypes: [try contentType(payload["content_type"])])

        case .choosePhoto:
            let limit: Int
            switch payload["selection_limit"] {
            case nil, .some(.null): limit = 1
            case .some(.int(let value)): limit = value
            default: throw ActionFailure(.invalidPayload, "selection_limit は整数で指定してください")
            }
            guard (1...20).contains(limit) else {
                throw ActionFailure(.invalidPayload, "selection_limit は1〜20で指定してください")
            }
            return .choosePhoto(limit: limit)

        case .draftMessage:
            let body = try requiredText(payload, "body")
            var recipients: [String] = []
            if let raw = optionalText(payload, "recipient") {
                recipients = raw.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
                guard recipients.count <= 5, recipients.allSatisfy({ matches(recipientPattern, $0) }) else {
                    throw ActionFailure(.invalidPayload, "宛先の形式が不正です")
                }
            }
            return .draftMessage(recipients: recipients, body: body)
        }
    }

    static func httpsURL(_ text: String) throws -> URL {
        guard let components = URLComponents(string: text),
              components.scheme?.lowercased() == "https",
              let host = components.host, !host.isEmpty,
              components.user == nil, components.password == nil,
              let url = components.url else {
            throw ActionFailure(.invalidPayload, "HTTPSのURLのみ開けます")
        }
        return url
    }

    private static func requiredText(_ payload: [String: JSONScalar], _ key: String) throws -> String {
        guard let text = optionalText(payload, key) else {
            throw ActionFailure(.invalidPayload, "\(key) が必要です")
        }
        return text
    }

    private static func optionalText(_ payload: [String: JSONScalar], _ key: String) -> String? {
        guard let text = payload[key]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else {
            return nil
        }
        return text
    }

    private static func matches(_ regex: NSRegularExpression, _ text: String) -> Bool {
        regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) != nil
    }

    private static func contentType(_ value: JSONScalar?) throws -> UTType {
        guard let raw = value?.stringValue?.trimmingCharacters(in: .whitespaces), !raw.isEmpty else {
            return .item
        }
        if let type = UTType(raw) ?? UTType(mimeType: raw) ?? UTType(filenameExtension: raw.trimmingCharacters(in: CharacterSet(charactersIn: "."))) {
            return type
        }
        throw ActionFailure(.invalidPayload, "content_type が不正です")
    }

    static func date(_ payload: [String: JSONScalar], _ key: String) throws -> Date? {
        switch payload[key] {
        case nil, .some(.null):
            return nil
        case .some(.int(let seconds)):
            return Date(timeIntervalSince1970: TimeInterval(seconds))
        case .some(.double(let seconds)):
            return Date(timeIntervalSince1970: seconds)
        case .some(.string(let text)):
            if let parsed = parseDate(text.trimmingCharacters(in: .whitespaces)) { return parsed }
            throw ActionFailure(.invalidPayload, "\(key) の日時形式が不正です")
        case .some(.bool):
            throw ActionFailure(.invalidPayload, "\(key) の日時形式が不正です")
        }
    }

    static func parseDate(_ text: String) -> Date? {
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = iso.date(from: text) { return date }
        iso.formatOptions = [.withInternetDateTime]
        if let date = iso.date(from: text) { return date }
        let local = DateFormatter()
        local.locale = Locale(identifier: "en_US_POSIX")
        local.timeZone = .current
        for format in ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm", "yyyy-MM-dd HH:mm", "yyyy-MM-dd"] {
            local.dateFormat = format
            if let date = local.date(from: text) { return date }
        }
        return nil
    }
}

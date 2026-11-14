import Foundation
import Security

/// State shared between the 作業用 app and its screen-broadcast extension through the App Group.
/// The device token lives in the shared keychain, never in plain UserDefaults.
enum SharedStore {
    private static let service = "karry-device"
    private static let account = "token"
    private static let sharingKey = "sharingAt"
    private static let stopKey = "stopAt"

    static var appGroup: String { info("KarryAppGroup") }
    static var serverURL: URL { URL(string: info("KarryServerURL")) ?? URL(string: "https://karry-agent-1.onrender.com")! }
    private static var defaults: UserDefaults? { appGroup.isEmpty ? nil : UserDefaults(suiteName: appGroup) }

    private static func info(_ key: String) -> String {
        (Bundle.main.object(forInfoDictionaryKey: key) as? String) ?? ""
    }

    private static func query() -> [String: Any] {
        var query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: service,
                                    kSecAttrAccount as String: account]
        if !appGroup.isEmpty { query[kSecAttrAccessGroup as String] = appGroup }
        return query
    }

    static var token: String? {
        get {
            var query = query()
            query[kSecReturnData as String] = true
            query[kSecMatchLimit as String] = kSecMatchLimitOne
            var item: CFTypeRef?
            guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }
            return String(data: data, encoding: .utf8)
        }
        set {
            SecItemDelete(query() as CFDictionary)
            guard let newValue, let data = newValue.data(using: .utf8) else { return }
            var item = query()
            item[kSecValueData as String] = data
            item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            SecItemAdd(item as CFDictionary, nil)
        }
    }

    /// The extension stamps this every few seconds; a stale stamp means sharing ended (or the extension was killed).
    static var isSharing: Bool {
        guard let at = defaults?.object(forKey: sharingKey) as? Double else { return false }
        return Date().timeIntervalSince1970 - at < 10
    }
    static func markSharing() { defaults?.set(Date().timeIntervalSince1970, forKey: sharingKey) }
    static func clearSharing() { defaults?.removeObject(forKey: sharingKey) }

    static var stopRequested: Bool { defaults?.object(forKey: stopKey) != nil }
    static func requestStop() { defaults?.set(Date().timeIntervalSince1970, forKey: stopKey) }
    static func clearStop() { defaults?.removeObject(forKey: stopKey) }
}

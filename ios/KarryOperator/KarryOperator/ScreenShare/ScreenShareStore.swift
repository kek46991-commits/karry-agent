import Foundation
import Security

/// Shares the device registration with the screen-broadcast extension through the team keychain
/// access group (first entry of keychain-access-groups), so no App Group capability is required.
enum ScreenShareStore {
    private static let service = "karry-mobile-screen-share"

    static var serverURL: URL? { read("server-url").flatMap(URL.init(string:)) }
    static var deviceID: String? { read("device-id") }
    static var token: String? { read("device-token") }

    static func configure(serverURL: String, deviceID: String, token: String?) {
        write("server-url", serverURL)
        write("device-id", deviceID)
        write("device-token", token)
    }

    static func clear() {
        for account in ["server-url", "device-id", "device-token", "sharing-at"] { write(account, nil) }
    }

    static var isSharing: Bool {
        guard let value = read("sharing-at"), let stamp = Double(value) else { return false }
        return Date().timeIntervalSince1970 - stamp < 10
    }

    static func markSharing() { write("sharing-at", String(Date().timeIntervalSince1970)) }
    static func clearSharing() { write("sharing-at", nil) }

    private static func query(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    private static func read(_ account: String) -> String? {
        var query = query(account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data, let text = String(data: data, encoding: .utf8), !text.isEmpty else { return nil }
        return text
    }

    private static func write(_ account: String, _ value: String?) {
        SecItemDelete(query(account) as CFDictionary)
        guard let value, !value.isEmpty else { return }
        var item = query(account)
        item[kSecValueData as String] = Data(value.utf8)
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(item as CFDictionary, nil)
    }
}

import Foundation
import Security

protocol TokenStoring {
    func readToken() -> String?
    func saveToken(_ token: String) throws
    func deleteToken() throws
}

enum KeychainError: Error, Equatable {
    case unexpectedStatus(OSStatus)
}

/// Stores only the device-scoped token, in this device's Keychain, never synced.
struct KeychainTokenStore: TokenStoring {
    let service: String
    let account: String

    init(service: String = "io.github.kek46991.KarryOperator.device-token", account: String = "device_token") {
        self.service = service
        self.account = account
    }

    private var baseQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    func readToken() -> String? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let token = String(data: data, encoding: .utf8), !token.isEmpty else {
            return nil
        }
        return token
    }

    func saveToken(_ token: String) throws {
        let data = Data(token.utf8)
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let status = SecItemUpdate(baseQuery as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            var query = baseQuery
            query.merge(attributes) { _, new in new }
            let addStatus = SecItemAdd(query as CFDictionary, nil)
            guard addStatus == errSecSuccess else { throw KeychainError.unexpectedStatus(addStatus) }
        } else if status != errSecSuccess {
            throw KeychainError.unexpectedStatus(status)
        }
    }

    func deleteToken() throws {
        let status = SecItemDelete(baseQuery as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError.unexpectedStatus(status)
        }
    }
}

/// Non-secret settings. The owner token is intentionally never stored here.
struct OperatorSettings {
    static let defaultBaseURL = "https://karry-agent-1.onrender.com"
    private static let baseURLKey = "karry.operator.baseURL"
    private static let deviceIDKey = "karry.operator.deviceID"

    let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    var baseURL: String {
        get { defaults.string(forKey: Self.baseURLKey) ?? Self.defaultBaseURL }
        nonmutating set { defaults.set(newValue, forKey: Self.baseURLKey) }
    }

    var deviceID: String {
        get {
            if let saved = defaults.string(forKey: Self.deviceIDKey), !saved.isEmpty { return saved }
            let generated = "iphone-" + UUID().uuidString.prefix(8).lowercased()
            defaults.set(generated, forKey: Self.deviceIDKey)
            return generated
        }
        nonmutating set { defaults.set(newValue, forKey: Self.deviceIDKey) }
    }
}

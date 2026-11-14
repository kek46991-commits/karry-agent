import Foundation

enum AppIdentity {
#if KARRY_AGENT_4
    static let displayName = "Karry-Agent 4"
    static let fallbackBundleIdentifier = "io.github.kek46991.KarryOperator4"
    static let storageNamespace = "karry.operator4"
    static let defaultDeviceIDPrefix = "operation-iphone-4"
#else
    static let displayName = "Karry-Agent 2"
    static let fallbackBundleIdentifier = "io.github.kek46991.KarryOperator"
    static let storageNamespace = "karry.operator"
    static let defaultDeviceIDPrefix = "operation-iphone"
#endif

    /// The bundle ID the app was actually signed with, so custom IDs keep Keychain and self-open consistent.
    static let bundleIdentifier = resolveBundleIdentifier(Bundle.main.bundleIdentifier)
    static let deviceTokenService = "\(bundleIdentifier).device-token"

    static func resolveBundleIdentifier(_ runtimeValue: String?) -> String {
        let trimmed = runtimeValue?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return trimmed.isEmpty ? fallbackBundleIdentifier : trimmed
    }
}

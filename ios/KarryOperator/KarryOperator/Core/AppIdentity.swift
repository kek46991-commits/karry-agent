import Foundation

enum AppIdentity {
#if KARRY_AGENT_4
    static let displayName = "Karry-Agent 4"
    static let bundleIdentifier = "io.github.kek46991.KarryOperator4"
    static let storageNamespace = "karry.operator4"
    static let defaultDeviceIDPrefix = "operation-iphone-4"
#else
    static let displayName = "Karry-Agent 2"
    static let bundleIdentifier = "io.github.kek46991.KarryOperator"
    static let storageNamespace = "karry.operator"
    static let defaultDeviceIDPrefix = "operation-iphone"
#endif

    static let deviceTokenService = "\(bundleIdentifier).device-token"
}

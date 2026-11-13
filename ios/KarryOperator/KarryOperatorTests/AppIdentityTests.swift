import XCTest
@testable import KarryOperator

final class AppIdentityTests: XCTestCase {
    func testUsesTheBundleIDTheAppWasBuiltWith() throws {
        let runtime = try XCTUnwrap(Bundle.main.bundleIdentifier)
        XCTAssertEqual(AppIdentity.bundleIdentifier, runtime)
        XCTAssertEqual(AppIdentity.deviceTokenService, "\(runtime).device-token")
        XCTAssertEqual(KeychainTokenStore().service, "\(runtime).device-token")
        XCTAssertEqual(ActionPlanner.ownBundleID, runtime)
        XCTAssertEqual(
            try ActionPlanner.plan(for: action("open_app", ["bundle_id": .string(runtime)])),
            .openApp(bundleID: runtime, url: nil)
        )
        if let expected = ProcessInfo.processInfo.environment["KARRY_EXPECTED_BUNDLE_ID"] {
            XCTAssertEqual(runtime, expected)
        }
    }

    func testFallsBackWhenRuntimeBundleIDIsMissing() {
        XCTAssertEqual(AppIdentity.resolveBundleIdentifier(nil), AppIdentity.fallbackBundleIdentifier)
        XCTAssertEqual(AppIdentity.resolveBundleIdentifier(""), AppIdentity.fallbackBundleIdentifier)
        XCTAssertEqual(AppIdentity.resolveBundleIdentifier("  \n"), AppIdentity.fallbackBundleIdentifier)
        XCTAssertEqual(AppIdentity.resolveBundleIdentifier("com.kishi.karryagent2"), "com.kishi.karryagent2")
    }
}

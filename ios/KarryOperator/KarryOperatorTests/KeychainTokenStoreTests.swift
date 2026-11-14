import XCTest
@testable import KarryOperator

final class KeychainTokenStoreTests: XCTestCase {
    func testRoundTripUpdateAndDelete() throws {
        let store = KeychainTokenStore(service: "io.github.kek46991.KarryOperator.tests.\(UUID().uuidString)")
        defer { try? store.deleteToken() }
        XCTAssertNil(store.readToken())
        try store.saveToken("first")
        XCTAssertEqual(store.readToken(), "first")
        try store.saveToken("second")
        XCTAssertEqual(store.readToken(), "second")
        try store.deleteToken()
        XCTAssertNil(store.readToken())
        XCTAssertNoThrow(try store.deleteToken())
    }
}

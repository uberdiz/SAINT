import XCTest
@testable import SaintCore

/// The phone's log as the PC collects it (`log.get`): numbered lines, "after N", a new run starts again.
final class AppLogTests: XCTestCase {
    func testLinesAfterACursorAndWhatWasDropped() {
        let log = AppLog(capacity: 10)
        for i in 0..<15 { log.info("test", "line \(i)") }
        let all = log.since(after: 0)
        let lines = all["lines"] as? [String] ?? []
        XCTAssertEqual(lines.count, 10)
        XCTAssertTrue(lines.first?.hasSuffix("ios.test: line 5") ?? false)
        XCTAssertEqual(all["cursor"] as? Int, 15)
        XCTAssertEqual(all["dropped"] as? Int, 5)
        XCTAssertEqual((log.since(after: 15)["lines"] as? [String])?.count, 0)
        XCTAssertEqual((log.since(after: 13)["lines"] as? [String])?.count, 2)
    }

    func testAnotherRunsCursorStartsOver() {
        let log = AppLog(capacity: 10)
        log.info("test", "a")
        log.info("test", "b")
        XCTAssertEqual((log.since(after: 2, boot: "not-this-run")["lines"] as? [String])?.count, 2)
        XCTAssertEqual((log.since(after: 99)["lines"] as? [String])?.count, 2)
        XCTAssertEqual(log.since(after: 0)["boot"] as? String, log.boot)
    }
}

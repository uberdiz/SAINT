import XCTest
@testable import SaintCore

/// Fifty phrases the desktop's reminder parser understands, with what it makes of each at a fixed "now"
/// (Wednesday 2026-09-30, noon, local time). tests/data/time_cases.json is written by the Python side.
final class TimeParseTests: XCTestCase {
    private let calendar = Calendar.current

    private func date(_ c: [String: Any]) -> Date {
        var parts = DateComponents()
        parts.year = c["year"] as? Int
        parts.month = c["month"] as? Int
        parts.day = c["day"] as? Int
        parts.hour = c["hour"] as? Int
        parts.minute = c["minute"] as? Int
        parts.second = 0
        return calendar.date(from: parts) ?? Date()
    }

    func testTheDesktopsReferenceCases() throws {
        let file = try TestData.object("time_cases")
        let now = date(try XCTUnwrap(file["now"] as? [String: Any]))
        let cases = try XCTUnwrap(file["cases"] as? [[String: Any]])
        XCTAssertGreaterThanOrEqual(cases.count, 50)
        for c in cases {
            let text = c["text"] as? String ?? ""
            let parsed = TimeParse.extractReminder(text, now: now)
            if c["reminder"] is NSNull || c["reminder"] == nil {
                XCTAssertNil(parsed, "“\(text)” should not be a reminder")
                continue
            }
            let expected = try XCTUnwrap(c["reminder"] as? [String: Any])
            if expected["error"] != nil {                       // the desktop raises on an impossible time ("at 25")
                XCTAssertTrue(parsed == nil || parsed?.schedule == nil, text)
                continue
            }
            let reminder = try XCTUnwrap(parsed, "“\(text)” should be a reminder")
            XCTAssertEqual(reminder.message, expected["message"] as? String, "“\(text)” message")
            XCTAssertEqual(reminder.isTimer, expected["timer"] as? Bool, "“\(text)” timer")
            guard let want = expected["schedule"] as? [String: Any] else {
                XCTAssertNil(reminder.schedule, "“\(text)” should have no time")
                continue
            }
            let schedule = try XCTUnwrap(reminder.schedule, "“\(text)” should have a time")
            switch want["type"] as? String {
            case "once":
                guard case .once(let at) = schedule else { XCTFail("“\(text)” should be a one-off"); continue }
                XCTAssertEqual(at, date(want), "“\(text)” time")
            case "interval":
                guard case .interval(let every, _) = schedule else { XCTFail("“\(text)” should repeat every so often"); continue }
                XCTAssertEqual(every, want["every_sec"] as? Int, "“\(text)” interval")
            case "daily":
                guard case .daily(let time, let days) = schedule else { XCTFail("“\(text)” should be daily"); continue }
                XCTAssertEqual(time, want["time"] as? String, "“\(text)” time of day")
                XCTAssertEqual(days, want["days"] as? [Int], "“\(text)” days")
            default:
                XCTFail("unknown schedule type in the test data")
            }
        }
    }

    func testOverlappingTimeWordsAreCutOutOnlyOnce() throws {
        // "this evening" contains "evening": removing both used to leave "Ll mom"
        let now = date(["year": 2026, "month": 9, "day": 30, "hour": 12, "minute": 0])
        let reminder = try XCTUnwrap(TimeParse.extractReminder("remind me this evening to call mom", now: now))
        XCTAssertEqual(reminder.message.lowercased(), "call mom")
    }

    func testSchedulesSurviveAJSONRoundTrip() throws {
        let day = Date(timeIntervalSince1970: 1_800_000_000)
        for schedule in [Schedule.once(at: day), .interval(everySec: 7200, start: day), .daily(time: "07:30", days: [0, 2, 4]),
                         .daily(time: "21:00", days: nil)] {
            let data = try JSONEncoder().encode(schedule)
            XCTAssertEqual(try JSONDecoder().decode(Schedule.self, from: data), schedule)
            XCTAssertEqual(Schedule(json: schedule.json), schedule)
        }
    }

    func testNextRun() {
        let now = date(["year": 2026, "month": 9, "day": 30, "hour": 12, "minute": 0])
        XCTAssertEqual(TimeParse.nextRun(.daily(time: "15:00", days: nil), after: now), date(["year": 2026, "month": 9, "day": 30, "hour": 15, "minute": 0]))
        XCTAssertEqual(TimeParse.nextRun(.daily(time: "09:00", days: nil), after: now), date(["year": 2026, "month": 10, "day": 1, "hour": 9, "minute": 0]))
        XCTAssertNil(TimeParse.nextRun(.once(at: now.addingTimeInterval(-60)), after: now))
        let start = now.addingTimeInterval(-90)
        XCTAssertEqual(TimeParse.nextRun(.interval(everySec: 60, start: start), after: now), start.addingTimeInterval(120))
    }

    func testDescribe() {
        let now = date(["year": 2026, "month": 9, "day": 30, "hour": 12, "minute": 0])
        XCTAssertEqual(TimeParse.describe(.once(at: date(["year": 2026, "month": 9, "day": 30, "hour": 17, "minute": 0])), now: now), "today at 5:00 PM")
        XCTAssertEqual(TimeParse.describe(.once(at: date(["year": 2026, "month": 10, "day": 1, "hour": 9, "minute": 30])), now: now), "tomorrow at 9:30 AM")
        XCTAssertEqual(TimeParse.describe(.daily(time: "08:30", days: [0, 1, 2, 3, 4]), now: now), "every weekday at 8:30 AM")
        XCTAssertEqual(TimeParse.describe(.interval(everySec: 7200, start: now), now: now), "every 2 hours")
    }
}

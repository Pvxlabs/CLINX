import XCTest
@testable import CLINXMonitor

final class PresentationPerformanceTests: XCTestCase {
    func testRepeatedTimelineDerivation() {
        let tasks = SyntheticTasks.all()
        measure {
            for _ in 0..<10 {
                for task in tasks {
                    _ = task.timeline
                    _ = task.lastProgressMinutes
                    _ = task.durationText
                }
            }
        }
    }

    func testRepeatedTimestampParsingPreservesOffsetsFractionsAndInvalidValues() throws {
        let utc = "2026-10-02T03:15:34.123456Z"
        let offset = "2026-10-02T11:15:34.123456+08:00"
        let first = try XCTUnwrap(TimestampParser.date(from: utc))
        for _ in 0..<20 {
            XCTAssertEqual(TimestampParser.date(from: utc), first)
            XCTAssertEqual(TimestampParser.date(from: offset), first)
            XCTAssertNil(TimestampParser.date(from: "not-a-date"))
            XCTAssertNil(TimestampParser.date(from: nil))
        }
        XCTAssertEqual(try XCTUnwrap(TimestampParser.date(from: "2026-10-02T03:15:35Z"))
            .timeIntervalSince(first), 0.877, accuracy: 0.001)
    }

    func testUnixSecondsPreserveWholeAndFractionalTimes() throws {
        for seconds in ["0", "1789030437", "1789030437.125"] {
            let date = try XCTUnwrap(TimestampParser.date(from: seconds))
            XCTAssertEqual(date.timeIntervalSince1970, try XCTUnwrap(Double(seconds)), accuracy: 0.000001)
            XCTAssertEqual(TimestampParser.date(from: seconds), date)
        }
        XCTAssertEqual(TimestampParser.date(from: "0"),
                       TimestampParser.date(from: "1970-01-01T00:00:00Z"))
    }

    func testUnixTimesProduceTaskDurationAndLastProgress() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "api-examples", withExtension: "json"))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        let examples = try XCTUnwrap(root["examples"] as? [[String: Any]])
        let example = try XCTUnwrap(examples.first { $0["name"] as? String == "pass_detail" })
        var raw = try XCTUnwrap(example["response"] as? [String: Any])
        var times = try XCTUnwrap(raw["timestamps"] as? [String: Any])
        times["started_at"] = "1789030437"
        times["completed_at"] = "1789030852"
        times["last_progress_at"] = "1789030852"
        raw["timestamps"] = times
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let task = try decoder.decode(ObservedTask.self, from: JSONSerialization.data(withJSONObject: raw))
        XCTAssertEqual(task.durationText, "6m 55s")
        XCTAssertEqual(try XCTUnwrap(task.lastProgressDate).timeIntervalSince1970, 1789030852)
        XCTAssertEqual(RelativeTime.ago(since: task.lastProgressDate,
                                        now: Date(timeIntervalSince1970: 1789038052)), "2h ago")
        XCTAssertEqual(RelativeTime.minutes(since: task.lastProgressDate,
                                            now: Date(timeIntervalSince1970: 1789038052)), 120)
        XCTAssertNotEqual(task.rowTail(status: .failed), "Failed —")
    }

    func testMissingInvalidAndNonFiniteTimesStayUnavailable() {
        for value in [nil, "", "not-a-date", "NaN", "inf", "-inf", "1e300"] as [String?] {
            XCTAssertNil(TimestampParser.date(from: value))
        }
    }
}

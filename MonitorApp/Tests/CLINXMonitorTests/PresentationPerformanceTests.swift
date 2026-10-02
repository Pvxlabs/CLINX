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
}

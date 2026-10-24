import Foundation

/// Scheduled idle time and snapshot expiry must use the same polling cadence.
/// A healthy low-power poll waits 15 seconds; it cannot expire after 10 seconds.
enum MonitorPollingPolicy {
    static func interval(hasActiveTasks: Bool, lowPowerMode: Bool) -> TimeInterval {
        lowPowerMode || !hasActiveTasks ? 15 : 2
    }

    static func freshnessLimit(hasActiveTasks: Bool, lowPowerMode: Bool) -> TimeInterval {
        max(3 * interval(hasActiveTasks: hasActiveTasks, lowPowerMode: lowPowerMode), 10)
    }
}

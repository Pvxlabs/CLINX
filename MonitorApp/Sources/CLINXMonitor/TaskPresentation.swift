import Foundation

// Presentation-only derivation from the canonical Observer models.
// The wire contract is untouched: this file never invents fields, never infers success,
// and never exposes raw payloads.

enum TimestampParser {
    // Snapshot timestamps recur across rows, filters and inspector updates. Parse each
    // immutable value once instead of doing ICU work repeatedly on the UI thread.
    private static let parsedDates: NSCache<NSString, NSDate> = {
        let cache = NSCache<NSString, NSDate>()
        cache.countLimit = 2048
        return cache
    }()

    private static let withFraction: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSXXXXX"
        return formatter
    }()

    private static let plain: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ssXXXXX"
        return formatter
    }()

    /// The Observer emits microseconds; `DateFormatter` wants exactly three fraction digits,
    /// so the fraction is normalised before parsing.
    static func date(from value: String?) -> Date? {
        guard let value, !value.isEmpty else { return nil }
        if let date = parsedDates.object(forKey: value as NSString) { return date as Date }
        var normalized = value
        if let dot = value.firstIndex(of: ".") {
            var index = value.index(after: dot)
            var kept = 0
            while index < value.endIndex, value[index].isNumber {
                if kept < 3 { index = value.index(after: index); kept += 1 }
                else { break }
            }
            var tail = index
            while tail < value.endIndex, value[tail].isNumber { tail = value.index(after: tail) }
            let fraction = value[value.index(after: dot)..<index]
            normalized = String(value[value.startIndex..<dot]) + "." + fraction + String(value[tail...])
        }
        let date = withFraction.date(from: normalized) ?? plain.date(from: normalized)
        if let date { parsedDates.setObject(date as NSDate, forKey: value as NSString) }
        return date
    }
}

enum RelativeTime {
    private static let clock: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "HH:mm:ss"
        return formatter
    }()

    static func clock(_ date: Date?) -> String {
        guard let date else { return "—" }
        return clock.string(from: date)
    }

    static func ago(since date: Date?, now: Date = Date()) -> String {
        guard let date else { return "—" }
        let seconds = max(0, Int(now.timeIntervalSince(date)))
        if seconds < 60 { return "just now" }
        let minutes = seconds / 60
        if minutes < 60 { return "\(minutes)m ago" }
        let hours = minutes / 60
        if hours < 24 { return "\(hours)h ago" }
        return "\(hours / 24)d ago"
    }

    static func minutes(since date: Date?, now: Date = Date()) -> Int? {
        guard let date else { return nil }
        return max(0, Int(now.timeIntervalSince(date)) / 60)
    }

    static func duration(from start: Date?, to end: Date?, now: Date = Date()) -> String {
        guard let start else { return "—" }
        let seconds = max(0, Int((end ?? now).timeIntervalSince(start)))
        if seconds < 60 { return "\(seconds)s" }
        let minutes = seconds / 60
        if minutes < 60 { return String(format: "%dm %02ds", minutes, seconds % 60) }
        return String(format: "%dh %02dm", minutes / 60, minutes % 60)
    }
}

extension ObservedTask {
    var startedDate: Date? { TimestampParser.date(from: timestamps.startedAt ?? timestamps.createdAt) }
    var lastProgressDate: Date? { TimestampParser.date(from: timestamps.lastProgressAt) }
    var completedDate: Date? { TimestampParser.date(from: timestamps.completedAt) }

    var titleText: String { title?.isEmpty == false ? title! : taskRef }

    var projectText: String { project?.isEmpty == false ? project! : "unassigned" }

    var hostText: String {
        if let host, !host.isEmpty { return host }
        if let identifier = routing.host.identifier, !identifier.isEmpty { return identifier }
        return "—"
    }

    var providerText: String {
        if let identifier = routing.provider.identifier, !identifier.isEmpty { return identifier }
        if let surface = routing.surface.identifier, !surface.isEmpty { return surface }
        return routing.provider.status
    }

    var executionText: String { executionRef ?? "—" }

    var activityText: String { currentActivity.label.isEmpty ? currentActivity.kind : currentActivity.label }

    var modelText: String { model.resolved ?? model.logical ?? "—" }

    var lastProgressText: String { RelativeTime.ago(since: lastProgressDate) }
    var lastProgressMinutes: Int? { RelativeTime.minutes(since: lastProgressDate) }
    var startedText: String { RelativeTime.clock(startedDate) }
    var durationText: String { RelativeTime.duration(from: startedDate, to: completedDate) }

    /// Row trailing text: attention rows lead with Blocked/Failed, completed rows with Done.
    func rowTail(status: MonitorStatus) -> String {
        switch status {
        case .blocked: return "Blocked \(lastProgressText)"
        case .failed: return "Failed \(lastProgressText)"
        case .completed: return "Done \(lastProgressText)"
        case .cancelled: return "Cancelled \(lastProgressText)"
        default: return lastProgressText
        }
    }

    /// Merge the canonical event stream and host-operation evidence into one rail.
    /// Host operations carry real timestamps and exit codes, which is how the design's
    /// "Host command dispatched / completed" rows are produced without inventing data.
    var timeline: [TimelineItem] {
        var items: [TimelineItem] = recentEvents.items.map { event in
            TimelineItem(id: event.eventRef,
                         time: RelativeTime.clock(TimestampParser.date(from: event.occurredAt)),
                         kind: EventPresentation.kind(for: event.kind),
                         title: EventPresentation.title(for: event.kind),
                         meta: nil,
                         mono: event.eventRef,
                         occurredAt: TimestampParser.date(from: event.occurredAt))
        }
        for op in hostOperations {
            let start = TimestampParser.date(from: op.startedAt)
            let end = TimestampParser.date(from: op.completedAt)
            let failed = (op.exitCode ?? 0) != 0 || op.resultState.uppercased().contains("FAIL")
            items.append(TimelineItem(id: op.hostExecutionRef + ".start",
                                      time: RelativeTime.clock(start),
                                      kind: .dispatch,
                                      title: "Host command dispatched",
                                      meta: op.operation,
                                      mono: op.hostExecutionRef, occurredAt: start))
            if end != nil {
                items.append(TimelineItem(id: op.hostExecutionRef + ".end",
                                          time: RelativeTime.clock(end),
                                          kind: failed ? .error : .done,
                                          title: failed ? "Host command failed" : "Host command completed",
                                          meta: (failed ? "exit \(op.exitCode ?? 1)" : "exit 0") + " · " + RelativeTime.duration(from: start, to: end),
                                          mono: op.hostExecutionRef, occurredAt: end))
            }
        }
        return items.enumerated().sorted {
            let lhs = $0.element.occurredAt ?? .distantPast
            let rhs = $1.element.occurredAt ?? .distantPast
            return lhs == rhs ? $0.offset < $1.offset : lhs < rhs
        }.map(\.element)
    }
}

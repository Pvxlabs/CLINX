import XCTest
@testable import CLINXMonitor

/// View-model coverage for the UI/UX redesign: the seven-state system, freshness,
/// progress, view filters, synthetic acceptance data and the read-only boundary.
final class RedesignStatusTests: XCTestCase {

    private func task(_ json: [String: Any]) throws -> ObservedTask {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(ObservedTask.self, from: JSONSerialization.data(withJSONObject: json))
    }

    private func example(_ name: String) throws -> [String: Any] {
        let file = try XCTUnwrap(Bundle.module.url(forResource: "api-examples", withExtension: "json"))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any])
        let examples = try XCTUnwrap(root["examples"] as? [[String: Any]])
        let record = try XCTUnwrap(examples.first { $0["name"] as? String == name })
        return try XCTUnwrap(record["response"] as? [String: Any])
    }

    func testSevenStateMappingKeepsFailedAndBlockedApart() throws {
        var record = try example("pass_detail")
        record["state"] = "FAILED"
        XCTAssertEqual(try task(record).monitorStatus, .failed)

        record["state"] = "BLOCKED"
        XCTAssertEqual(try task(record).monitorStatus, .blocked)

        record["state"] = "CANCELLED"
        XCTAssertEqual(try task(record).monitorStatus, .cancelled)

        record["state"] = "COMPLETED"
        XCTAssertEqual(try task(record).monitorStatus, .completed)

        record["state"] = "FUTURE_STATE"
        XCTAssertEqual(try task(record).monitorStatus, .unknown)
    }

    func testHistoricalPassNeverOverridesCurrentBlocked() throws {
        let blocked = try task(try example("blocked_detail"))
        XCTAssertEqual(blocked.monitorStatus, .blocked)
        XCTAssertFalse(blocked.resultPassed, "a PASS must not surface on a blocked execution")
        XCTAssertEqual(blocked.displayState, .blocked, "legacy mapping is unchanged")

        let passed = try task(try example("pass_detail"))
        XCTAssertEqual(passed.monitorStatus, .completed)
        XCTAssertTrue(passed.resultPassed)
    }

    func testRetryRequiredFailureStaysFailed() throws {
        var record = try example("pass_detail")
        record["state"] = "FAILED"
        record["retry_required"] = true
        XCTAssertEqual(try task(record).monitorStatus, .failed,
                       "exit ≠ 0 must never be rendered as a generic block")

        record["state"] = "BLOCKED"
        XCTAssertEqual(try task(record).monitorStatus, .blocked)
    }

    func testHostFailureFlipsStatusToFailed() throws {
        var record = try example("running_detail")
        let operation: [String: Any] = [
            "host_execution_ref": "hostexec_test", "execution_ref": "exec_fixture",
            "host": "p620", "surface": "host", "operation_class": "read_only",
            "capability": "shell", "operation": "swift test", "started_at": "2026-09-30T17:54:48.295309+00:00",
            "completed_at": "2026-09-30T17:54:50.295309+00:00", "duration_ms": 2000,
            "exit_code": 1, "result_state": "FAILED", "timed_out": false,
        ]
        record["host_operations"] = [operation]
        XCTAssertEqual(try task(record).monitorStatus, .failed)
    }

    func testRunningBecomesStaleWhenAuthorityIsNotLive() throws {
        let running = try task(try example("running_detail"))
        XCTAssertEqual(running.monitorStatus, .running)
        XCTAssertEqual(running.status(freshness: .stale), .stale)
        XCTAssertEqual(running.status(freshness: .lastKnown), .stale)
        XCTAssertEqual(running.status(freshness: .current), .running)
    }

    func testProgressPresentationUsesCanonicalDenominatorOnly() throws {
        let running = try task(try example("running_detail"))
        XCTAssertEqual(running.progressPresentation, .unavailable)
        XCTAssertEqual(running.progressLabel, "Progress unavailable")
        XCTAssertEqual(running.progressReason, "No canonical progress denominator is available.")
    }

    func testIncompleteExecutionNeverPresentsCompletedPhaseAsProgress() throws {
        var record = try example("blocked_detail")
        record["phases"] = [
            ["phase_ref": "p1", "title": "Plan", "state": "COMPLETED", "completed_units": 4,
             "total_units": 4, "unit": "steps", "evidence_ref": "p1", "progress_percent": NSNull()],
            ["phase_ref": "p2", "title": "Provider delivery", "state": "BLOCKED", "completed_units": NSNull(),
             "total_units": NSNull(), "unit": "steps", "evidence_ref": "p2", "progress_percent": NSNull()],
        ]
        let blocked = try task(record)
        guard case .indeterminate(let label, let done) = blocked.progressPresentation else {
            return XCTFail("a blocked execution must not show a finished denominator")
        }
        XCTAssertEqual(label, blocked.stage)
        XCTAssertEqual(done, 4)

        var running = try example("running_detail")
        running["phases"] = [
            ["phase_ref": "p3", "title": "Validation", "state": "RUNNING", "completed_units": 17,
             "total_units": 24, "unit": "steps", "evidence_ref": "p3", "progress_percent": NSNull()],
        ]
        guard case .determinate(let runningLabel, let runningDone, let runningTotal) = try task(running).progressPresentation else {
            return XCTFail("a running phase with a denominator must be determinate")
        }
        XCTAssertEqual(runningLabel, "Validation")
        XCTAssertEqual(runningDone, 17)
        XCTAssertEqual(runningTotal, 24)

        var completed = try example("pass_detail")
        completed["phases"] = [
            ["phase_ref": "p1", "title": "Complete", "state": "COMPLETED", "completed_units": 14,
             "total_units": 14, "unit": "steps", "evidence_ref": "p1", "progress_percent": NSNull()],
        ]
        guard case .determinate(let completedLabel, _, let completedTotal) = try task(completed).progressPresentation else {
            return XCTFail("a completed execution must keep its final denominator")
        }
        XCTAssertEqual(completedLabel, "Complete")
        XCTAssertEqual(completedTotal, 14)
    }

    func testTimestampParserHandlesMicrosecondOffsets() {
        let date = TimestampParser.date(from: "2026-09-30T17:54:48.295309+00:00")
        XCTAssertNotNil(date)
        let roundTrip = TimestampParser.date(from: "2026-09-30T17:54:48+00:00")
        XCTAssertEqual(roundTrip.map { Int($0.timeIntervalSince1970) },
                       date.map { Int($0.timeIntervalSince1970) })
    }

    func testViewFiltersMatchTheHandoffTable() {
        let statuses: [MonitorStatus] = [.running, .blocked, .failed, .completed, .cancelled, .stale, .unknown]
        XCTAssertEqual(statuses.filter { MonitorView.active.matches($0) }, [.running, .blocked, .stale])
        XCTAssertEqual(statuses.filter { MonitorView.blocked.matches($0) }, [.blocked])
        XCTAssertEqual(statuses.filter { MonitorView.failed.matches($0) }, [.failed])
        XCTAssertEqual(statuses.filter { MonitorView.completed.matches($0) }, [.completed, .cancelled])
        XCTAssertEqual(statuses.filter { MonitorView.recent.matches($0) }.count, 7)
        XCTAssertEqual(MonitorView.allCases.map(\.shortcutIndex), [1, 2, 3, 4, 5])
    }

    func testObserverEventKindsMapToTimelineTitles() {
        XCTAssertEqual(EventPresentation.title(for: "V1HostExecutionStartedObserved"), "Host command dispatched")
        XCTAssertEqual(EventPresentation.kind(for: "V1HostExecutionStartedObserved"), .dispatch)
        XCTAssertEqual(EventPresentation.kind(for: "V1ExecutionResultPersistedObserved"), .writeback)
        XCTAssertEqual(EventPresentation.kind(for: "V1LeaseReleasedObserved"), .deliver)
    }
}

final class SyntheticAcceptanceTests: XCTestCase {

    func testSyntheticScenariosCoverTheDesignSet() {
        XCTAssertEqual(SyntheticScenario.allCases.map(\.rawValue),
                       ["healthy", "running", "blocked", "failed", "stale", "offline", "long", "empty"])
        XCTAssertEqual(SyntheticScenario.long.label, "Long Content")
        XCTAssertEqual(SyntheticScenario.stale.freshness, .stale)
        XCTAssertEqual(SyntheticScenario.offline.freshness, .lastKnown)
        XCTAssertEqual(SyntheticScenario.running.freshness, .current)
    }

    func testSyntheticDataIsAlwaysReadOnly() async throws {
        for scenario in SyntheticScenario.allCases {
            let service = SyntheticObserverService(scenario: scenario)
            let health = try await service.health()
            XCTAssertTrue(health.readOnly, "\(scenario) must stay read-only")
            let page = try await service.tasks(active: true, offset: 0)
            for task in page.items {
                XCTAssertTrue(task.mutationBoundary.observerReadOnly)
                XCTAssertTrue(task.mutationBoundary.allowedActions.isEmpty)
                XCTAssertEqual(task.schemaVersion, "1")
            }
        }
    }

    func testSyntheticHealthFollowsScenarioLiveness() async throws {
        let live = try await SyntheticObserverService(scenario: .running).health()
        let stale = try await SyntheticObserverService(scenario: .stale).health()
        let offline = try await SyntheticObserverService(scenario: .offline).health()
        XCTAssertEqual(live.authorityLiveness, "LIVE")
        XCTAssertEqual(stale.authorityLiveness, "STALE")
        XCTAssertEqual(offline.authorityLiveness, "UNKNOWN")
    }

    func testEmptyScenarioServesNothing() async throws {
        let service = SyntheticObserverService(scenario: .empty)
        let page = try await service.tasks(active: true, offset: 0)
        XCTAssertTrue(page.items.isEmpty)
        XCTAssertFalse(page.hasMore)
    }

    @MainActor
    func testStoreServesSyntheticScenariosWithoutLiveEndpoint() async throws {
        let store = MonitorStore()
        store.useSynthetic(.running)
        await store.refresh()
        XCTAssertEqual(store.connection, .connected)
        XCTAssertFalse(store.allTasks.isEmpty)
        XCTAssertEqual(store.freshness, .current)
        XCTAssertEqual(store.authority, .live)

        let blocked = store.allTasks.filter { store.status(of: $0) == .blocked }
        XCTAssertFalse(blocked.isEmpty)
        XCTAssertEqual(store.counts[.blocked] ?? -1, blocked.count)

        store.view = .blocked
        XCTAssertEqual(store.visibleTasks.count, blocked.count)
        XCTAssertEqual(store.groupedTasks.count, 1) // non-active views are a single list
    }

    @MainActor
    func testOfflineScenarioKeepsLastKnownDataVisible() async throws {
        let store = MonitorStore()
        store.useSynthetic(.offline)
        await store.refresh()
        XCTAssertEqual(store.freshness, .lastKnown)
        XCTAssertFalse(store.allTasks.isEmpty, "offline must never blank the app")
        XCTAssertFalse(store.visibleTasks.isEmpty)
        XCTAssertEqual(store.connectivityNote, "Observer unavailable")
    }

    @MainActor
    func testStaleScenarioStopsClaimingRunning() async throws {
        let store = MonitorStore()
        store.useSynthetic(.stale)
        await store.refresh()
        XCTAssertEqual(store.freshness, .stale)
        XCTAssertTrue(store.allTasks.allSatisfy { store.status(of: $0) != .running })
        XCTAssertTrue(store.allTasks.contains { store.status(of: $0) == .stale })
    }

    @MainActor
    func testSearchAndFiltersNarrowTheList() async throws {
        let store = MonitorStore()
        store.useSynthetic(.running)
        await store.refresh()
        store.view = .recent
        let all = store.visibleTasks.count
        XCTAssertGreaterThan(all, 0)

        store.searchText = "audit"
        XCTAssertLessThan(store.visibleTasks.count, all)
        XCTAssertTrue(store.visibleTasks.allSatisfy { $0.titleText.lowercased().contains("audit") })

        store.searchText = ""
        store.projectFilter = "ORION"
        XCTAssertTrue(store.visibleTasks.allSatisfy { $0.projectText == "ORION" })
        store.projectFilter = nil

        store.hostFilter = "m2-build"
        XCTAssertTrue(store.visibleTasks.allSatisfy { $0.hostText == "m2-build" })
        store.hostFilter = nil

        store.timeWindow = .hour
        XCTAssertTrue(store.visibleTasks.allSatisfy { ($0.lastProgressMinutes ?? 0) <= 60 })
    }

    @MainActor
    func testKeyboardSelectionMovesThroughVisibleRows() async throws {
        let store = MonitorStore()
        store.useSynthetic(.running)
        await store.refresh()
        store.view = .recent
        let tasks = store.visibleTasks
        XCTAssertGreaterThan(tasks.count, 2)

        store.beginSelection(tasks[0].taskRef)
        XCTAssertEqual(store.selectionIndex(), 0)
        store.moveSelection(by: 1)
        XCTAssertEqual(store.selectedRef, tasks[1].taskRef)
        store.moveSelection(by: -1)
        XCTAssertEqual(store.selectedRef, tasks[0].taskRef)
        store.moveSelection(by: -1)
        XCTAssertEqual(store.selectedRef, tasks[0].taskRef, "selection must clamp at the top")
    }

    @MainActor
    func testSelectionSurvivesRefresh() async throws {
        let store = MonitorStore()
        store.useSynthetic(.running)
        await store.refresh()
        store.view = .recent
        let target = try XCTUnwrap(store.visibleTasks.first)
        await store.select(target.taskRef)
        await store.refresh()
        XCTAssertEqual(store.selectedRef, target.taskRef)
        XCTAssertEqual(store.selected?.taskRef, target.taskRef)
    }

    @MainActor
    func testPaginationLoadsTheNextPage() async throws {
        let store = MonitorStore()
        store.useSynthetic(.running)
        await store.refresh()
        store.view = .recent
        let firstPage = store.visibleTasks.count
        XCTAssertTrue(store.hasMoreInCurrentView)
        await store.loadMore()
        XCTAssertGreaterThan(store.visibleTasks.count, firstPage)
    }
}

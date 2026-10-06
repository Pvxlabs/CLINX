import Foundation
import CryptoKit

/// Converts v2 read-only observations to rows for the existing Monitor projection.
/// These references are presentation identities, never canonical task references.
enum NetworkObservationAdapter {
    static func isPresentationRef(_ ref: String) -> Bool { ref.hasPrefix("native_observation_") }

    static func project(_ observations: [NetworkObservation], canonical: [ObservedTask]) -> [ObservedTask] {
        let canonicalRefs = Set(canonical.map(\.taskRef))
        let canonicalThreads = Set(observations.compactMap { item -> String? in
            guard let ref = item.taskRef, canonicalRefs.contains(ref) else { return nil }
            return threadKey(item)
        })
        var byThread: [String: NetworkObservation] = [:]
        for item in observations where item.schemaVersion == "clinx-observation-v1" {
            if let ref = item.taskRef, canonicalRefs.contains(ref) { continue }
            let key = threadKey(item)
            if canonicalThreads.contains(key) { continue }
            if let current = byThread[key] {
                // A bound observation carries more identity evidence; otherwise use the
                // latest observation of the same native thread.
                let itemBound = item.taskRef != nil
                let currentBound = current.taskRef != nil
                if itemBound == currentBound && item.receivedAt <= current.receivedAt { continue }
                if !itemBound && currentBound { continue }
            }
            byThread[key] = item
        }
        return byThread.sorted { $0.key < $1.key }.map { row($0.value, key: $0.key) }
    }

    private static func threadKey(_ item: NetworkObservation) -> String {
        [item.nodeId, item.userScope, item.provider,
         item.nativeThreadId.isEmpty ? item.observationId : item.nativeThreadId]
            .joined(separator: "\u{1F}")
    }

    private static func row(_ item: NetworkObservation, key: String) -> ObservedTask {
        let digest = SHA256.hash(data: Data(key.utf8)).map { String(format: "%02x", $0) }.joined()
        let ref = "native_observation_" + digest
        let state: String
        switch item.turn.nativeState {
        case "RUNNING":
            // Persisted native indexes are historical evidence, not proof that a
            // runtime is still executing. Only an explicit liveness proof may
            // enter the Monitor Active projection.
            state = item.liveness == "PROVEN" ? "CODEX_RUNNING" : "UNKNOWN"
        case "FAILED": state = "FAILED"
        case "COMPLETED": state = "COMPLETED"
        case "CANCELLED", "INTERRUPTED", "TIMED_OUT", "DISCONNECTED": state = "CANCELLED"
        default: state = "UNKNOWN"
        }
        let observed = item.sourceUpdatedAt.isEmpty
            ? ISO8601DateFormatter().string(from: Date(timeIntervalSince1970: item.receivedAt))
            : item.sourceUpdatedAt
        let started = item.turn.startedAt?.isEmpty == false ? item.turn.startedAt : nil
        let completed = item.turn.completedAt?.isEmpty == false ? item.turn.completedAt : nil
        let host = item.nodeId
        let activity = item.turn.progress ?? item.turn.summary ?? item.turn.nativeState
        return ObservedTask(
            schemaVersion: "presentation-native-v2", taskRef: ref, executionRef: nil,
            project: item.project, title: item.shortTitle, host: host,
            state: state, stage: item.turn.nativeState, executionState: state,
            executionStage: item.turn.nativeState,
            model: ModelIdentity(logical: nil, resolved: nil), reasoning: nil,
            currentActivity: CurrentActivity(kind: "NATIVE_OBSERVATION", label: activity, observedAt: observed),
            blocker: nil,
            timestamps: TaskTimestamps(createdAt: started, updatedAt: observed,
                                       lastProgressAt: observed, startedAt: started,
                                       completedAt: completed, observedAt: observed),
            elapsedSeconds: nil, codexRunning: state == "CODEX_RUNNING", retryRequired: false,
            mutationBoundary: MutationBoundary(observerReadOnly: true, allowedActions: [],
                                               executionOperationClasses: nil, executionSurface: nil,
                                               authorityStatus: "NATIVE_OBSERVATION_ONLY"),
            routing: Routing(host: RouteComponent(identifier: item.nodeId, status: "OBSERVED"),
                             surface: RouteComponent(identifier: item.sourceClient, status: "OBSERVED"),
                             provider: RouteComponent(identifier: item.provider, status: "OBSERVED"),
                             transport: RouteComponent(identifier: nil, status: "UNKNOWN")),
            phases: [], progressPercent: nil, progressBasis: "NATIVE_OBSERVATION_ONLY",
            recentEvents: EventPage(schemaVersion: nil, taskRef: nil, observedAt: observed,
                                    items: [], nextCursor: "", hasMore: false, coverage: item.coverage),
            hostOperations: [], hostOperationsHasMore: false, finalResult: nil,
            artifacts: [], artifactsStatus: "UNAVAILABLE", menuState: "READ_ONLY",
            observationId: item.observationId)
    }
}

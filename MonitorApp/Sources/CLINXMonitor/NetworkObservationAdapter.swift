import Foundation
import CryptoKit

/// Converts v2 read-only observations to rows for the existing Monitor projection.
/// These references are presentation identities, never canonical task references.
enum NetworkObservationAdapter {
    static func isPresentationRef(_ ref: String) -> Bool { ref.hasPrefix("native_observation_") }

    static func project(_ observations: [NetworkObservation], canonical: [ObservedTask]) -> [ObservedTask] {
        var byThread: [String: NetworkObservation] = [:]
        for item in observations where item.schemaVersion == "clinx-observation-v1" {
            let key = threadKey(item)
            if let current = byThread[key] {
                if item.turn.ordinal < current.turn.ordinal { continue }
                if item.turn.ordinal == current.turn.ordinal && item.turn.turnId == current.turn.turnId {
                    if current.turn.executionRef != nil && item.turn.executionRef == nil { continue }
                    if item.turn.executionRef != nil && current.turn.executionRef == nil {
                        byThread[key] = item
                        continue
                    }
                }
                if item.turn.ordinal == current.turn.ordinal && item.receivedAt <= current.receivedAt { continue }
            }
            byThread[key] = item
        }
        return byThread.sorted { $0.key < $1.key }.compactMap { key, item in
            if let ref = item.taskRef, let execution = item.turn.executionRef,
               canonical.contains(where: { $0.taskRef == ref && $0.executionRef == execution }) { return nil }
            return row(item, key: key)
        }
    }

    static func supersedesAdoption(_ task: ObservedTask, observations: [NetworkObservation]) -> Bool {
        guard task.executionRef == nil, let identity = task.nativeConversation else { return false }
        return observations.contains { item in
            guard item.nodeId == identity.nodeId && item.provider == identity.provider &&
                item.nativeThreadId == identity.threadId else { return false }
            if item.turn.nativeState == "RUNNING" {
                return item.liveness == "PROVEN" && item.freshness == "RECENT" &&
                    (0..<30).contains(Date().timeIntervalSince1970 - item.receivedAt)
            }
            // Durable terminal evidence may supersede an older adoption, never a
            // newer queued intent or any task with a canonical execution.
            guard item.turn.isTerminal, item.turn.completedAt?.isEmpty == false,
                  let updated = TimestampParser.date(from: task.timestamps.updatedAt) else { return false }
            return Double(item.turn.ordinal) / 1000 >= updated.timeIntervalSince1970
        }
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
            state = item.liveness == "PROVEN" && item.freshness == "RECENT" &&
                (0..<30).contains(Date().timeIntervalSince1970 - item.receivedAt) ? "CODEX_RUNNING" : "UNKNOWN"
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

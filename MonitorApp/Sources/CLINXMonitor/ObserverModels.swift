import Foundation

// Canonical v1 wire models. Use JSONDecoder.keyDecodingStrategy = .convertFromSnakeCase.
// Open string states preserve forward compatibility; never decode unknown state as PASS.
// ISO-8601 timestamps stay Strings to preserve exact source precision and nullability.
public struct ObserverHealth: Codable, Sendable {
    public let schemaVersion: String
    public let status: String
    public let readOnly: Bool
    public let observedAt: String
    public let authorityLiveness: String
    public let authMode: String
    public let eventsTransport: String
}

public struct TaskPage: Codable, Sendable {
    public let schemaVersion: String
    public let observedAt: String
    public let items: [ObservedTask]
    public let nextOffset: Int?
    public let hasMore: Bool
}

public struct ObservedTask: Codable, Identifiable, Sendable {
    public var id: String { taskRef }
    public let schemaVersion: String
    public let taskRef: String
    public let executionRef: String?
    public let project: String?
    public let title: String?
    public let host: String?
    public let state: String
    public let stage: String
    public let executionState: String
    public let executionStage: String
    public let model: ModelIdentity
    public let reasoning: String? // effort setting only; never reasoning content
    public let currentActivity: CurrentActivity
    public let blocker: Blocker?
    public let timestamps: TaskTimestamps
    public let elapsedSeconds: Int?
    public let codexRunning: Bool
    public let retryRequired: Bool
    public let mutationBoundary: MutationBoundary
    public let routing: Routing
    public let phases: [Phase]
    public let progressPercent: Double?
    public let progressBasis: String
    public let recentEvents: EventPage
    public let hostOperations: [HostOperation]
    public let hostOperationsHasMore: Bool
    public let finalResult: FinalResult?
    public let artifacts: [ArtifactMetadata]
    public let artifactsStatus: String
    public let menuState: String
    /// Original v2 identity for read-only native session content; never an execution ref.
    public var observationId: String? = nil
}

public struct ModelIdentity: Codable, Sendable {
    public let logical: String?
    public let resolved: String?
}
public struct CurrentActivity: Codable, Sendable {
    public let kind: String
    public let label: String
    public let observedAt: String?
}
public struct Blocker: Codable, Sendable {
    public let code: String
    public let message: String
}
public struct TaskTimestamps: Codable, Sendable {
    public let createdAt: String?
    public let updatedAt: String?
    public let lastProgressAt: String?
    public let startedAt: String?
    public let completedAt: String?
    public let observedAt: String
}
public struct MutationBoundary: Codable, Sendable {
    public let observerReadOnly: Bool
    public let allowedActions: [String]
    public let executionOperationClasses: [String]?
    public let executionSurface: String?
    public let authorityStatus: String
}
public struct RouteComponent: Codable, Sendable {
    public let identifier: String?
    public let status: String
}
public struct Routing: Codable, Sendable {
    public let host: RouteComponent
    public let surface: RouteComponent
    public let provider: RouteComponent
    public let transport: RouteComponent
}
public struct Phase: Codable, Identifiable, Sendable {
    public var id: String { phaseRef }
    public let phaseRef: String
    public let title: String
    public let state: String
    public let completedUnits: Int?
    public let totalUnits: Int?
    public let unit: String?
    public let evidenceRef: String
    public let progressPercent: Double?
}
public struct EventPage: Codable, Sendable {
    public let schemaVersion: String? // present on endpoint, omitted on nested recent_events
    public let taskRef: String?
    public let observedAt: String?
    public let items: [ObserverEvent]
    public let nextCursor: String
    public let hasMore: Bool
    public let coverage: String
}
public struct ObserverEvent: Codable, Identifiable, Sendable {
    public var id: String { eventRef }
    public let cursor: String
    public let eventRef: String
    public let kind: String
    public let executionRef: String?
    public let occurredAt: String?
    public let recordedAt: String?
}
public struct HostOperation: Codable, Identifiable, Sendable, Equatable {
    public var id: String { hostExecutionRef }
    public let hostExecutionRef: String
    public let executionRef: String
    public let host: String
    public let surface: String
    public let operationClass: String
    public let capability: String
    public let operation: String
    public let startedAt: String?
    public let completedAt: String?
    public let durationMs: Int?
    public let exitCode: Int?
    public let resultState: String
    public let timedOut: Bool
}
public struct FinalResult: Codable, Sendable, Equatable {
    public let executionRef: String
    public let status: String
    public let summary: String?
    public let validation: String?
    public let blockers: String?
    public let nextState: String?
    public let receivedAt: String?
    public let changedFiles: [String]? // v1 server always null; raw paths omitted
    public let textTruncated: Bool
    public let redaction: String
}
public struct ArtifactMetadata: Codable, Identifiable, Sendable {
    public var id: String { artifactRef }
    public let artifactRef: String
    public let displayName: String
    public let mediaType: String
    public let byteSize: Int
    public let sha256: String
    public let createdAt: String
    public let executionRef: String
    public let classification: String
    // No file path, file URL, signed download URL or arbitrary open action.
}
public struct ObserverError: Codable, Sendable {
    public let schemaVersion: String
    public let error: String
}

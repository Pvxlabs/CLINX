import Foundation
import CryptoKit
@preconcurrency import MarkdownUI

// v2 routes carry a versioned observation contract. Canonical v1 task models
// remain compatible; taskRef comes only from canonical registry admission.
struct NetworkObservation: Codable, Identifiable, Sendable, Equatable {
    var id: String { observationId }
    let schemaVersion: String
    let observationId: String
    let nodeId: String
    let deviceName: String
    let userScope: String
    let provider: String
    let nativeThreadId: String
    let project: String
    let title: String
    let source: String
    let sourceClient: String
    let taskRef: String?
    let bindingEvidence: String
    let sourceGeneration: String
    let coverage: String
    let sourceUpdatedAt: String
    let receivedAt: Double
    let freshness: String
    let liveness: String
    let turn: ObservationTurn
    let control: ObservationControl
    var codexUri: String? = nil
    var displayState: String? = nil
    var livenessReason: String? = nil
    var livenessValidUntil: Double? = nil

    var shortTitle: String { String(title.split(separator: "\n").first.map(String.init)?.prefix(100) ?? title.prefix(100)) }
    var isRunning: Bool { turn.nativeState == "RUNNING" }
    var statusLabel: String { turn.nativeState }
}

struct ObservationTurn: Codable, Identifiable, Sendable, Equatable {
    var id: String { turnId ?? executionRef ?? "metadata" }
    let turnId: String?
    let ordinal: Int64
    let nativeState: String
    let executionRef: String?
    let executionState: String?
    let businessResult: String?
    let resultProcessing: String
    let startedAt: String?
    let completedAt: String?
    let progress: String?
    let summary: String?
    let artifacts: [String]
    var source: String? = nil
    /// Exact native turn_context evidence, absent on older Observer versions.
    var model: String? = nil
    var isTerminal: Bool {
        ["COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"].contains(nativeState)
    }
}
struct ObservationControl: Codable, Sendable, Equatable {
    let nodeId: String
    let nativeThreadId: String
    let taskRef: String?
    let executionRef: String?
    let entrypoint: String
    let enabled: Bool
    let reason: String
    let allowedActions: [String]
}
struct ObservationPage: Codable, Sendable {
    let schemaVersion: String
    let items: [NetworkObservation]
    let nextCursor: String?
    let hasMore: Bool
    let coverage: [String: String]
    let retentionEvicted: Int
    var sources: [ObservationSource]? = nil
}
struct ObservationSource: Codable, Identifiable, Sendable {
    var id: String { nodeId }
    let nodeId: String
    let displayName: String
    let state: String
    let coverage: String
    let ackSeq: Int64?
    let gap: String?
}
struct ObservationDetail: Codable, Sendable {
    let schemaVersion: String
    let item: NetworkObservation
    let turns: [ObservationTurn]
    let nextCursor: String?
    let hasMore: Bool
    let gap: String?
    let contentStatus: String
}
struct ObservationContext: Codable, Sendable {
    let schemaVersion: String
    let contextStatus: String?
    let lastUserIntent: String?
    let lastCodexResult: String?
    let nextCursor: String?
}

protocol NetworkObservationServing: Sendable {
    func observations(filters: ObservationFilters, cursor: String?) async throws -> ObservationPage
    func observation(_ id: String, cursor: String?) async throws -> ObservationDetail
    func observationContext(_ id: String, cursor: String?) async throws -> ObservationContext
    func observationActivity(_ id: String, cursor: String?) async throws -> ObservationActivityPage
}
struct ObservationFilters: Equatable, Sendable {
    var nativeThreadId = ""
    var node = ""
    var project = ""
    var state = ""
    var kind = ""
    var query: [URLQueryItem] {
        [("node", node), ("project", project), ("state", state), ("kind", kind), ("native_thread_id", nativeThreadId)]
            .filter { !$0.1.isEmpty }.map { URLQueryItem(name: $0.0, value: $0.1) }
    }
}

struct ObservationActivityEntry: Codable, Identifiable, Sendable, Equatable {
    var id: String { eventId }
    let eventId: String
    let turnId: String?
    let executionRef: String?
    let sourceSeq: Int64
    let recordedAt: Double
    let nativeState: String
    let businessResult: String?
    let kind: String
    let text: String?
    let artifacts: [String]
}
struct ObservationActivityPage: Codable, Sendable {
    let schemaVersion: String
    let observationId: String
    let items: [ObservationActivityEntry]
    let coverage: String
    let nextCursor: String?
    let hasMore: Bool
}

extension NetworkObservationServing {
    func observationActivity(_ id: String, cursor: String?) async throws -> ObservationActivityPage {
        ObservationActivityPage(schemaVersion: "clinx-observation-v1", observationId: id,
            items: [], coverage: "UNAVAILABLE", nextCursor: nil, hasMore: false)
    }
}

/// Derived UI data only. Raw observation receipts and Codex text stay unchanged.
struct RenderedObservationText: Sendable, Equatable {
    let text: String
    let markdown: MarkdownContent
    static func == (lhs: Self, rhs: Self) -> Bool { lhs.text == rhs.text }
}

struct RenderedObservationUpdate: Identifiable, Sendable, Equatable {
    let id: String
    let state: String
    let recordedAt: Double
    let content: RenderedObservationText
}

struct RenderedObservationHistory: Sendable, Equatable {
    var response: RenderedObservationText?
    var updates: [RenderedObservationUpdate] = []
}

/// Parsing and snapshot deduplication never run in SwiftUI body or on MainActor.
/// Keep only the current bounded page's text; this is not another history store.
actor ObservationHistoryRenderer {
    private var scope: String?
    private var cache: [String: RenderedObservationText] = [:]
    private(set) var parseCount = 0

    static func distinctUpdates(_ activity: [ObservationActivityEntry]) -> [ObservationActivityEntry] {
        var seen = Set<[String?]>()
        return activity.reversed().filter { entry in
            entry.text?.isEmpty == false && seen.insert(identityFields(entry)).inserted
        }
    }

    private static func identityFields(_ entry: ObservationActivityEntry) -> [String?] {
        [entry.turnId, entry.executionRef, entry.nativeState, entry.businessResult, entry.kind, entry.text]
    }

    private static func identity(scope: String, entry: ObservationActivityEntry) -> String {
        var digest = SHA256()
        for field in [scope] + identityFields(entry) {
            if let field {
                digest.update(data: Data("\(field.utf8.count):".utf8))
                digest.update(data: Data(field.utf8))
            } else {
                digest.update(data: Data("-:".utf8))
            }
        }
        return digest.finalize().map { String(format: "%02x", $0) }.joined()
    }

    func render(scope: String, activity: [ObservationActivityEntry], response: String?) -> RenderedObservationHistory {
        if self.scope != scope { cache = [:]; self.scope = scope }
        var retained: [String: RenderedObservationText] = [:]
        func prepare(_ text: String) -> RenderedObservationText {
            if let existing = retained[text] ?? cache[text] { retained[text] = existing; return existing }
            let value = RenderedObservationText(text: text, markdown: MarkdownContent(text))
            parseCount += 1
            retained[text] = value
            return value
        }
        let updates = Self.distinctUpdates(activity).map { entry in
            RenderedObservationUpdate(id: Self.identity(scope: scope, entry: entry),
                state: entry.nativeState, recordedAt: entry.recordedAt, content: prepare(entry.text!))
        }
        let renderedResponse = response.flatMap { $0.isEmpty ? nil : prepare($0) }
        cache = retained
        return RenderedObservationHistory(response: renderedResponse, updates: updates)
    }
}

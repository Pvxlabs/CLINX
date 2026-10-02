import Foundation

public struct ActivityMessage: Codable, Identifiable, Sendable, Equatable {
    public let id: String
    public let ordinal: Int64
    public let revision: Int64
    public let timestampMs: Int64?
    public let kind: String
    public let text: String
    public let truncated: Bool
    // Prepared once on ObserverClient's actor during decoding, not when a lazy row
    // enters the viewport. Keep derived presentation out of the wire payload.
    let formattedText: AttributedString

    enum CodingKeys: String, CodingKey {
        case id, ordinal, revision, timestampMs, kind, text, truncated
    }

    init(id: String, ordinal: Int64, revision: Int64, timestampMs: Int64?,
         kind: String, text: String, truncated: Bool) {
        self.id = id; self.ordinal = ordinal; self.revision = revision
        self.timestampMs = timestampMs; self.kind = kind; self.text = text
        self.truncated = truncated
        formattedText = (try? AttributedString(markdown: text,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(text)
    }

    public init(from decoder: Decoder) throws {
        let value = try decoder.container(keyedBy: CodingKeys.self)
        self.init(id: try value.decode(String.self, forKey: .id),
                  ordinal: try value.decode(Int64.self, forKey: .ordinal),
                  revision: try value.decode(Int64.self, forKey: .revision),
                  timestampMs: try value.decodeIfPresent(Int64.self, forKey: .timestampMs),
                  kind: try value.decode(String.self, forKey: .kind),
                  text: try value.decode(String.self, forKey: .text),
                  truncated: try value.decode(Bool.self, forKey: .truncated))
    }

    public static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.id == rhs.id && lhs.revision == rhs.revision && lhs.ordinal == rhs.ordinal &&
        lhs.timestampMs == rhs.timestampMs && lhs.kind == rhs.kind &&
        lhs.text == rhs.text && lhs.truncated == rhs.truncated
    }

    var date: Date? { timestampMs.map { Date(timeIntervalSince1970: Double($0) / 1000) } }
}

public struct ActivityPage: Codable, Sendable {
    public let schemaVersion: String
    public let taskRef: String
    public let executionRef: String
    public let observedAt: String
    public let source: String
    public let availability: String
    public let reason: String?
    public let items: [ActivityMessage]
    public let nextCursor: String?
    public let olderCursor: String?
    public let hasMore: Bool
}

/// Optional read capability: synthetic/status-only sources explicitly lack a native feed.
public protocol ActivityServing: Sendable {
    func activity(_ ref: String, execution: String, after: String?, before: String?) async throws -> ActivityPage
}

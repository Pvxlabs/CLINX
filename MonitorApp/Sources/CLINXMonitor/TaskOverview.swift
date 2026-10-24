import Foundation

extension ObservedTask {
    /// Supplementary facts only: status, workspace and host live in the header.
    var overviewRows: [MetadataGrid.Row] {
        var rows: [MetadataGrid.Row] = [
            .init(label: "TASK ID", value: taskRef, mono: true, copyable: true),
            .init(label: "THREAD ID", value: nativeConversation?.threadId ?? "UNKNOWN",
                  mono: true, copyable: nativeConversation?.threadId.isEmpty == false),
            .init(label: "MODEL", value: modelText, mono: false),
            .init(label: "PROVIDER", value: providerText, mono: false),
            .init(label: "STARTED", value: startedDetailText, mono: false),
        ]
        if let result = exactResult {
            rows.append(.init(label: "RESULT", value: result.status, mono: false))
        } else if hasCanonicalTerminalState || completedDate != nil {
            rows.append(.init(label: "RESULT", value: "NOT REPORTED", mono: false))
        }
        return rows
    }
}

import AppKit
import SwiftUI

/// Observer connection settings only — no execution settings exist in a read-only client.
///
/// Figma source of truth: the "Settings" screen (`SettingsPanel`, connection pane) of
/// `CLINX Monitor UI/UX Redesign`. Presented as the native macOS Settings scene (⌘,).
struct MonitorSettingsView: View {
    @ObservedObject var store: MonitorStore

    var body: some View {
        TabView {
            ConnectionSettingsView(store: store)
                .tabItem { Label("Connection", systemImage: "bolt.horizontal") }
            AppearanceSettingsView(store: store)
                .tabItem { Label("Appearance", systemImage: "circle.lefthalf.filled") }
            ShortcutSettingsView()
                .tabItem { Label("Shortcuts", systemImage: "keyboard") }
        }
        .frame(width: 560)
    }
}

private struct ConnectionSettingsView: View {
    @ObservedObject var store: MonitorStore

    @State private var endpoint = ""
    @State private var replacement = ""
    @State private var status: String?
    @State private var statusIsError = false
    @State private var testing = false
    @State private var credentialStored = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            field("Observer endpoint", hint: "CLINX observer read API. HTTPS only.") {
                TextField("https://observer.example.ts.net", text: $endpoint)
                    .textFieldStyle(.roundedBorder)
                    .font(DS.Font.mono(11.5))
            }
            field("Credential", hint: "Read-scoped bearer token · stored in this Mac’s Keychain") {
                HStack(spacing: 8) {
                    HStack(spacing: 6) {
                        Image(systemName: "lock").font(.system(size: 10))
                        Text(credentialStored ? "••••••••••••••••••••••" : "No credential stored")
                            .font(DS.Font.mono(11.5))
                    }
                    .foregroundStyle(DS.Palette.textSecondary)
                    .padding(.horizontal, 8)
                    .frame(height: 24)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 5).fill(DS.Palette.surfaceSecondary))
                    .overlay(RoundedRectangle(cornerRadius: 5).strokeBorder(DS.Palette.border, lineWidth: 1))

                    SecureField("Replace…", text: $replacement)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 130)
                        .onSubmit { replaceCredential() }
                    Button("Replace") { replaceCredential() }
                        .disabled(replacement.isEmpty)
                }
            }
            field("Connection status", hint: nil) {
                HStack(spacing: 8) {
                    StatusGlyphView(glyph: statusGlyph, color: statusColor, size: 11)
                    Text(statusText)
                        .font(DS.Font.body)
                        .foregroundStyle(DS.Palette.textPrimary)
                }
                .frame(height: 24)
            }
            field("Last successful", hint: nil) {
                Text(lastSuccessText)
                    .font(DS.Font.mono(11.5))
                    .monospacedDigit()
                    .foregroundStyle(DS.Palette.textPrimary)
                    .frame(height: 24, alignment: .leading)
            }
            field("", hint: nil) {
                HStack(spacing: 10) {
                    Button("Test connection") { Task { await testConnection() } }
                        .disabled(testing || endpoint.isEmpty)
                    Button("Save endpoint") { saveEndpoint() }
                        .disabled(endpoint == store.endpointText)
                    Button("Remove local credential") { removeCredential() }
                        .disabled(!credentialStored)
                    if testing { ProgressView().controlSize(.small) }
                }
            }
            if let status {
                Text(status)
                    .font(DS.Font.meta)
                    .foregroundStyle(statusIsError ? DS.Palette.error : DS.Palette.success)
                    .padding(.top, 4)
            }

            Spacer(minLength: 12)

            HStack(spacing: 6) {
                Image(systemName: "eye").font(.system(size: 11))
                Text("Monitor is read-only. It has no execution controls and requests read scope only.")
                    .font(DS.Font.micro)
            }
            .foregroundStyle(DS.Palette.textTertiary)
            .padding(.top, 8)
            .overlay(alignment: .top) { Rectangle().fill(DS.Palette.divider).frame(height: 1) }
        }
        .padding(20)
        .frame(minHeight: 320)
        .onAppear {
            endpoint = store.endpointText
            credentialStored = (try? ObserverKeychain.read()) != nil
        }
    }

    private var statusGlyph: StatusGlyph {
        switch store.connection7 {
        case .connected: return .circleCheck
        case .degraded: return .triangle
        case .offline: return .squareCross
        }
    }

    private var statusColor: Color {
        switch store.connection7 {
        case .connected: return DS.Palette.success
        case .degraded: return DS.Palette.stale
        case .offline: return DS.Palette.error
        }
    }

    private var statusText: String {
        switch store.connection7 {
        case .connected: return "Connected · authority \(store.authority.label.lowercased()) · sync \(store.syncText)"
        case .degraded: return "Degraded · \(store.syncText)"
        case .offline: return store.connectivityNote ?? "Offline"
        }
    }

    private var lastSuccessText: String {
        guard let last = store.lastSuccessfulFetch else { return "—" }
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return "\(formatter.string(from: last)) (\(RelativeTime.ago(since: last)))"
    }

    private func saveEndpoint() {
        do {
            try store.configure(endpoint: endpoint)
            statusIsError = false
            status = "Endpoint saved. Polling restarted."
        } catch {
            statusIsError = true
            status = "The endpoint must be a private HTTPS URL without credentials, path or query."
        }
    }

    private func replaceCredential() {
        do {
            try ObserverKeychain.replace(replacement)
            replacement = ""
            credentialStored = true
            store.credentialsChanged()
            statusIsError = false
            status = "Credential replaced in the Keychain."
        } catch {
            replacement = ""
            statusIsError = true
            status = "The credential was rejected (32–256 characters, A–Z a–z 0–9 _ -)."
        }
    }

    private func removeCredential() {
        do {
            try ObserverKeychain.revokeLocal()
            credentialStored = false
            store.credentialsChanged()
            statusIsError = false
            status = "Local credential removed."
        } catch {
            statusIsError = true
            status = "Could not remove the local credential."
        }
    }

    private func testConnection() async {
        testing = true
        defer { testing = false }
        do {
            guard let url = URL(string: endpoint) else { throw MonitorError.invalidEndpoint }
            let client = try ObserverClient(baseURL: url)
            let health = try await client.health()
            statusIsError = !health.readOnly
            status = health.readOnly
                ? "Handshake ok · read scope verified · schema \(health.schemaVersion)"
                : "Observer did not report a read-only schema."
        } catch let error as MonitorError {
            statusIsError = true
            switch error {
            case .server(let code): status = "Handshake failed · HTTP \(code)"
            case .credentialUnavailable: status = "Credential unavailable in the Keychain"
            case .invalidEndpoint: status = "Endpoint rejected before any request"
            default: status = "Handshake failed · no valid read response"
            }
        } catch {
            statusIsError = true
            status = "Handshake failed"
        }
    }

    private func field<Content: View>(_ label: String, hint: String?,
                                      @ViewBuilder content: () -> Content) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Text(label)
                .font(DS.Font.body)
                .foregroundStyle(DS.Palette.textSecondary)
                .frame(width: 130, alignment: .trailing)
                .padding(.top, 3)
            VStack(alignment: .leading, spacing: 4) {
                content()
                if let hint {
                    Text(hint)
                        .font(DS.Font.micro)
                        .foregroundStyle(DS.Palette.textTertiary)
                }
            }
        }
        .padding(.vertical, 6)
    }
}

private struct AppearanceSettingsView: View {
    @ObservedObject var store: MonitorStore

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Appearance follows the macOS system setting. The redesign ships light and dark semantic tokens, so no separate appearance switch is needed.")
                .font(DS.Font.body)
                .foregroundStyle(DS.Palette.textSecondary)
                .fixedSize(horizontal: false, vertical: true)

            Rectangle().fill(DS.Palette.divider).frame(height: 1)

            Toggle("Use synthetic acceptance data", isOn: Binding(
                get: { store.syntheticScenario != nil },
                set: { enabled in
                    if enabled {
                        store.useSynthetic(store.syntheticScenario ?? .running)
                    } else {
                        store.useLiveObserver()
                    }
                }))
            .toggleStyle(.switch)

            if store.syntheticScenario != nil {
                HStack(spacing: 8) {
                    Text("Scenario")
                        .font(DS.Font.body)
                        .foregroundStyle(DS.Palette.textSecondary)
                    Picker("Scenario", selection: Binding(
                        get: { store.syntheticScenario ?? .running },
                        set: { store.useSynthetic($0) })) {
                        ForEach(SyntheticScenario.allCases) { scenario in
                            Text(scenario.label).tag(scenario)
                        }
                    }
                    .labelsHidden()
                    .frame(width: 200)
                }
                Text("Synthetic data is served through the same read-only Observer contract and is always badged SYNTHETIC DATA.")
                    .font(DS.Font.micro)
                    .foregroundStyle(DS.Palette.textTertiary)
            } else {
                Text("Live mode reads the private P620 Observer with this Mac’s stored credential.")
                    .font(DS.Font.micro)
                    .foregroundStyle(DS.Palette.textTertiary)
            }

            Spacer()
        }
        .padding(20)
        .frame(minHeight: 320)
    }
}

private struct ShortcutSettingsView: View {
    private let rows: [(String, String)] = [
        ("Search", "⌘K"), ("Refresh", "⌘R"), ("Settings", "⌘,"),
        ("Active", "⌘1"), ("Blocked", "⌘2"), ("Failed", "⌘3"), ("Recent", "⌘4"), ("Completed", "⌘5"),
        ("Move selection", "↑ ↓"), ("Dismiss", "Esc"), ("Copy (row)", "right-click"),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 0) {
                ForEach(rows.indices, id: \.self) { index in
                    HStack {
                        Text(rows[index].0)
                            .font(DS.Font.body)
                            .foregroundStyle(DS.Palette.textSecondary)
                        Spacer()
                        Kbd(text: rows[index].1)
                    }
                    .padding(.horizontal, 8)
                    .frame(height: 28)
                    .overlay(alignment: .bottom) { Rectangle().fill(DS.Palette.divider).frame(height: 1) }
                }
            }
            Spacer()
        }
        .padding(20)
        .frame(minHeight: 320)
    }
}

import AppKit
import SwiftUI

/// Read-only Monitor preferences, presented in the native macOS Settings scene.
struct MonitorSettingsView: View {
    @ObservedObject var store: MonitorStore
    @SceneStorage("monitor.settingsTab") private var selectedTab = SettingsTab.devices.rawValue

    private enum SettingsTab: String, CaseIterable {
        case devices = "Devices", connection = "Connection", appearance = "Appearance", shortcuts = "Shortcuts"

        var symbol: String {
            switch self {
            case .devices: return "desktopcomputer"
            case .connection: return "bolt.horizontal"
            case .appearance: return "circle.lefthalf.filled"
            case .shortcuts: return "keyboard"
            }
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 4) {
                ForEach(SettingsTab.allCases, id: \.self) { tab in
                    Button { selectedTab = tab.rawValue } label: {
                        VStack(spacing: 6) {
                            Image(systemName: tab.symbol).font(.system(size: 23))
                                .frame(height: 26)
                            Text(tab.rawValue).font(DS.Font.body)
                        }
                        .frame(width: 108, height: 64)
                        .foregroundStyle(selectedTab == tab.rawValue ? DS.Palette.accent : DS.Palette.textSecondary)
                        .background(RoundedRectangle(cornerRadius: 9)
                            .fill(selectedTab == tab.rawValue ? Color.primary.opacity(0.07) : Color.clear))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityAddTraits(selectedTab == tab.rawValue ? .isSelected : [])
                }
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 8)
            .overlay(alignment: .bottom) { Rectangle().fill(DS.Palette.divider).frame(height: 1) }

            Group {
                switch SettingsTab(rawValue: selectedTab) ?? .devices {
                case .devices: DevicesSettingsView(monitor: store)
                case .connection: ConnectionSettingsView(store: store)
                case .appearance: AppearanceSettingsView(store: store)
                case .shortcuts: ShortcutSettingsView()
                }
            }
            .frame(height: 440)
        }
        .frame(width: 620)
        .background(SettingsWindowChrome())
    }
}

private struct SettingsWindowChrome: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView { WindowView() }
    func updateNSView(_ view: NSView, context: Context) {}

    private final class WindowView: NSView {
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            window?.titleVisibility = .hidden
        }
    }
}

private struct ConnectionSettingsView: View {
    @ObservedObject var store: MonitorStore

    @State private var endpoint = ""
    @State private var replacement = ""
    @State private var status: String?
    @State private var statusIsError = false
    @State private var credentialStored = false
    @State private var checkingCredential = true

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Configure a manual Observer connection.")
                .font(DS.Font.body).foregroundStyle(DS.Palette.textSecondary)
                .frame(height: 24, alignment: .topLeading)

            HStack(spacing: 12) {
                Image(systemName: "bolt.horizontal")
                    .font(.system(size: 22)).foregroundStyle(DS.Palette.textSecondary)
                    .frame(width: 34)
                VStack(alignment: .leading, spacing: 4) {
                    Text(store.linkedDeviceName ?? "Manual connection").font(DS.Font.bodyEmphasis)
                    HStack(spacing: 6) {
                        StatusGlyphView(glyph: statusGlyph, color: statusColor, size: 10)
                        Text(statusText).font(DS.Font.meta).foregroundStyle(DS.Palette.textSecondary)
                    }
                }
                Spacer()
            }
            .padding(14)
            .background(RoundedRectangle(cornerRadius: 10).fill(DS.Palette.surfaceSecondary))

            VStack(alignment: .leading, spacing: 8) {
                Text("Observer endpoint").font(DS.Font.bodyEmphasis).foregroundStyle(DS.Palette.textSecondary)
                HStack(spacing: 8) {
                    TextField("https://observer.example.ts.net", text: $endpoint)
                        .textFieldStyle(.roundedBorder).font(DS.Font.mono(11.5))
                        .accessibilityLabel("Observer endpoint")
                    Button { saveEndpoint() } label: { Text("Save").frame(width: 64) }
                        .disabled(endpoint == store.manualEndpoint)
                }
                Text("Private HTTPS address for the read-only Observer.")
                    .font(DS.Font.micro).foregroundStyle(DS.Palette.textTertiary)
            }

            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    Text("Credential").font(DS.Font.bodyEmphasis).foregroundStyle(DS.Palette.textSecondary)
                    Spacer()
                    Text(checkingCredential ? "Checking Keychain…" : (credentialStored ? "Saved in Keychain" : "Not saved"))
                        .font(DS.Font.micro).foregroundStyle(DS.Palette.textTertiary)
                    Menu {
                        Button("Remove local credential", role: .destructive) { removeCredential() }
                            .disabled(!credentialStored)
                    } label: { Image(systemName: "ellipsis") }
                    .menuStyle(.borderlessButton).menuIndicator(.hidden).frame(width: 18)
                    .accessibilityLabel("Credential options")
                }
                HStack(spacing: 8) {
                    SecureField("Enter a new read-only token…", text: $replacement)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("New Observer credential")
                        .onSubmit { replaceCredential() }
                    Button { replaceCredential() } label: { Text("Replace").frame(width: 64) }
                        .disabled(replacement.isEmpty)
                }
                Text("Stored securely on this Mac. Leave blank to keep the saved credential.")
                    .font(DS.Font.micro).foregroundStyle(DS.Palette.textTertiary)
            }

            if let status {
                Text(status).font(DS.Font.meta)
                    .foregroundStyle(statusIsError ? DS.Palette.error : DS.Palette.success)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
            HStack(spacing: 6) {
                Image(systemName: "lock.shield")
                Text("Monitor access is read-only. Credentials stay in this Mac’s Keychain.")
            }
            .font(DS.Font.micro).foregroundStyle(DS.Palette.textTertiary)
            .padding(.top, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay(alignment: .top) { Rectangle().fill(DS.Palette.divider).frame(height: 1) }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .task {
            endpoint = store.manualEndpoint
            checkingCredential = true
            credentialStored = await Task.detached { ObserverKeychain.contains() }.value
            checkingCredential = false
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
        case .connected: return "Connected · synced \(store.syncText)"
        case .degraded: return "Degraded · \(store.syncText)"
        case .offline: return store.connectivityNote ?? "Offline"
        }
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

}

private struct AppearanceSettingsView: View {
    @ObservedObject var store: MonitorStore

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("Appearance automatically follows your Mac’s Light or Dark setting.")
                .font(DS.Font.body)
                .foregroundStyle(DS.Palette.textSecondary)
                .fixedSize(horizontal: false, vertical: true)

            Rectangle().fill(DS.Palette.divider).frame(height: 1)

            HStack {
                Text("Use synthetic acceptance data").font(DS.Font.body)
                Spacer()
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
                .labelsHidden()
            }

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
                Text("Live mode reads the selected Observer with this Mac’s stored credential.")
                    .font(DS.Font.micro)
                    .foregroundStyle(DS.Palette.textTertiary)
            }

            Spacer()
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

private struct ShortcutSettingsView: View {
    private let rows: [(String, String)] = [
        ("Refresh", "⌘R"), ("Settings", "⌘,"),
        ("Active", "⌘1"), ("Blocked", "⌘2"), ("Failed", "⌘3"), ("Recent", "⌘4"), ("Completed", "⌘5"),
        ("Move selection", "↑ ↓"), ("Dismiss", "Esc"), ("Copy (row)", "right-click"),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(spacing: 0) {
                ForEach(rows.indices, id: \.self) { index in
                    HStack {
                        Text(rows[index].0)
                            .font(DS.Font.body)
                            .foregroundStyle(DS.Palette.textSecondary)
                        Spacer()
                        Kbd(text: rows[index].1)
                    }
                    .padding(.horizontal, 4)
                    .frame(height: 36)
                    .overlay(alignment: .bottom) { Rectangle().fill(DS.Palette.divider).frame(height: 1) }
                }
            }
            Spacer()
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

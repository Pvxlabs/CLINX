import SwiftUI

struct DevicesSettingsView: View {
    @ObservedObject var monitor: MonitorStore
    @StateObject private var devices = DeviceConnectionStore()
    @State private var pairing: NearbyDevice?
    @State private var forgetting: NearbyDevice?

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Connect to a nearby CLINX host to view its tasks.")
                        .font(DS.Font.body).foregroundStyle(DS.Palette.textSecondary)
                }
                Spacer()
                Button { Task { await devices.scan() } } label: {
                    Image(systemName: "arrow.clockwise").frame(width: 24, height: 24)
                }
                .buttonStyle(.plain).help("Refresh nearby devices")
                .accessibilityLabel("Refresh nearby devices")
                .disabled(devices.scanning || devices.busyDeviceID != nil)
            }
            currentConnection
            if devices.backendChecked && !devices.backendReady {
                Text("Secure pairing is unavailable. Reinstall the discovery runtime to pair a new device.")
                    .font(DS.Font.meta).foregroundStyle(DS.Palette.error)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    deviceSection("Paired devices", peers: devices.paired,
                                  empty: "Devices you pair with will appear here.")
                    deviceSection("Nearby devices", peers: devices.nearby,
                                  empty: "No nearby devices found. Keep both devices on the same local network and enable pairing on the other device.")
                }
                .padding(.vertical, 2)
            }
            if let error = devices.error, pairing == nil {
                Label(error, systemImage: "exclamationmark.circle")
                    .font(DS.Font.meta).foregroundStyle(DS.Palette.error)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 6) {
                Image(systemName: "lock.shield")
                Text("Paired devices reconnect securely. Monitor access is read-only.")
            }
            .font(DS.Font.micro).foregroundStyle(DS.Palette.textTertiary)
            .padding(.top, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay(alignment: .top) { Rectangle().fill(DS.Palette.divider).frame(height: 1) }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .foregroundStyle(DS.Palette.textPrimary)
        .task {
            while !Task.isCancelled {
                await devices.scan()
                try? await Task.sleep(nanoseconds: 8_000_000_000)
            }
        }
        .sheet(item: $pairing, onDismiss: { devices.cancel() }) { device in
            PairDeviceSheet(device: device, devices: devices, monitor: monitor)
        }
        .confirmationDialog("Forget \(forgetting?.displayName ?? "device")?", isPresented: Binding(
            get: { forgetting != nil }, set: { if !$0 { forgetting = nil } }), titleVisibility: .visible) {
            if let device = forgetting {
                Button("Forget device", role: .destructive) {
                    Task { await devices.forget(device, monitor: monitor) }
                }
            }
            Button("Cancel", role: .cancel) { forgetting = nil }
        } message: {
            Text("This removes trust and the saved Monitor credential from this Mac. You will need to pair again. Access saved on other devices is unchanged.")
        }
    }

    private var currentConnection: some View {
        HStack(spacing: 12) {
            Image(systemName: "desktopcomputer")
                .font(.system(size: 22)).foregroundStyle(DS.Palette.textSecondary)
                .frame(width: 34)
            VStack(alignment: .leading, spacing: 4) {
                Text(monitor.linkedDeviceName ?? (monitor.endpointText.isEmpty ? "No device connected" : "Manual connection"))
                    .font(DS.Font.bodyEmphasis)
                Text(monitor.endpointText.isEmpty ? "Choose a device below to get started." : monitor.runtimeStatus.connection)
                    .font(DS.Font.meta).foregroundStyle(DS.Palette.textSecondary)
            }
            Spacer()
            if monitor.linkedDeviceID != nil {
                Button("Disconnect") { monitor.disconnectPairedDevice() }
                    .controlSize(.small)
            }
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 10).fill(DS.Palette.surfaceSecondary))
    }

    private func deviceSection(_ title: String, peers: [NearbyDevice], empty: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(title).font(DS.Font.bodyEmphasis).foregroundStyle(DS.Palette.textSecondary)
                if title == "Nearby devices", devices.scanning { ProgressView().controlSize(.mini) }
                Spacer()
            }
            if peers.isEmpty {
                Text(empty).font(DS.Font.meta).foregroundStyle(DS.Palette.textTertiary)
                    .fixedSize(horizontal: false, vertical: true).padding(.vertical, 10)
            }
            ForEach(peers) { device in
                HStack(spacing: 12) {
                    Image(systemName: "desktopcomputer").font(.system(size: 17))
                        .foregroundStyle(DS.Palette.textSecondary).frame(width: 30)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(device.displayName).font(DS.Font.bodyEmphasis).lineLimit(1)
                        Text(deviceStatus(device)).font(DS.Font.meta)
                            .foregroundStyle(device.identityMismatch ? DS.Palette.error : DS.Palette.textTertiary)
                    }
                    Spacer()
                    if devices.busyDeviceID == device.id {
                        ProgressView().controlSize(.small)
                    } else if monitor.linkedDeviceID == device.id {
                        Image(systemName: "checkmark.circle.fill").foregroundStyle(DS.Palette.success)
                            .accessibilityLabel("Selected Monitor device")
                    } else {
                        Button(device.paired ? "Connect" : "Pair…") {
                            devices.clearError()
                            if device.paired { devices.connect(device, monitor: monitor) }
                            else { pairing = device }
                        }
                        .controlSize(.small)
                        .disabled(!device.online || device.identityMismatch || devices.busyDeviceID != nil || (!device.paired && !devices.backendReady))
                    }
                    if device.paired {
                        Menu {
                            Button("Forget device…", role: .destructive) { forgetting = device }
                        } label: { Image(systemName: "ellipsis") }
                        .menuStyle(.borderlessButton).menuIndicator(.hidden).frame(width: 18)
                        .disabled(devices.busyDeviceID != nil)
                        .accessibilityLabel("Options for \(device.displayName)")
                    }
                }
                .padding(.vertical, 10)
                .overlay(alignment: .bottom) { Rectangle().fill(DS.Palette.divider).frame(height: 1) }
            }
        }
    }

    private func deviceStatus(_ device: NearbyDevice) -> String {
        if device.identityMismatch { return "Identity changed · verify this device" }
        if devices.busyDeviceID == device.id { return devices.phase ?? "Connecting…" }
        if monitor.linkedDeviceID == device.id { return monitor.runtimeStatus.connection }
        return device.online ? (device.paired ? "Paired · available" : "Available to pair") : "Paired · offline"
    }
}

private struct PairDeviceSheet: View {
    let device: NearbyDevice
    @ObservedObject var devices: DeviceConnectionStore
    @ObservedObject var monitor: MonitorStore
    @Environment(\.dismiss) private var dismiss
    @State private var pin = ""
    @FocusState private var codeFocused: Bool
    private var alreadyPaired: Bool { devices.paired.contains { $0.id == device.id } }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Image(systemName: "lock.shield").font(.system(size: 26)).foregroundStyle(DS.Palette.accent)
            Text("\(alreadyPaired ? "Connect to" : "Pair with") \(device.displayName)").font(.system(size: 17, weight: .semibold))
            Text(alreadyPaired ? "Device pairing is saved. You can retry the Monitor connection without another code." : "Enable pairing on this device, then enter the four-digit code shown there. Codes expire after 60 seconds.")
                .font(DS.Font.body).foregroundStyle(DS.Palette.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if !alreadyPaired {
                SecureField("0000", text: $pin)
                .accessibilityLabel("Four-digit pairing code")
                .textFieldStyle(.roundedBorder).font(.system(size: 20, design: .monospaced))
                .frame(width: 180).focused($codeFocused)
                .disabled(devices.busyDeviceID != nil)
                .onChange(of: pin) { value in pin = String(value.filter { $0.isASCII && $0.isNumber }.prefix(4)) }
                .onSubmit { connect() }
            }
            if let phase = devices.phase {
                HStack { ProgressView().controlSize(.small); Text(phase).font(DS.Font.meta) }
            }
            if let error = devices.error {
                Text(error).font(DS.Font.meta).foregroundStyle(DS.Palette.error)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Spacer()
                Button("Cancel") { pin = ""; devices.cancel(); dismiss() }.keyboardShortcut(.cancelAction)
                Button(alreadyPaired ? "Connect Monitor" : "Pair and connect") { connect() }
                    .keyboardShortcut(.defaultAction)
                    .disabled((!alreadyPaired && pin.count != 4) || devices.busyDeviceID != nil)
            }
        }
        .padding(24).frame(width: 360)
        .onAppear { codeFocused = true }
        .onChange(of: devices.connectedDeviceID) { id in if id == device.id { dismiss() } }
        .onDisappear { pin = "" }
    }

    private func connect() {
        guard (alreadyPaired || pin.count == 4), devices.busyDeviceID == nil else { return }
        devices.connect(device, pin: alreadyPaired ? nil : pin, monitor: monitor)
        pin = ""
    }
}

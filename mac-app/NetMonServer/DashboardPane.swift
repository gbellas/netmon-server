import SwiftUI

/// Live dashboard — device cards rendered from the state dict.
///
/// The Mac UI doesn't try to mirror the iPhone's card designs pixel-
/// for-pixel. Instead it shows a denser per-device panel with the
/// status + key metrics inline, taking advantage of the wider window.
struct DashboardPane: View {
    @EnvironmentObject private var api: LocalAPIClient

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                header
                if api.devices.isEmpty {
                    emptyState
                } else {
                    LazyVGrid(
                        columns: [
                            GridItem(.adaptive(minimum: 320, maximum: 500), spacing: 12)
                        ],
                        spacing: 12
                    ) {
                        ForEach(api.devices) { device in
                            DeviceCard(device: device)
                                .environmentObject(api)
                        }
                    }
                }
            }
            .padding(16)
        }
    }

    private var header: some View {
        HStack {
            Text("Overview").font(.title2.weight(.semibold))
            Spacer()
            if let err = api.lastError, !api.isConnected {
                Label(err, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "wifi.router")
                .font(.system(size: 40))
                .foregroundStyle(.secondary)
            Text("No devices configured")
                .font(.title3.weight(.semibold))
            Text("Add a device in the Devices tab to start polling.")
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 60)
    }
}

/// Single device card on the dashboard. Adapts its body by device kind
/// — router kinds show per-WAN status rows, ICMP kinds show a simple
/// online/offline chip. The card is a pure view over the published
/// state dict, no driver-specific logic.
struct DeviceCard: View {
    let device: DeviceRecord
    @EnvironmentObject private var api: LocalAPIClient

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: device.iconName)
                    .foregroundStyle(.secondary)
                Text(device.displayName)
                    .font(.headline)
                Spacer()
                kindChip
            }
            Divider()
            body(for: device)
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 10).fill(.background.tertiary))
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .strokeBorder(.separator, lineWidth: 0.5)
        )
    }

    private var kindChip: some View {
        Text(device.kindLabel)
            .font(.caption2.weight(.medium))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(.secondary.opacity(0.12)))
            .foregroundStyle(.secondary)
    }

    @ViewBuilder
    private func body(for device: DeviceRecord) -> some View {
        switch device.kind.replacingOccurrences(of: "legacy_", with: "") {
        case "peplink_router", "unifi_network":
            routerBody
        case "icmp_ping":
            pingBody
        default:
            genericBody
        }
    }

    // MARK: - Router (Peplink + UniFi)

    private var routerBody: some View {
        let wanIndices = discoveredWanIndices()
        return VStack(alignment: .leading, spacing: 6) {
            if wanIndices.isEmpty {
                Text("No WAN data yet — poller hasn't reported.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(wanIndices, id: \.self) { idx in
                    WanRow(deviceId: device.id, wanIndex: idx)
                        .environmentObject(api)
                }
            }
        }
    }

    /// Scan the state dict for keys of the form `<deviceId>.wanN.*`
    /// (or the legacy `udm.wanN.*` / `br1.wanN.*`) to discover which
    /// WAN slots actually have data. Supports arbitrary WAN counts —
    /// no hardcoded "1...2" loop.
    private func discoveredWanIndices() -> [Int] {
        // Server publishes state keyed by either device id or the
        // short legacy prefix. We accept both so the card works on
        // either configuration.
        let prefixes = [device.id, "udm", "br1"].filter { !$0.isEmpty }
        var indices: Set<Int> = []
        for prefix in prefixes {
            for key in api.state.keys {
                let pattern = "\(prefix).wan"
                if key.hasPrefix(pattern) {
                    let rest = key.dropFirst(pattern.count)
                    if let dot = rest.firstIndex(of: "."),
                       let n = Int(rest[..<dot]) {
                        indices.insert(n)
                    }
                }
            }
            if !indices.isEmpty { break }
        }
        return indices.sorted()
    }

    // MARK: - ICMP ping

    private var pingBody: some View {
        let keys = api.state.keys
            .filter { $0.hasPrefix("\(device.id).") && $0.hasSuffix(".loss_pct") }
            .sorted()
        return VStack(alignment: .leading, spacing: 6) {
            if keys.isEmpty {
                Text("No targets reporting yet.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(keys, id: \.self) { key in
                    PingRow(key: key)
                        .environmentObject(api)
                }
            }
        }
    }

    // MARK: - Fallback

    private var genericBody: some View {
        Text("Unknown device kind — state updates will arrive in the Events tab.")
            .font(.caption)
            .foregroundStyle(.secondary)
    }
}

/// One WAN row inside a router card. Status dot + name + throughput +
/// a power toggle that dispatches to the local API client.
private struct WanRow: View {
    let deviceId: String
    let wanIndex: Int
    @EnvironmentObject private var api: LocalAPIClient
    @State private var toggling = false

    var body: some View {
        HStack(spacing: 8) {
            Circle().fill(dotColor).frame(width: 8, height: 8)
            Text(name).font(.subheadline)
            Spacer()
            throughputLabel
            Button {
                toggleEnable()
            } label: {
                Image(systemName: isEnabled ? "power.circle.fill" : "power.circle")
                    .foregroundStyle(isEnabled ? .green : .secondary)
            }
            .buttonStyle(.plain)
            .help(isEnabled ? "Disable WAN\(wanIndex)" : "Enable WAN\(wanIndex)")
            .disabled(toggling)
        }
    }

    private func keyFor(_ suffix: String) -> String? {
        for prefix in [deviceId, "udm", "br1"] {
            let key = "\(prefix).wan\(wanIndex).\(suffix)"
            if api.state[key] != nil { return key }
        }
        return nil
    }

    private var name: String {
        if let k = keyFor("name"), let s = api.state[k]?.stringValue, !s.isEmpty {
            return s
        }
        return "WAN \(wanIndex)"
    }

    private var isEnabled: Bool {
        keyFor("enable").flatMap { api.bool($0) } ?? true
    }

    private var status: String {
        keyFor("status").flatMap { api.string($0) } ?? ""
    }

    private var dotColor: Color {
        if !isEnabled { return .gray }
        switch status {
        case "connected":    return .green
        case "standby":      return .yellow
        case "disabled":     return .gray
        default:             return .orange
        }
    }

    @ViewBuilder
    private var throughputLabel: some View {
        if let k = keyFor("rx_bps"), let rx = api.double(k),
           let k2 = keyFor("tx_bps"), let tx = api.double(k2) {
            Text("\(formatBps(rx))↓ \(formatBps(tx))↑")
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
        }
    }

    private func formatBps(_ bps: Double) -> String {
        if bps >= 1_000_000 { return String(format: "%.1fM", bps / 1_000_000) }
        if bps >= 1_000     { return String(format: "%.0fK", bps / 1_000) }
        return "\(Int(bps))"
    }

    private func toggleEnable() {
        toggling = true
        Task {
            try? await api.enableWan(deviceId: deviceId,
                                     wanIndex: wanIndex,
                                     enabled: !isEnabled)
            // Optimistic — the next state tick will correct us if
            // the server rejects the toggle.
            await api.refreshDevices()
            toggling = false
        }
    }
}

private struct PingRow: View {
    /// Key form: "<deviceId>.<target>.loss_pct"
    let key: String
    @EnvironmentObject private var api: LocalAPIClient

    var body: some View {
        HStack(spacing: 8) {
            Circle().fill(dotColor).frame(width: 8, height: 8)
            Text(target).font(.subheadline)
            Spacer()
            if let latency = api.double(key.replacingOccurrences(of: ".loss_pct", with: ".latency_ms")) {
                Text("\(Int(latency)) ms")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            Text(lossText)
                .font(.caption.monospacedDigit())
                .foregroundStyle(lossColor)
        }
    }

    private var target: String {
        let parts = key.split(separator: ".")
        if parts.count >= 3 {
            return parts.dropFirst().dropLast().joined(separator: ".")
        }
        return key
    }

    private var loss: Double {
        api.double(key) ?? 0
    }

    private var lossText: String {
        String(format: "%.0f%% loss", loss)
    }

    private var dotColor: Color {
        if loss == 0 { return .green }
        if loss < 20 { return .yellow }
        return .red
    }

    private var lossColor: Color {
        loss > 0 ? .red : .secondary
    }
}

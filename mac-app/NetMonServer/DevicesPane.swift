import SwiftUI

/// Device list + detail editor. Simpler than the iPhone's Settings
/// flow: one list, one detail pane, one form. Edit/add writes back
/// via the server's /api/devices PUT — the server handles config
/// merge + reload-driver-on-save itself.
///
/// This first cut of the Mac UI is read-only; the edit form is a
/// stub that points the user at the iPhone app (which has the fully-
/// built device editor). When the Mac-side form is implemented it'll
/// slot into the same detail column.
struct DevicesPane: View {
    @EnvironmentObject private var api: LocalAPIClient
    @State private var selection: DeviceRecord?

    var body: some View {
        NavigationSplitView {
            List(api.devices, selection: $selection) { device in
                NavigationLink(value: device) {
                    DeviceRow(device: device)
                }
            }
            .navigationTitle("Devices")
            .navigationSplitViewColumnWidth(min: 220, ideal: 260)
        } detail: {
            if let selection {
                DeviceDetailView(device: selection)
                    .environmentObject(api)
            } else {
                ContentUnavailableView(
                    "Select a device",
                    systemImage: "wifi.router",
                    description: Text("Pick a device from the list to see its live state.")
                )
            }
        }
    }
}

private struct DeviceRow: View {
    let device: DeviceRecord

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: device.iconName)
                .foregroundStyle(.secondary)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                Text(device.displayName).font(.body)
                Text(subtitle).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }

    private var subtitle: String {
        var parts = [device.kindLabel]
        if !device.host.isEmpty { parts.append(device.host) }
        return parts.joined(separator: " · ")
    }
}

/// Detail view for one device — shows every published state key and a
/// link to edit the device in the iPhone app. The Mac window intentionally
/// doesn't re-implement the full edit form; the iPhone form is already
/// the single source of truth for device config.
private struct DeviceDetailView: View {
    let device: DeviceRecord
    @EnvironmentObject private var api: LocalAPIClient

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                header
                metadataCard
                stateKeysCard
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .navigationTitle(device.displayName)
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: device.iconName)
                .font(.system(size: 28))
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(device.displayName).font(.title3.weight(.semibold))
                Text("\(device.kindLabel) · id \(device.id)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
    }

    private var metadataCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionTitle("Configuration")
            infoRow("Host",         device.host.isEmpty ? "—" : device.host, mono: true)
            infoRow("Kind",         device.kind, mono: true)
            infoRow("Mobile",       device.isMobile ? "yes" : "no")
            if !device.capabilities.isEmpty {
                infoRow("Capabilities", device.capabilities.joined(separator: ", "))
            }
            Text("Use the NetMon iPhone app's Settings → Devices to edit. The Mac editor is on the roadmap.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.top, 4)
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 10).fill(.background.tertiary))
    }

    private var stateKeysCard: some View {
        let keys = relevantStateKeys().sorted()
        return VStack(alignment: .leading, spacing: 6) {
            sectionTitle("Live state (\(keys.count) keys)")
            if keys.isEmpty {
                Text("No state keys yet — poller hasn't reported.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(keys, id: \.self) { key in
                    HStack(alignment: .firstTextBaseline) {
                        Text(key)
                            .font(.system(.caption, design: .monospaced))
                            .frame(minWidth: 180, alignment: .leading)
                        Spacer()
                        Text(format(api.state[key]))
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                }
            }
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 10).fill(.background.tertiary))
    }

    private func relevantStateKeys() -> [String] {
        // State keys prefixed by device id or by legacy short-name.
        let prefixes = [device.id + ".", "udm.", "br1."]
        return api.state.keys.filter { key in
            prefixes.contains { key.hasPrefix($0) }
        }
    }

    private func format(_ v: JSONValue?) -> String {
        guard let v else { return "—" }
        switch v {
        case .null:          return "null"
        case .bool(let b):   return String(b)
        case .int(let i):    return String(i)
        case .double(let d): return String(format: "%.2f", d)
        case .string(let s): return s.isEmpty ? "\"\"" : s
        case .array(let a):  return "[\(a.count) items]"
        case .object(let o): return "{\(o.count) keys}"
        }
    }

    private func sectionTitle(_ text: String) -> some View {
        Text(text)
            .font(.caption.weight(.semibold))
            .foregroundStyle(.secondary)
            .textCase(.uppercase)
            .padding(.bottom, 2)
    }

    private func infoRow(_ label: String, _ value: String, mono: Bool = false) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label)
                .foregroundStyle(.secondary)
                .frame(width: 110, alignment: .leading)
            Text(value)
                .font(mono ? .system(.body, design: .monospaced) : .body)
                .textSelection(.enabled)
            Spacer()
        }
    }
}

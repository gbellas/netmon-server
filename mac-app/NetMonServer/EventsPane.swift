import SwiftUI

/// Live event feed. Pulls from /api/events on start + merges in
/// WebSocket "event" messages as they arrive, so new events appear
/// at the top without a refresh.
struct EventsPane: View {
    @EnvironmentObject private var api: LocalAPIClient
    @State private var filter: String = ""

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            Divider()
            list
        }
        .navigationTitle("Events")
    }

    private var toolbar: some View {
        HStack {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(.secondary)
            TextField("Filter by device, kind, or message…", text: $filter)
                .textFieldStyle(.plain)
            if !filter.isEmpty {
                Button { filter = "" } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
            }
            Spacer()
            Button("Refresh") {
                Task { await api.refreshEvents() }
            }
            .buttonStyle(.bordered)
        }
        .padding(12)
    }

    private var list: some View {
        let filtered = api.events.filter { matches($0, filter) }
        return Group {
            if filtered.isEmpty {
                ContentUnavailableView(
                    api.events.isEmpty ? "No events" : "No events match your filter",
                    systemImage: "tray",
                    description: Text(api.events.isEmpty
                        ? "Events will appear here as devices poll."
                        : "Clear the filter to see everything.")
                )
            } else {
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(filtered) { event in
                            EventRow(event: event)
                            Divider()
                        }
                    }
                }
            }
        }
    }

    private func matches(_ event: ServerEvent, _ query: String) -> Bool {
        guard !query.isEmpty else { return true }
        let q = query.lowercased()
        return event.message.lowercased().contains(q)
            || event.kind.lowercased().contains(q)
            || (event.device?.lowercased().contains(q) ?? false)
            || (event.severity?.lowercased().contains(q) ?? false)
    }
}

private struct EventRow: View {
    let event: ServerEvent

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: event.severityIcon)
                .foregroundStyle(iconColor)
                .font(.body)
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(event.kind)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    if let dev = event.device, !dev.isEmpty {
                        Text("· \(dev)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(event.date, style: .time)
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                Text(event.message)
                    .font(.body)
                    .textSelection(.enabled)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private var iconColor: Color {
        switch event.severity {
        case "critical", "error": return .red
        case "warning":           return .orange
        case "info":              return .blue
        default:                  return .secondary
        }
    }
}

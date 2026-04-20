import SwiftUI

/// Sidebar sections in the main window. Stable across launches via
/// AppStorage so the user lands on whichever pane they last viewed.
enum MainPane: String, CaseIterable, Identifiable {
    case dashboard = "Dashboard"
    case devices   = "Devices"
    case events    = "Events"
    case history   = "History"
    case server    = "Server"

    var id: String { rawValue }

    var systemImage: String {
        switch self {
        case .dashboard: return "square.grid.2x2"
        case .devices:   return "wifi.router"
        case .events:    return "list.bullet.rectangle"
        case .history:   return "chart.xyaxis.line"
        case .server:    return "server.rack"
        }
    }
}

/// Root of the Mac main window. A two-column NavigationSplitView with
/// sidebar selection + detail pane. Creates its own @StateObject
/// LocalAPIClient and binds it to the ServerController's token so
/// regenerating the token (from Setup) reconnects the WebSocket.
struct MainWindowView: View {
    @EnvironmentObject private var controller: ServerController
    @StateObject private var api = LocalAPIClient()
    @AppStorage("mainWindow.selectedPane") private var selectedPaneRaw = MainPane.dashboard.rawValue

    private var selectedPane: Binding<MainPane> {
        Binding(
            get: { MainPane(rawValue: selectedPaneRaw) ?? .dashboard },
            set: { selectedPaneRaw = $0.rawValue }
        )
    }

    var body: some View {
        NavigationSplitView {
            Sidebar(selection: selectedPane, api: api)
                .navigationSplitViewColumnWidth(min: 180, ideal: 200, max: 240)
        } detail: {
            detailView
                .frame(minWidth: 600, minHeight: 480)
        }
        .navigationTitle(selectedPane.wrappedValue.rawValue)
        .onAppear {
            api.apiToken = controller.apiToken
            api.start()
        }
        .onDisappear { api.stop() }
        .onChange(of: controller.apiToken) { _, new in
            api.apiToken = new
        }
        .environmentObject(api)
    }

    @ViewBuilder
    private var detailView: some View {
        switch selectedPane.wrappedValue {
        case .dashboard: DashboardPane()
        case .devices:   DevicesPane()
        case .events:    EventsPane()
        case .history:   HistoryPane()
        case .server:    ServerPane()
        }
    }
}

/// Left sidebar — pane picker + connection health chip at the bottom.
private struct Sidebar: View {
    @Binding var selection: MainPane
    @ObservedObject var api: LocalAPIClient

    var body: some View {
        VStack(spacing: 0) {
            List(MainPane.allCases, selection: $selection) { pane in
                Label(pane.rawValue, systemImage: pane.systemImage)
                    .tag(pane)
            }
            Divider()
            connectionChip
                .padding(10)
        }
    }

    private var connectionChip: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(api.isConnected ? Color.green : Color.orange)
                .frame(width: 8, height: 8)
            Text(api.isConnected ? "Live" : "Connecting…")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
            if let last = api.lastUpdate {
                Text(last, style: .time)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }
    }
}

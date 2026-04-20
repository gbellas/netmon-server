import SwiftUI

/// Server operator view — the Mac app's existing Preferences tabs
/// (Overview + Logs) restructured to fit the main-window sidebar.
/// Points at the same ServerController, so Start / Stop / Restart
/// still flow through.
struct ServerPane: View {
    @EnvironmentObject private var controller: ServerController
    @State private var subtab: SubTab = .overview

    enum SubTab: String, CaseIterable {
        case overview = "Overview"
        case logs     = "Logs"
    }

    var body: some View {
        VStack(spacing: 0) {
            Picker("", selection: $subtab) {
                ForEach(SubTab.allCases, id: \.self) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .padding(12)
            Divider()
            Group {
                switch subtab {
                case .overview: overview
                case .logs:     logs
                }
            }
        }
        .navigationTitle("Server")
    }

    private var overview: some View {
        VStack(alignment: .leading, spacing: 14) {
            row("Status",
                controller.statusDescription,
                valueColor: controller.status.isRunning ? .green : .orange)
            row("URL", controller.serverURL, mono: true)
            row("Token",
                controller.apiToken.map { "\($0.prefix(12))…" } ?? "(none)",
                mono: true)
            row("Runtime dir", controller.runtimeDir.path, mono: true, small: true)

            iosPromoCard

            Spacer()

            HStack {
                Button("Open dashboard in browser") {
                    if let url = URL(string: controller.serverURL) {
                        NSWorkspace.shared.open(url)
                    }
                }
                .disabled(!controller.status.isRunning)
                Button("Restart") { controller.restart() }
                    .disabled(!controller.status.isRunning)
                Spacer()
                if controller.status.isRunning {
                    Button("Stop") { controller.stop() }
                        .foregroundStyle(.red)
                } else {
                    Button("Start") { controller.start() }
                        .buttonStyle(.borderedProminent)
                        .disabled(controller.isFirstRun)
                }
            }
        }
        .padding(16)
    }

    /// Promo for the iOS companion app. Links to the project landing
    /// page so the URL survives release churn — the page hosts the
    /// current TestFlight / App Store URLs and can be updated without
    /// shipping a new Mac build.
    private var iosPromoCard: some View {
        Link(destination: URL(string: "https://gbellas.github.io/netmon-server/#get-the-ios-app")!) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: "iphone")
                    .font(.system(size: 32))
                    .foregroundStyle(.tint)
                    .frame(width: 40)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Get NetMon for iOS")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(.primary)
                    Text("Live dashboard on your phone, pairs with this server via QR code.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                Image(systemName: "arrow.up.right.square")
                    .foregroundStyle(.secondary)
            }
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 10).fill(.tint.opacity(0.08)))
        }
        .buttonStyle(.plain)
        .padding(.top, 8)
    }

    private var logs: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 1) {
                    ForEach(Array(controller.logTail.enumerated()), id: \.offset) { i, line in
                        Text(line)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                            .id(i)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
            }
            .onChange(of: controller.logTail.count) { _, new in
                if new > 0 { proxy.scrollTo(new - 1, anchor: .bottom) }
            }
        }
    }

    private func row(
        _ label: String, _ value: String,
        valueColor: Color = .primary, mono: Bool = false, small: Bool = false
    ) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label)
                .foregroundStyle(.secondary)
                .frame(width: 110, alignment: .leading)
            Text(value)
                .font(mono
                      ? (small ? .system(.caption, design: .monospaced)
                               : .system(.body, design: .monospaced))
                      : (small ? .caption : .body))
                .foregroundStyle(valueColor)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

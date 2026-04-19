import SwiftUI

/// Settings window, opened from the menu bar. Intentionally minimal —
/// this app is supposed to fade into the background once configured.
/// The main "change stuff" surface is the iPhone app's Settings → Devices.
struct PreferencesView: View {
    @EnvironmentObject private var controller: ServerController

    var body: some View {
        TabView {
            overviewTab
                .tabItem { Label("Overview", systemImage: "info.circle") }
            logsTab
                .tabItem { Label("Logs", systemImage: "doc.text") }
        }
        .padding(20)
    }

    private var overviewTab: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("NetMon Server").font(.largeTitle.bold())
            Divider()
            row(label: "Status", value: controller.statusDescription,
                valueColor: controller.status.isRunning ? .green : .orange)
            row(label: "URL",    value: controller.serverURL)
            row(label: "Token",
                value: controller.apiToken.map { "\($0.prefix(12))…" } ?? "(none)")
            row(label: "Runtime dir", value: controller.runtimeDir.path,
                mono: true, small: true)

            Spacer()

            HStack {
                Button("Open dashboard") {
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
    }

    private var logsTab: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(controller.logTail.enumerated()),
                            id: \.offset) { (i, line) in
                        Text(line)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                            .id(i)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 4)
            }
            .onChange(of: controller.logTail.count) { _, new in
                // Keep the view pinned to the bottom as new lines stream in.
                if new > 0 { proxy.scrollTo(new - 1, anchor: .bottom) }
            }
        }
    }

    private func row(
        label: String, value: String, valueColor: Color = .primary,
        mono: Bool = false, small: Bool = false
    ) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label)
                .foregroundStyle(.secondary)
                .frame(width: 110, alignment: .leading)
            Text(value)
                .font(mono
                      ? (small
                         ? .system(.caption, design: .monospaced)
                         : .system(.body, design: .monospaced))
                      : (small ? .caption : .body))
                .foregroundStyle(valueColor)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

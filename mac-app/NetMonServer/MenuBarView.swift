import SwiftUI

/// Contents of the menu-bar dropdown. Uses SwiftUI's native `Menu`
/// idioms (Button, Divider, Menu) which macOS renders as a real
/// NSMenu under the hood — no custom popover styling needed.
struct MenuBarContent: View {
    @EnvironmentObject private var controller: ServerController
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        // Status header — shows current state; not clickable.
        Text("NetMon Server — \(controller.statusDescription)")
            .font(.caption)

        Divider()

        if controller.status.isRunning {
            Button("Open dashboard (web UI)") {
                if let url = URL(string: controller.serverURL) {
                    NSWorkspace.shared.open(url)
                }
            }
            Button("Copy API token") {
                if let tok = controller.apiToken {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(tok, forType: .string)
                }
            }
            .disabled(controller.apiToken == nil)

            Divider()
            Button("Restart server") { controller.restart() }
            Button("Stop server")    { controller.stop() }
        } else {
            Button("Start server") { controller.start() }
                .disabled(controller.isFirstRun)
            if controller.isFirstRun {
                Text("First-run setup required")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }

        Divider()

        Button(controller.isFirstRun ? "Run setup…" : "Preferences…") {
            // `openWindow` in SwiftUI navigates to a `Window` scene by
            // id. We declared both "setup" and "preferences" windows
            // in the App body so either route works.
            openWindow(id: controller.isFirstRun ? "setup" : "preferences")
            NSApp.activate(ignoringOtherApps: true)
        }

        Divider()

        Button("Quit NetMon Server") {
            NSApp.terminate(nil)
        }
        .keyboardShortcut("q")
    }
}

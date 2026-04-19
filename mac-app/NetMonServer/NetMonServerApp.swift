import SwiftUI

/// NetMon Server menu-bar app.
///
/// Runs the Python FastAPI server as a subprocess and exposes a macOS
/// menu-bar entry for lifecycle control. No Dock icon — this is a
/// utility app, people don't switch to it, they peek at its status.
///
/// The heavy lifting lives in `ServerController`. This entry point is
/// just wiring: spawn the controller, mount the menu bar extra, and
/// show the setup window on first run.
@main
struct NetMonServerApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var controller = ServerController()

    var body: some Scene {
        MenuBarExtra {
            MenuBarContent()
                .environmentObject(controller)
        } label: {
            // The status bar icon reflects server health. SF Symbols
            // color tinting works through a plain Text wrapper.
            Image(systemName: controller.statusSymbolName)
        }
        .menuBarExtraStyle(.menu)

        // Optional "dashboard-like" window for settings. Not the same
        // as the iPhone dashboard — this is the server's own knobs:
        // port, auto-start, logs. Hidden by default; user opens from
        // the menu.
        Window("NetMon Server", id: "preferences") {
            PreferencesView()
                .environmentObject(controller)
                .frame(minWidth: 540, minHeight: 420)
        }
        .windowResizability(.contentSize)
        .defaultPosition(.center)

        Window("Setup NetMon Server", id: "setup") {
            SetupView()
                .environmentObject(controller)
                .frame(minWidth: 520, minHeight: 560)
        }
        .windowResizability(.contentSize)
        .defaultPosition(.center)
    }
}

/// Lean NSApplicationDelegate — we hide the Dock icon on launch so the
/// app is menu-bar-only. The launch-services metadata alone (LSUIElement)
/// could do it, but explicit is safer because the app bundle may be
/// built without Info.plist edits during development.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)    // no Dock icon
        // Silent once-per-24h GitHub releases check. Triggers a dialog
        // only when a newer version is available.
        UpdateChecker.shared.checkSilentlyIfDue()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        // False: closing the Preferences window shouldn't quit the
        // app — we're menu-bar-based, the user quits via "Quit NetMon
        // Server" menu item.
        return false
    }
}

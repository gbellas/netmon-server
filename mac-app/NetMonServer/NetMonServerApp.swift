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
/// Shared bridge so AppDelegate can reach the SwiftUI-owned controller
/// without being handed a reference — @StateObject can't be accessed
/// from AppDelegate's scope. The App scene writes here on init.
enum NetMonAppGlue {
    @MainActor static var sharedController: ServerController!
}

@main
struct NetMonServerApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var controller: ServerController

    init() {
        let c = ServerController()
        _controller = StateObject(wrappedValue: c)
        NetMonAppGlue.sharedController = c
    }

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

        // Main window — full NetMon UI (Dashboard / Devices / Events /
        // History / Server). Opened from the menu bar's "Show NetMon"
        // item, or auto-opened at launch once setup is complete.
        // The "preferences" id is preserved for back-compat with the
        // old menu-bar binding; both point at the same scene.
        Window("NetMon", id: "main") {
            MainWindowView()
                .environmentObject(controller)
                .frame(minWidth: 900, minHeight: 600)
        }
        .windowResizability(.contentSize)
        .defaultPosition(.center)

        Window("Setup NetMon Server", id: "setup") {
            SetupWindowRoot()
                .environmentObject(controller)
                .frame(minWidth: 600, minHeight: 560)
        }
        .windowResizability(.contentSize)
        .defaultPosition(.center)
    }
}

/// Routes the setup window to either the new first-run wizard or the
/// existing SetupView, based on whether the wizard has been completed.
/// Keeps the decision out of the App body so the Window scene remains
/// stable across state changes.
struct SetupWindowRoot: View {
    @EnvironmentObject private var controller: ServerController
    var body: some View {
        if WizardPersistence.isWizardComplete(controller: controller) {
            SetupView()
        } else {
            FirstRunWizardView()
        }
    }
}

/// Lean NSApplicationDelegate — we hide the Dock icon on launch so the
/// app is menu-bar-only. The launch-services metadata alone (LSUIElement)
/// could do it, but explicit is safer because the app bundle may be
/// built without Info.plist edits during development.
final class AppDelegate: NSObject, NSApplicationDelegate {
    /// Retained AppKit window for the first-run wizard. SwiftUI's
    /// `Window` scene + `openWindow` can't be reliably triggered before
    /// the user opens the MenuBarExtra (its body is lazy), so for the
    /// launch-time auto-open we host the wizard in a plain NSWindow.
    private var wizardWindow: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)    // no Dock icon
        // Silent once-per-24h GitHub releases check. Triggers a dialog
        // only when a newer version is available.
        UpdateChecker.shared.checkSilentlyIfDue()
        NotificationCenter.default.addObserver(
            self, selector: #selector(handleWizardFinished),
            name: Notification.Name("NetMonWizardFinished"), object: nil
        )
        // Kick off the first-run wizard immediately on launch if the
        // marker isn't present. We reach into the NSApp for the shared
        // controller; it's a @StateObject on the App scene so we fetch
        // it via a small helper.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in
            self?.presentWizardIfNeeded()
        }
    }

    @MainActor
    private func presentWizardIfNeeded() {
        guard let controller = NetMonAppGlue.sharedController else { return }
        if WizardPersistence.isWizardComplete(controller: controller) { return }
        if wizardWindow != nil { return }
        let hosting = NSHostingController(
            rootView: FirstRunWizardView()
                .environmentObject(controller)
        )
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 640, height: 620),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered, defer: false
        )
        win.title = "Setup NetMon Server"
        win.contentViewController = hosting
        win.center()
        win.isReleasedWhenClosed = false
        wizardWindow = win
        NSApp.setActivationPolicy(.regular)    // show in Dock while wizard is up
        NSApp.activate(ignoringOtherApps: true)
        win.makeKeyAndOrderFront(nil)
    }

    @objc private func handleWizardFinished() {
        wizardWindow?.close()
        wizardWindow = nil
        // Back to menu-bar only after the wizard is done.
        NSApp.setActivationPolicy(.accessory)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        // False: closing the Preferences window shouldn't quit the
        // app — we're menu-bar-based, the user quits via "Quit NetMon
        // Server" menu item.
        return false
    }
}

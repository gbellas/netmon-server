import AppKit
import Foundation

/// Polls GitHub releases for a newer version and, optionally, prompts
/// the user to open the release page. Two entry points:
///
/// 1. `checkSilentlyIfDue()` — called on launch. Uses a per-day cache
///    (`UserDefaults` key `lastUpdateCheck`) so it only hits GitHub
///    once per 24h. No UI unless an update is available.
///
/// 2. `checkNow(interactive:)` — invoked from the "Check for Updates…"
///    menu item. Always hits GitHub; always shows a dialog (either
///    "Update available" or "You're up to date").
///
/// Release source of truth: the `gbellas/netmon-server` GitHub repo's
/// /releases/latest endpoint. `tag_name` is expected to be "vX.Y.Z"
/// (leading v optional).
final class UpdateChecker {
    static let shared = UpdateChecker()

    private let releasesURL = URL(string: "https://api.github.com/repos/gbellas/netmon-server/releases/latest")!
    private let releasePageURL = URL(string: "https://github.com/gbellas/netmon-server/releases/latest")!
    private let lastCheckKey = "lastUpdateCheck"

    func checkSilentlyIfDue() {
        let last = UserDefaults.standard.object(forKey: lastCheckKey) as? Date
        if let last, Date().timeIntervalSince(last) < 24 * 3600 {
            return
        }
        check(interactive: false)
    }

    func checkNow(interactive: Bool) {
        check(interactive: interactive)
    }

    private func check(interactive: Bool) {
        var req = URLRequest(url: releasesURL)
        req.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        URLSession.shared.dataTask(with: req) { [weak self] data, _, err in
            guard let self else { return }
            UserDefaults.standard.set(Date(), forKey: self.lastCheckKey)
            guard err == nil, let data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let tag = obj["tag_name"] as? String else {
                if interactive { DispatchQueue.main.async { self.showError() } }
                return
            }
            let latest = Self.normalize(tag)
            let current = Self.normalize(Self.bundleVersion)
            let newer = Self.isNewer(latest, than: current)
            DispatchQueue.main.async {
                if newer {
                    self.promptUpdate(latest: latest)
                } else if interactive {
                    self.showUpToDate(version: current)
                }
            }
        }.resume()
    }

    private static var bundleVersion: String {
        (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "0.0.0"
    }

    private static func normalize(_ v: String) -> String {
        var s = v
        if s.hasPrefix("v") { s.removeFirst() }
        return s
    }

    /// Lexicographic numeric compare on dot-separated components.
    private static func isNewer(_ a: String, than b: String) -> Bool {
        let ap = a.split(separator: ".").map { Int($0) ?? 0 }
        let bp = b.split(separator: ".").map { Int($0) ?? 0 }
        for i in 0..<max(ap.count, bp.count) {
            let x = i < ap.count ? ap[i] : 0
            let y = i < bp.count ? bp[i] : 0
            if x != y { return x > y }
        }
        return false
    }

    private func promptUpdate(latest: String) {
        let alert = NSAlert()
        alert.messageText = "Update available: v\(latest)"
        alert.informativeText = "A newer version of NetMon Server is available. Download now?"
        alert.addButton(withTitle: "Download")
        alert.addButton(withTitle: "Later")
        if alert.runModal() == .alertFirstButtonReturn {
            NSWorkspace.shared.open(releasePageURL)
        }
    }

    private func showUpToDate(version: String) {
        let alert = NSAlert()
        alert.messageText = "You're up to date."
        alert.informativeText = "NetMon Server v\(version) is the latest release."
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    private func showError() {
        let alert = NSAlert()
        alert.messageText = "Update check failed"
        alert.informativeText = "Couldn't reach GitHub. Try again later."
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }
}

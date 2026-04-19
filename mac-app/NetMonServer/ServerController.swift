import Foundation
import SwiftUI
import AppKit

/// Manages the Python server subprocess.
///
/// Responsibilities:
///  - Start / stop / restart `run.sh` (which launches uvicorn)
///  - Track lifecycle so the menu bar icon + menus reflect reality
///  - Capture stdout+stderr into a rolling in-memory buffer (~500 lines)
///    for the "View logs" menu item; we don't need to ship a huge log
///    viewer, just a "what happened most recently" peek
///  - Auto-restart on crash (up to 5x in 60s, then give up)
///  - Generate the API token on first run so the Setup window can show
///    the user their QR code + paste-ready value
///
/// Design note: Swift 5.10 still doesn't have ObservedObject auto-
/// dependency tracking for @MainActor classes like SwiftUI wants, so
/// we're using @MainActor + ObservableObject explicitly. @Published
/// triggers view refresh.
@MainActor
final class ServerController: ObservableObject {

    enum Status: Equatable {
        case stopped
        case starting
        case running(pid: Int32)
        case crashed(lastError: String)

        var isRunning: Bool {
            if case .running = self { return true }
            return false
        }
    }

    @Published var status: Status = .stopped
    /// Most recent ~500 lines of combined stdout/stderr. Used by the
    /// log peek window; trimmed to keep memory bounded.
    @Published var logTail: [String] = []
    /// Current API token (read from .env). nil before first run.
    @Published var apiToken: String?
    /// Server URL (for pasting into the iPhone app / generating QR).
    @Published var serverURL: String = "http://localhost:8077"

    private var process: Process?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?
    private var restartCount = 0
    private var restartWindowStart: Date = .distantPast

    // MARK: - Paths inside the app bundle

    /// Where the bundled Python + server code live inside the .app.
    /// `prepare-bundle.sh` writes to `mac-app/NetMonServer/Resources/`
    /// which Xcode copies to `Contents/Resources/Resources/` (folder
    /// references nest one level under the outer Resources dir).
    private var bundlePythonRoot: URL {
        Bundle.main.resourceURL!
            .appendingPathComponent("Resources", isDirectory: true)
            .appendingPathComponent("python-bundle", isDirectory: true)
    }

    /// Directory the server actually runs from. NOT inside the app
    /// bundle (that's read-only for codesigned apps) — we copy the
    /// server Python files to `~/Library/Application Support/NetMonServer/`
    /// on first launch and run from there. Lets `.env` and
    /// `config.local.yaml` persist between app updates.
    var runtimeDir: URL {
        let base = FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask
        ).first!
        return base.appendingPathComponent("NetMonServer", isDirectory: true)
    }

    var envFile: URL { runtimeDir.appendingPathComponent(".env") }
    var configLocalFile: URL {
        runtimeDir.appendingPathComponent("config.local.yaml")
    }

    // MARK: - First-run detection

    /// True if `.env` with a valid token doesn't exist yet. Setup UI
    /// should walk the user through generating one.
    var isFirstRun: Bool { apiToken == nil || (apiToken?.isEmpty ?? true) }

    init() {
        // Ensure runtime dir exists; copy server code if this is the
        // first launch (or if the bundled code is newer than what's
        // in Application Support, e.g. after an app update).
        try? FileManager.default.createDirectory(
            at: runtimeDir, withIntermediateDirectories: true
        )
        syncServerFilesFromBundle()
        loadExistingToken()
    }

    // MARK: - Start / stop

    func start() {
        guard !status.isRunning, case .starting = status else {
            if status.isRunning { return }
            self.status = .starting
            actuallyStart()
            return
        }
        actuallyStart()
    }

    private func actuallyStart() {
        let proc = Process()
        proc.currentDirectoryURL = runtimeDir

        // The bundled Python interpreter + the server script paths.
        let python = bundlePythonRoot
            .appendingPathComponent("bin/python3")
        let server = runtimeDir.appendingPathComponent("server.py")
        guard FileManager.default.isExecutableFile(atPath: python.path),
              FileManager.default.fileExists(atPath: server.path) else {
            self.status = .crashed(
                lastError: "Missing bundled Python or server script. "
                         + "App bundle may be corrupt — reinstall."
            )
            return
        }

        // Using uvicorn as the CLI rather than importing server.app in a
        // Python wrapper: matches what `run.sh` does in dev, so the
        // same server.py code paths execute (including startup event
        // handlers and the `on_event('startup')` hook). uvicorn lives
        // at <bundle>/bin/uvicorn after pip-install in the prepare step.
        proc.executableURL = bundlePythonRoot
            .appendingPathComponent("bin/uvicorn")
        proc.arguments = [
            "server:app",
            "--host", "0.0.0.0",
            "--port", "8077",
            "--log-level", "info",
        ]

        // Environment: pass through HOME (launchd Mac apps need it),
        // include the bundled Python's bin in PATH so subprocess shell
        // calls from server.py find the right binaries.
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "\(bundlePythonRoot.appendingPathComponent("bin").path):"
                   + (env["PATH"] ?? "/usr/bin:/bin")
        // Honor the user's .env via uvicorn's runtime — we tell the
        // subprocess to source it via dotenv… actually no, we inject
        // it directly so every Python module sees these vars without
        // dotenv being a dep. Parse lines, split on `=`.
        for (k, v) in loadEnvFile() {
            env[k] = v
        }
        proc.environment = env

        let out = Pipe(), err = Pipe()
        proc.standardOutput = out
        proc.standardError = err
        self.stdoutPipe = out
        self.stderrPipe = err
        tailPipe(out, label: "stdout")
        tailPipe(err, label: "stderr")

        proc.terminationHandler = { [weak self] p in
            Task { @MainActor in self?.handleTermination(p) }
        }

        do {
            try proc.run()
            self.process = proc
            self.status = .running(pid: proc.processIdentifier)
            self.appendLog("[controller] started server, pid=\(proc.processIdentifier)")
        } catch {
            self.status = .crashed(
                lastError: "couldn't launch: \(error.localizedDescription)"
            )
        }
    }

    func stop() {
        guard let proc = process else {
            status = .stopped
            return
        }
        proc.terminate()
        // Wait briefly for graceful exit; SIGKILL if it ignores SIGTERM.
        DispatchQueue.global().asyncAfter(deadline: .now() + 3) { [weak proc] in
            if proc?.isRunning == true {
                kill(proc!.processIdentifier, SIGKILL)
            }
        }
    }

    func restart() {
        stop()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            self?.start()
        }
    }

    private func handleTermination(_ p: Process) {
        let code = p.terminationStatus
        appendLog("[controller] server exited with status \(code)")
        process = nil
        if code == 0 || code == SIGTERM {
            status = .stopped
            return
        }
        // Auto-restart with a rate limit. More than 5 crashes in 60s
        // means something is deeply wrong — stop trying so we don't
        // hammer the logs.
        let now = Date()
        if now.timeIntervalSince(restartWindowStart) > 60 {
            restartWindowStart = now
            restartCount = 0
        }
        restartCount += 1
        if restartCount > 5 {
            status = .crashed(lastError:
                "server crashed >5 times in 60s; auto-restart disabled. "
              + "Check logs, then use the menu to restart manually.")
            return
        }
        appendLog("[controller] auto-restarting (attempt \(restartCount))…")
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            self?.start()
        }
    }

    // MARK: - Log capture

    private func tailPipe(_ pipe: Pipe, label: String) {
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let str = String(data: data, encoding: .utf8)
            else { return }
            Task { @MainActor in
                for line in str.split(separator: "\n", omittingEmptySubsequences: true) {
                    self?.appendLog("[\(label)] \(line)")
                }
            }
        }
    }

    private func appendLog(_ line: String) {
        logTail.append(line)
        if logTail.count > 500 { logTail.removeFirst(logTail.count - 500) }
    }

    // MARK: - Config + token management

    private func loadExistingToken() {
        guard let contents = try? String(contentsOf: envFile, encoding: .utf8) else {
            apiToken = nil; return
        }
        for line in contents.split(separator: "\n") {
            if line.hasPrefix("NETMON_API_TOKEN=") {
                apiToken = String(line.dropFirst("NETMON_API_TOKEN=".count))
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                return
            }
        }
        apiToken = nil
    }

    /// Generate a new API token, write it to `.env`, and update the
    /// published property. Called from the Setup window's "Generate"
    /// button.
    func generateApiToken() -> String {
        // 32 bytes, URL-safe base64 without padding. Matches the
        // server's own auth.init_token() output format.
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        let tok = Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
        upsertEnv("NETMON_API_TOKEN", value: tok)
        apiToken = tok
        return tok
    }

    /// Parse `.env` into a dict. Tolerant of blank lines + comments.
    private func loadEnvFile() -> [String: String] {
        guard let contents = try? String(contentsOf: envFile, encoding: .utf8)
        else { return [:] }
        var out: [String: String] = [:]
        for raw in contents.split(separator: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty, !line.hasPrefix("#"), let eq = line.firstIndex(of: "=")
            else { continue }
            let k = String(line[..<eq])
            let v = String(line[line.index(after: eq)...])
            out[k] = v
        }
        return out
    }

    /// Insert-or-replace a single key in `.env`. Preserves formatting
    /// of other lines (unlike a naive "dump the whole dict back").
    private func upsertEnv(_ key: String, value: String) {
        var lines: [String] = []
        if let existing = try? String(contentsOf: envFile, encoding: .utf8) {
            lines = existing.split(separator: "\n", omittingEmptySubsequences: false)
                .map(String.init)
        }
        let prefix = "\(key)="
        var found = false
        for i in lines.indices {
            if lines[i].hasPrefix(prefix) {
                lines[i] = "\(prefix)\(value)"
                found = true
                break
            }
        }
        if !found { lines.append("\(prefix)\(value)") }
        let output = lines.joined(separator: "\n") + "\n"
        try? output.write(to: envFile, atomically: true, encoding: .utf8)
        // chmod 600 — .env holds device passwords.
        try? FileManager.default.setAttributes(
            [.posixPermissions: NSNumber(value: 0o600)],
            ofItemAtPath: envFile.path
        )
    }

    // MARK: - Bundle → runtime sync

    /// Copy the server's Python files from the app bundle into the
    /// runtime dir on launch (or update). Skips files already up-to-date
    /// and NEVER overwrites `.env` / `config.local.yaml` / `secrets/`
    /// (those are operator state, not app code).
    private func syncServerFilesFromBundle() {
        let fm = FileManager.default
        let bundled = Bundle.main.resourceURL!
            .appendingPathComponent("Resources", isDirectory: true)
            .appendingPathComponent("server-code", isDirectory: true)
        guard fm.fileExists(atPath: bundled.path) else {
            return    // development mode: no bundled code, expect user to
                     // point runtime dir at their dev checkout
        }
        let preservePaths: Set<String> = [
            ".env", "config.local.yaml", "logs",
            "secrets", "push_tokens.json", "scheduled_config.json",
            "alerts_config.json",
        ]
        guard let entries = try? fm.contentsOfDirectory(
            at: bundled, includingPropertiesForKeys: nil
        ) else { return }
        for src in entries {
            let name = src.lastPathComponent
            if preservePaths.contains(name) { continue }
            let dst = runtimeDir.appendingPathComponent(name)
            // Always overwrite — simpler than diffing, and files in
            // the bundle are authoritative. Slight cost on app
            // startup but runs once.
            try? fm.removeItem(at: dst)
            try? fm.copyItem(at: src, to: dst)
        }
    }

    // MARK: - Menu bar helpers

    var statusSymbolName: String {
        switch status {
        case .stopped:   return "circle.slash"
        case .starting:  return "arrow.triangle.2.circlepath"
        case .running:   return "dot.circle.fill"
        case .crashed:   return "exclamationmark.triangle.fill"
        }
    }

    var statusDescription: String {
        switch status {
        case .stopped:            return "Stopped"
        case .starting:           return "Starting…"
        case .running(let pid):   return "Running (pid \(pid))"
        case .crashed(let err):   return "Crashed — \(err)"
        }
    }
}

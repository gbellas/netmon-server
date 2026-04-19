import SwiftUI
import AppKit
import Foundation
import Security
import CoreImage.CIFilterBuiltins

// MARK: - Wizard model

/// Drives the 7-step first-run wizard. Holds all transient form state so
/// the view layer is a dumb projection of the model, and persists a
/// simple resume marker to UserDefaults keyed by step — if the user
/// force-quits mid-wizard, the next launch opens at the same step.
@MainActor
final class FirstRunWizardModel: ObservableObject {
    enum DeviceKind: String, CaseIterable, Identifiable {
        case unifiNetwork = "unifi_network"
        case peplinkRouter = "peplink_router"
        case peplinkDerived = "peplink_derived"
        case icmpPing = "icmp_ping"
        var id: String { rawValue }
        var display: String {
            switch self {
            case .unifiNetwork:   return "UniFi Network"
            case .peplinkRouter:  return "Peplink Router"
            case .peplinkDerived: return "Peplink Derived"
            case .icmpPing:       return "ICMP Ping Target"
            }
        }
    }

    // Published wizard state
    @Published var step: Int = 0              // 0..6
    @Published var token: String = ""
    @Published var tokenCustom: String = ""
    @Published var tokenError: String?

    // Step 3 — first device
    @Published var deviceKind: DeviceKind = .unifiNetwork
    @Published var deviceId: String = "gateway"
    @Published var deviceName: String = "Home Gateway"
    @Published var host: String = ""
    @Published var username: String = "admin"
    @Published var password: String = ""
    @Published var sshUsername: String = "admin"
    @Published var sshPassword: String = ""
    @Published var pingTargetsText: String = "8.8.8.8\n1.1.1.1"
    @Published var deviceTestResult: String?
    @Published var deviceTestOK: Bool = false
    @Published var deviceTesting: Bool = false

    // Step 4 — InControl 2
    @Published var icSkip: Bool = true
    @Published var icClientId: String = ""
    @Published var icClientSecret: String = ""
    @Published var icOrgId: String = ""
    @Published var icTestResult: String?
    @Published var icTestOK: Bool = false
    @Published var icTesting: Bool = false

    // Step 5 — pairing URL
    @Published var pairingLANHost: String = "localhost:8077"

    private let defaults = UserDefaults.standard
    private let stepKey = "NetMonFirstRunWizard.step"
    private let tokenKey = "NetMonFirstRunWizard.token"

    init() {
        step = defaults.integer(forKey: stepKey)
        if step < 0 || step > 6 { step = 0 }
        if let t = defaults.string(forKey: tokenKey), !t.isEmpty { token = t }
        pairingLANHost = Self.primaryLANHost() ?? "localhost:8077"
    }

    func persistStep() {
        defaults.set(step, forKey: stepKey)
        if !token.isEmpty { defaults.set(token, forKey: tokenKey) }
    }

    func clearProgressMarker() {
        defaults.removeObject(forKey: stepKey)
        defaults.removeObject(forKey: tokenKey)
    }

    // MARK: - Token generation

    /// 64 hex characters (32 bytes) via SecRandomCopyBytes.
    static func generate64HexToken() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        if status != errSecSuccess {
            // Fall back to arc4random — still cryptographically random on
            // Darwin. Shouldn't happen in practice.
            for i in 0..<bytes.count { bytes[i] = UInt8.random(in: 0...255) }
        }
        return bytes.map { String(format: "%02x", $0) }.joined()
    }

    func autoGenerateToken() {
        token = Self.generate64HexToken()
        tokenCustom = token
        tokenError = nil
    }

    func applyCustomToken() -> Bool {
        let trimmed = tokenCustom.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.count < 16 {
            tokenError = "Token must be at least 16 characters."
            return false
        }
        token = trimmed
        tokenError = nil
        return true
    }

    // MARK: - Primary LAN host discovery

    /// Walk interfaces via getifaddrs; return "<ip>:8077" for the first
    /// non-loopback IPv4 address on an UP + RUNNING interface.
    static func primaryLANHost(port: Int = 8077) -> String? {
        var ifaddrPtr: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&ifaddrPtr) == 0, let first = ifaddrPtr else {
            return nil
        }
        defer { freeifaddrs(ifaddrPtr) }
        var ptr: UnsafeMutablePointer<ifaddrs>? = first
        while let curr = ptr {
            let flags = Int32(curr.pointee.ifa_flags)
            let upRunning = (flags & IFF_UP) != 0 && (flags & IFF_RUNNING) != 0
            let notLoopback = (flags & IFF_LOOPBACK) == 0
            if upRunning, notLoopback, let sa = curr.pointee.ifa_addr,
               sa.pointee.sa_family == UInt8(AF_INET) {
                var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
                let sz = socklen_t(MemoryLayout<sockaddr_in>.size)
                if getnameinfo(sa, sz, &host, socklen_t(host.count),
                               nil, 0, NI_NUMERICHOST) == 0 {
                    let ip = String(cString: host)
                    if !ip.hasPrefix("169.254.") {      // skip link-local
                        return "\(ip):\(port)"
                    }
                }
            }
            ptr = curr.pointee.ifa_next
        }
        return nil
    }

    // MARK: - Pairing URL

    var pairingURL: String {
        let url = "http://\(pairingLANHost)"
        let urlEnc = Data(url.utf8).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
        let tokEnc = Data(token.utf8).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
        return "netmon://pair?url=\(urlEnc)&token=\(tokEnc)"
    }
}

// MARK: - URLSession allowing self-signed

/// Peplink + UniFi ship with self-signed certs by default. Standard
/// URLSession will refuse them — swap in a delegate that accepts any
/// server trust for these specifically-scoped test calls. We don't reuse
/// this for general traffic; it lives and dies inside the wizard.
final class InsecureURLSessionDelegate: NSObject, URLSessionDelegate {
    func urlSession(_ session: URLSession,
                    didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition,
                                                  URLCredential?) -> Void) {
        if challenge.protectionSpace.authenticationMethod
            == NSURLAuthenticationMethodServerTrust,
           let trust = challenge.protectionSpace.serverTrust {
            completionHandler(.useCredential, URLCredential(trust: trust))
            return
        }
        completionHandler(.performDefaultHandling, nil)
    }
}

enum InsecureSession {
    /// One session shared within the wizard. Delegate retained on the
    /// session so it outlives the call sites.
    static let shared: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 10
        cfg.httpCookieStorage = HTTPCookieStorage.sharedCookieStorage(
            forGroupContainerIdentifier: "NetMonWizard"
        )
        cfg.httpCookieAcceptPolicy = .always
        return URLSession(configuration: cfg,
                          delegate: InsecureURLSessionDelegate(),
                          delegateQueue: nil)
    }()
}

// MARK: - Test call implementations

enum DeviceTest {
    struct TestError: Error { let message: String }

    /// POST JSON login to UniFi, then a follow-up GET to confirm the
    /// session. Returns a description on success, throws on failure.
    static func unifi(host: String, username: String,
                      password: String) async throws -> String {
        guard !host.isEmpty else { throw TestError(message: "Host is empty.") }
        let base = "https://\(host)"
        guard let loginURL = URL(string: "\(base)/api/auth/login") else {
            throw TestError(message: "Invalid host.")
        }
        var req = URLRequest(url: loginURL)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body = try JSONSerialization.data(
            withJSONObject: ["username": username, "password": password]
        )
        req.httpBody = body
        let (_, resp) = try await InsecureSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse else {
            throw TestError(message: "No HTTP response.")
        }
        if http.statusCode != 200 {
            throw TestError(message: "Login failed: HTTP \(http.statusCode)")
        }
        // Follow-up confirms the cookie jar has a working session.
        if let followURL = URL(string: "\(base)/proxy/network/integration/v1/sites") {
            var fr = URLRequest(url: followURL)
            fr.setValue("application/json", forHTTPHeaderField: "Accept")
            let (_, r2) = try await InsecureSession.shared.data(for: fr)
            if let h2 = r2 as? HTTPURLResponse {
                if h2.statusCode == 200 {
                    return "UniFi reached (HTTP 200, sites endpoint OK)"
                }
                return "UniFi login OK; sites endpoint HTTP \(h2.statusCode). "
                     + "Token probably needs older /api/s/default/stat path — "
                     + "continuing anyway."
            }
        }
        return "UniFi login OK"
    }

    static func peplink(host: String, username: String,
                        password: String) async throws -> String {
        guard !host.isEmpty else { throw TestError(message: "Host is empty.") }
        let base = "https://\(host)"
        guard let loginURL = URL(string: "\(base)/api/login") else {
            throw TestError(message: "Invalid host.")
        }
        var req = URLRequest(url: loginURL)
        req.httpMethod = "POST"
        req.setValue("application/x-www-form-urlencoded",
                     forHTTPHeaderField: "Content-Type")
        func enc(_ s: String) -> String {
            s.addingPercentEncoding(
                withAllowedCharacters: .urlQueryAllowed
            ) ?? s
        }
        let form = "func=login&username=\(enc(username))&password=\(enc(password))"
        req.httpBody = Data(form.utf8)
        let (data, resp) = try await InsecureSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse else {
            throw TestError(message: "No HTTP response.")
        }
        if http.statusCode != 200 {
            throw TestError(message: "Login failed: HTTP \(http.statusCode)")
        }
        if let text = String(data: data, encoding: .utf8),
           text.contains("\"stat\":\"failed\"") || text.contains("error") {
            // Peplink sometimes 200's with an error JSON body — peek at it.
            if text.localizedCaseInsensitiveContains("password") {
                throw TestError(message: "Peplink rejected credentials.")
            }
        }
        // Follow up with status.system for a friendly name.
        if let statURL = URL(string: "\(base)/api/status.system") {
            let (d2, r2) = try await InsecureSession.shared.data(from: statURL)
            if let h2 = r2 as? HTTPURLResponse, h2.statusCode == 200,
               let obj = try? JSONSerialization.jsonObject(with: d2) as? [String: Any],
               let resp = obj["response"] as? [String: Any] {
                let name = (resp["name"] as? String)
                    ?? (resp["device"] as? [String: Any])?["name"] as? String
                    ?? "Peplink device"
                return "Peplink reached: \(name)"
            }
        }
        return "Peplink login OK"
    }

    static func icmpPing(target: String) async throws -> String {
        guard !target.isEmpty else { throw TestError(message: "No target.") }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/sbin/ping")
        proc.arguments = ["-c", "2", "-W", "1", target]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        do {
            try proc.run()
        } catch {
            throw TestError(message: "ping launch failed: \(error.localizedDescription)")
        }
        proc.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let out = String(data: data, encoding: .utf8) ?? ""
        if proc.terminationStatus == 0 {
            // Extract the avg RTT line if present.
            for line in out.components(separatedBy: "\n") {
                if line.contains("min/avg/max") { return line }
                if line.contains("time=") { return line.trimmingCharacters(in: .whitespaces) }
            }
            return "Ping OK (2/2 replies)"
        }
        throw TestError(message: "Ping timeout / unreachable.")
    }

    /// InControl 2 client-credentials OAuth check. Returns token preview
    /// string on success. NOTE: if your IC2 account uses a different grant
    /// type (e.g. password), this will fail — adjust in a follow-up.
    static func incontrol(clientId: String, clientSecret: String) async throws -> String {
        guard let url = URL(string: "https://api.ic.peplink.com/api/oauth2/token") else {
            throw TestError(message: "Bad URL.")
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/x-www-form-urlencoded",
                     forHTTPHeaderField: "Content-Type")
        func enc(_ s: String) -> String {
            s.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? s
        }
        let form = "client_id=\(enc(clientId))&client_secret=\(enc(clientSecret))"
              + "&grant_type=client_credentials"
        req.httpBody = Data(form.utf8)
        let (data, resp) = try await InsecureSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse else {
            throw TestError(message: "No HTTP response.")
        }
        if http.statusCode != 200 {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw TestError(message: "HTTP \(http.statusCode) — \(body.prefix(120))")
        }
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let tok = obj["access_token"] as? String {
            return "InControl OK (token …\(tok.suffix(6)))"
        }
        return "InControl OK"
    }
}

// MARK: - Config / env writers

/// Handles writing wizard results into the persisted server state.
/// Keeps ServerController unaware of wizard specifics — we read its
/// path helpers and write into them.
@MainActor
enum WizardPersistence {
    /// Append a single device entry under `devices:` in config.local.yaml.
    /// If config.local.yaml doesn't exist, seed it from config.yaml in
    /// the runtime dir. YAML is constructed by hand — the shape is
    /// small enough that a yaml library isn't worth the build cost.
    static func appendDevice(controller: ServerController,
                             model: FirstRunWizardModel) throws {
        let path = controller.configLocalFile
        let fm = FileManager.default
        if !fm.fileExists(atPath: path.path) {
            // Seed from config.yaml if present — keeps the ping_targets /
            // server / etc. sections intact.
            let source = controller.runtimeDir
                .appendingPathComponent("config.yaml")
            if fm.fileExists(atPath: source.path) {
                try? fm.copyItem(at: source, to: path)
            } else {
                try "devices: {}\n".write(to: path, atomically: true, encoding: .utf8)
            }
        }
        var text = (try? String(contentsOf: path, encoding: .utf8)) ?? ""
        let block = buildDeviceYAML(model: model)
        // Locate `devices:` top-level key and insert after it. If the
        // file uses `devices: {}` inline syntax, replace it with a
        // multiline form.
        if let range = text.range(
            of: #"(?m)^devices:\s*\{\s*\}\s*$"#,
            options: .regularExpression
        ) {
            text.replaceSubrange(range, with: "devices:")
        }
        if let range = text.range(
            of: #"(?m)^devices:\s*$"#, options: .regularExpression
        ) {
            // Insert right after the `devices:` line.
            let insertAt = text.index(after: range.upperBound)
            text.insert(contentsOf: block + "\n", at: insertAt)
        } else {
            // No devices: key at all — append one.
            if !text.hasSuffix("\n") { text += "\n" }
            text += "\ndevices:\n" + block + "\n"
        }
        try text.write(to: path, atomically: true, encoding: .utf8)
    }

    private static func buildDeviceYAML(model: FirstRunWizardModel) -> String {
        let id = model.deviceId.isEmpty ? "device1" : model.deviceId
        func q(_ s: String) -> String {
            // Quote with double quotes, escape \ and ".
            let esc = s.replacingOccurrences(of: "\\", with: "\\\\")
                       .replacingOccurrences(of: "\"", with: "\\\"")
            return "\"\(esc)\""
        }
        var lines: [String] = []
        lines.append("  \(id):")
        lines.append("    kind: \(model.deviceKind.rawValue)")
        lines.append("    name: \(q(model.deviceName))")
        switch model.deviceKind {
        case .unifiNetwork, .peplinkRouter, .peplinkDerived:
            lines.append("    host: \(q(model.host))")
            lines.append("    username: \(q(model.username))")
            lines.append("    password: \(q(model.password))")
            lines.append("    poll_interval: 15")
            lines.append("    verify_ssl: false")
            if model.deviceKind == .peplinkRouter, !model.sshUsername.isEmpty {
                lines.append("    ssh:")
                lines.append("      enabled: false")
                lines.append("      port: 22")
                lines.append("      username: \(q(model.sshUsername))")
                lines.append("      password: \(q(model.sshPassword))")
                lines.append("      ssh_timeout: 10")
            }
        case .icmpPing:
            lines.append("    targets:")
            let targets = model.pingTargetsText
                .split(separator: "\n")
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
            for t in targets {
                lines.append("      - name: \(q(t))")
                lines.append("        host: \(q(t))")
            }
        }
        return lines.joined(separator: "\n")
    }

    /// Write NETMON_API_TOKEN and optionally IC2 creds to .env. Uses the
    /// same upsert approach ServerController uses internally, but we
    /// implement it here since that method is private.
    static func writeEnv(controller: ServerController,
                         token: String,
                         icClientId: String?,
                         icClientSecret: String?) throws {
        let envFile = controller.envFile
        var lines: [String] = []
        if let existing = try? String(contentsOf: envFile, encoding: .utf8) {
            lines = existing.split(separator: "\n", omittingEmptySubsequences: false)
                .map(String.init)
        }
        func upsert(_ key: String, _ value: String) {
            let prefix = "\(key)="
            var found = false
            for i in lines.indices where lines[i].hasPrefix(prefix) {
                lines[i] = "\(prefix)\(value)"
                found = true
                break
            }
            if !found { lines.append("\(prefix)\(value)") }
        }
        upsert("NETMON_API_TOKEN", token)
        if let id = icClientId, !id.isEmpty {
            upsert("NETMON_INCONTROL_CLIENT_ID", id)
        }
        if let s = icClientSecret, !s.isEmpty {
            upsert("NETMON_INCONTROL_CLIENT_SECRET", s)
        }
        let output = lines.joined(separator: "\n")
            .trimmingCharacters(in: .whitespacesAndNewlines) + "\n"
        try output.write(to: envFile, atomically: true, encoding: .utf8)
        try? FileManager.default.setAttributes(
            [.posixPermissions: NSNumber(value: 0o600)],
            ofItemAtPath: envFile.path
        )
    }

    /// Update `incontrol.enabled` and `incontrol.org_id` inside
    /// config.local.yaml. Uses line-level rewrite.
    static func writeInControlOrgId(controller: ServerController,
                                    orgId: String) throws {
        let path = controller.configLocalFile
        let fm = FileManager.default
        if !fm.fileExists(atPath: path.path) {
            let source = controller.runtimeDir
                .appendingPathComponent("config.yaml")
            if fm.fileExists(atPath: source.path) {
                try? fm.copyItem(at: source, to: path)
            }
        }
        var text = (try? String(contentsOf: path, encoding: .utf8)) ?? ""
        // Replace existing incontrol block if present, else append.
        if text.range(of: #"(?m)^incontrol:"#, options: .regularExpression) != nil {
            // Simple line-level substitutions.
            text = text.replacingOccurrences(
                of: #"(?m)^(\s+)enabled:\s*(true|false)\s*$"#,
                with: "$1enabled: true",
                options: .regularExpression
            )
            text = text.replacingOccurrences(
                of: #"(?m)^(\s+)org_id:\s*\".*\"\s*$"#,
                with: "$1org_id: \"\(orgId)\"",
                options: .regularExpression
            )
            text = text.replacingOccurrences(
                of: #"(?m)^(\s+)org_id:\s*\S*\s*$"#,
                with: "$1org_id: \"\(orgId)\"",
                options: .regularExpression
            )
        } else {
            if !text.hasSuffix("\n") { text += "\n" }
            text += "\nincontrol:\n  enabled: true\n  org_id: \"\(orgId)\"\n"
                  + "  poll_interval: 60\n  event_limit: 30\n"
        }
        try text.write(to: path, atomically: true, encoding: .utf8)
    }

    /// Marker file that means "wizard has been completed at least once."
    /// Lives inside ServerController.runtimeDir so it's alongside other
    /// operator state.
    static func markerURL(controller: ServerController) -> URL {
        controller.runtimeDir.appendingPathComponent(".wizard_complete")
    }

    static func isWizardComplete(controller: ServerController) -> Bool {
        FileManager.default.fileExists(atPath: markerURL(controller: controller).path)
    }

    static func writeMarker(controller: ServerController) throws {
        let url = markerURL(controller: controller)
        let stamp = ISO8601DateFormatter().string(from: Date())
        try stamp.write(to: url, atomically: true, encoding: .utf8)
    }
}

// MARK: - View

struct FirstRunWizardView: View {
    @EnvironmentObject private var controller: ServerController
    @StateObject private var model = FirstRunWizardModel()
    @Environment(\.dismissWindow) private var dismissWindow

    private let totalSteps = 7

    var body: some View {
        VStack(spacing: 0) {
            stepDots
                .padding(.top, 20)
                .padding(.bottom, 12)
            Divider()
            ScrollView {
                currentStepView
                    .padding(24)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            footer
                .padding(.horizontal, 24)
                .padding(.vertical, 12)
        }
        .frame(minWidth: 600, minHeight: 560)
        .onAppear {
            // Auto-generate the token if step 2 hasn't been completed.
            if model.token.isEmpty {
                model.autoGenerateToken()
            }
            model.persistStep()
        }
    }

    // MARK: Step indicator

    private var stepDots: some View {
        HStack(spacing: 8) {
            ForEach(0..<totalSteps, id: \.self) { i in
                Circle()
                    .fill(i == model.step ? Color.accentColor
                          : (i < model.step ? Color.accentColor.opacity(0.5)
                                            : Color.gray.opacity(0.3)))
                    .frame(width: 10, height: 10)
            }
        }
    }

    // MARK: Step content

    @ViewBuilder
    private var currentStepView: some View {
        switch model.step {
        case 0: welcomeStep
        case 1: tokenStep
        case 2: deviceStep
        case 3: incontrolStep
        case 4: pairStep
        case 5: alertsStep
        case 6: doneStep
        default: welcomeStep
        }
    }

    // Step 0
    private var welcomeStep: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Welcome to NetMon")
                .font(.largeTitle.bold())
            Text("NetMon polls your network gear — UniFi gateways, Peplink "
               + "routers, ICMP ping targets — and streams live status, "
               + "cellular signal, WAN health, and alerts to the NetMon "
               + "iPhone app. This wizard walks you through generating an "
               + "API token, adding your first device, optionally "
               + "connecting Peplink InControl 2, and pairing your iPhone.")
                .foregroundStyle(.secondary)
        }
    }

    // Step 1
    private var tokenStep: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("API Token").font(.title.bold())
            Text("This shared secret authenticates your iPhone to this "
               + "server. A 64-character hex token has been generated — "
               + "copy it, or paste your own (minimum 16 characters).")
                .foregroundStyle(.secondary)
            HStack {
                TextField("Token", text: $model.tokenCustom)
                    .font(.system(.body, design: .monospaced))
                    .textFieldStyle(.roundedBorder)
                Button("Regenerate") {
                    model.autoGenerateToken()
                }
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(model.tokenCustom, forType: .string)
                }
            }
            if let err = model.tokenError {
                Text(err).foregroundStyle(.red).font(.caption)
            }
            Text("Stored to the server's .env file with 0600 permissions.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    // Step 2
    private var deviceStep: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Add Your First Device").font(.title.bold())
            Picker("Kind", selection: $model.deviceKind) {
                ForEach(FirstRunWizardModel.DeviceKind.allCases) { k in
                    Text(k.display).tag(k)
                }
            }
            .pickerStyle(.segmented)
            .onChange(of: model.deviceKind) { _, _ in
                model.deviceTestOK = false
                model.deviceTestResult = nil
            }

            Form {
                TextField("ID (used as state key prefix)", text: $model.deviceId)
                TextField("Display name", text: $model.deviceName)
                switch model.deviceKind {
                case .unifiNetwork, .peplinkRouter, .peplinkDerived:
                    TextField("Host (IP or hostname)", text: $model.host)
                    TextField("Username", text: $model.username)
                    SecureField("Password", text: $model.password)
                    if model.deviceKind == .peplinkRouter {
                        TextField("SSH username (optional)", text: $model.sshUsername)
                        SecureField("SSH password (optional)", text: $model.sshPassword)
                    }
                case .icmpPing:
                    VStack(alignment: .leading) {
                        Text("Targets (one per line, IP or hostname)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        TextEditor(text: $model.pingTargetsText)
                            .font(.system(.body, design: .monospaced))
                            .frame(minHeight: 80)
                            .border(Color.gray.opacity(0.3))
                    }
                }
            }
            HStack {
                Button(model.deviceTesting ? "Testing…" : "Test") {
                    Task { await runDeviceTest() }
                }
                .disabled(model.deviceTesting)
                if let r = model.deviceTestResult {
                    Text(r)
                        .font(.caption)
                        .foregroundStyle(model.deviceTestOK ? .green : .red)
                }
            }
        }
    }

    private func runDeviceTest() async {
        model.deviceTesting = true
        model.deviceTestResult = nil
        defer { model.deviceTesting = false }
        do {
            let msg: String
            switch model.deviceKind {
            case .unifiNetwork:
                msg = try await DeviceTest.unifi(
                    host: model.host, username: model.username,
                    password: model.password)
            case .peplinkRouter, .peplinkDerived:
                msg = try await DeviceTest.peplink(
                    host: model.host, username: model.username,
                    password: model.password)
            case .icmpPing:
                let first = model.pingTargetsText
                    .split(separator: "\n")
                    .map { $0.trimmingCharacters(in: .whitespaces) }
                    .first(where: { !$0.isEmpty }) ?? ""
                msg = try await DeviceTest.icmpPing(target: first)
            }
            model.deviceTestResult = "✓ " + msg
            model.deviceTestOK = true
        } catch let e as DeviceTest.TestError {
            model.deviceTestResult = "✗ " + e.message
            model.deviceTestOK = false
        } catch {
            model.deviceTestResult = "✗ " + error.localizedDescription
            model.deviceTestOK = false
        }
    }

    // Step 3
    private var incontrolStep: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("InControl 2 (optional)").font(.title.bold())
            Text("Peplink's cloud provides org-wide WAN usage and event "
               + "feeds alongside local polling. You can skip this now "
               + "and wire it up later.")
                .foregroundStyle(.secondary)
            Toggle("Skip for now", isOn: $model.icSkip)
            if !model.icSkip {
                Form {
                    TextField("Client ID", text: $model.icClientId)
                    SecureField("Client Secret", text: $model.icClientSecret)
                    TextField("Org ID", text: $model.icOrgId)
                }
                HStack {
                    Button(model.icTesting ? "Testing…" : "Test") {
                        Task { await runIcTest() }
                    }
                    .disabled(model.icTesting)
                    if let r = model.icTestResult {
                        Text(r)
                            .font(.caption)
                            .foregroundStyle(model.icTestOK ? .green : .red)
                    }
                }
            }
        }
    }

    private func runIcTest() async {
        model.icTesting = true
        model.icTestResult = nil
        defer { model.icTesting = false }
        do {
            let msg = try await DeviceTest.incontrol(
                clientId: model.icClientId,
                clientSecret: model.icClientSecret)
            model.icTestResult = "✓ " + msg
            model.icTestOK = true
        } catch let e as DeviceTest.TestError {
            model.icTestResult = "✗ " + e.message
            model.icTestOK = false
        } catch {
            model.icTestResult = "✗ " + error.localizedDescription
            model.icTestOK = false
        }
    }

    // Step 4
    private var pairStep: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Pair Your iPhone").font(.title.bold())
            HStack(alignment: .top, spacing: 20) {
                WizardQRCodeView(payload: model.pairingURL)
                    .frame(width: 240, height: 240)
                    .background(Color.white)
                    .cornerRadius(8)
                VStack(alignment: .leading, spacing: 8) {
                    Text("Scan with the NetMon iOS app — TestFlight link "
                       + "is TBD until Apple approves.")
                        .foregroundStyle(.secondary)
                    Text("Server URL: http://\(model.pairingLANHost)")
                        .font(.caption.monospaced())
                    Text("Token: \(model.token.prefix(12))…")
                        .font(.caption.monospaced())
                    Button("Copy pairing URL") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(model.pairingURL, forType: .string)
                    }
                }
            }
        }
    }

    // Step 5
    private var alertsStep: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Alerts").font(.title.bold())
            Text("Built-in alerts are on by default — WAN state flips, "
               + "cellular degradation, high loss, tunnel drops. You can "
               + "tune thresholds later from the main window's Alerts "
               + "panel. Push notifications arrive on paired iPhones.")
                .foregroundStyle(.secondary)
        }
    }

    // Step 6
    private var doneStep: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("You're all set").font(.title.bold())
            Text("NetMon is ready to start polling.")
                .foregroundStyle(.secondary)
            GroupBox {
                VStack(alignment: .leading, spacing: 6) {
                    Label("Server URL: http://\(model.pairingLANHost)",
                          systemImage: "network")
                    Label("Log file: ~/Library/Logs/NetMon/server.log "
                        + "(plus in-memory tail in the menu bar)",
                          systemImage: "doc.text")
                    Label("Menu bar icon up top — click it to open the "
                        + "dashboard or stop the server.",
                          systemImage: "menubar.rectangle")
                }
                .font(.callout)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    // MARK: Footer

    private var footer: some View {
        HStack {
            if model.step > 0 {
                Button("Back") {
                    model.step -= 1
                    model.persistStep()
                }
            }
            Spacer()
            if model.step == 2 {
                // Skip device or Add device
                Button("Skip") {
                    model.step += 1
                    model.persistStep()
                }
                Button("Add device") {
                    do {
                        try WizardPersistence.appendDevice(
                            controller: controller, model: model)
                        model.step += 1
                        model.persistStep()
                    } catch {
                        model.deviceTestResult =
                            "Failed to save: \(error.localizedDescription)"
                        model.deviceTestOK = false
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(!model.deviceTestOK)
            } else if model.step == 3 {
                Button("Skip") {
                    model.step += 1
                    model.persistStep()
                }
                Button("Continue") {
                    if !model.icSkip && model.icTestOK {
                        do {
                            try WizardPersistence.writeInControlOrgId(
                                controller: controller, orgId: model.icOrgId)
                        } catch {
                            // Non-fatal; surface and move on.
                            model.icTestResult =
                                "Saved partial: \(error.localizedDescription)"
                        }
                    }
                    model.step += 1
                    model.persistStep()
                }
                .buttonStyle(.borderedProminent)
            } else if model.step == 6 {
                Button("Finish") { finish() }
                    .buttonStyle(.borderedProminent)
            } else {
                Button("Continue") {
                    if model.step == 1 {
                        guard model.applyCustomToken() else { return }
                        do {
                            try WizardPersistence.writeEnv(
                                controller: controller,
                                token: model.token,
                                icClientId: nil, icClientSecret: nil)
                        } catch {
                            model.tokenError =
                                "Couldn't write .env: \(error.localizedDescription)"
                            return
                        }
                    }
                    model.step += 1
                    model.persistStep()
                }
                .buttonStyle(.borderedProminent)
            }
        }
    }

    private func finish() {
        // Persist IC2 creds + env in case user edited at step 3.
        if !model.icSkip {
            try? WizardPersistence.writeEnv(
                controller: controller,
                token: model.token,
                icClientId: model.icClientId,
                icClientSecret: model.icClientSecret)
        }
        try? WizardPersistence.writeMarker(controller: controller)
        model.clearProgressMarker()
        if !controller.status.isRunning {
            controller.start()
        }
        // Close the SwiftUI scene window if we're in that path; if we
        // were hosted in AppDelegate's NSWindow, the notification below
        // handles cleanup.
        dismissWindow(id: "setup")
        NotificationCenter.default.post(
            name: Notification.Name("NetMonWizardFinished"), object: nil
        )
    }
}

// MARK: - QR code view (local copy to avoid touching SetupView)

struct WizardQRCodeView: View {
    let payload: String
    var body: some View {
        let ciCtx = CIContext()
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(payload.utf8)
        filter.correctionLevel = "M"
        if let img = filter.outputImage {
            let scale: CGFloat = 960 / max(img.extent.width, 1)
            let scaled = img.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
            if let cg = ciCtx.createCGImage(scaled, from: scaled.extent) {
                return AnyView(Image(decorative: cg, scale: 1.0)
                    .resizable()
                    .interpolation(.none))
            }
        }
        return AnyView(Text("QR generation failed").foregroundStyle(.red))
    }
}

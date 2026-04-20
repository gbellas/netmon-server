import Foundation
import Combine

/// Thin client against the local NetMon server (127.0.0.1:8077).
///
/// The Mac app embeds the Python server and already knows the port +
/// token via `ServerController`. This client wires into both:
///   - REST for one-shot queries (devices, events, history)
///   - WebSocket for live state deltas (/ws)
///
/// All state is published on @MainActor so SwiftUI views can bind to
/// it directly without extra actor hops.
@MainActor
final class LocalAPIClient: ObservableObject {
    /// Flat key→value dict mirroring the server's state table. Views
    /// read individual keys via `state(_:)` instead of building typed
    /// models, matching how the iPhone app consumes this stream.
    @Published private(set) var state: [String: JSONValue] = [:]
    @Published private(set) var devices: [DeviceRecord] = []
    @Published private(set) var events: [ServerEvent] = []
    @Published private(set) var isConnected: Bool = false
    @Published private(set) var lastUpdate: Date?
    @Published private(set) var lastError: String?

    /// Bound to `ServerController.apiToken` so token regeneration flows
    /// through. Re-opens the websocket when the token changes.
    var apiToken: String? {
        didSet {
            guard oldValue != apiToken else { return }
            Task { await reconnectWebSocket() }
        }
    }

    private let baseURL = URL(string: "http://127.0.0.1:8077")!
    private let session: URLSession
    private var wsTask: URLSessionWebSocketTask?
    private var wsReceiveTask: Task<Void, Never>?
    private var pingTask: Task<Void, Never>?
    private var pollTimer: Timer?

    init() {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 10
        cfg.waitsForConnectivity = false
        self.session = URLSession(configuration: cfg)
    }

    // MARK: - Public API

    /// Kick off REST polls + open the WebSocket. Safe to call multiple
    /// times — subsequent calls cancel and restart cleanly.
    func start() {
        Task { await refreshDevices() }
        Task { await refreshEvents() }
        Task { await reconnectWebSocket() }
        // REST polling for things not covered by the WS stream.
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                await self?.refreshDevices()
                await self?.refreshEvents()
            }
        }
    }

    func stop() {
        pollTimer?.invalidate()
        pollTimer = nil
        wsReceiveTask?.cancel()
        wsReceiveTask = nil
        pingTask?.cancel()
        pingTask = nil
        wsTask?.cancel(with: .goingAway, reason: nil)
        wsTask = nil
        isConnected = false
    }

    /// Typed read of a state key. Returns nil if the key hasn't been
    /// published yet or the value doesn't coerce to the requested type.
    func string(_ key: String) -> String? { state[key]?.stringValue }
    func int(_ key: String)    -> Int?    { state[key]?.intValue }
    func double(_ key: String) -> Double? { state[key]?.doubleValue }
    func bool(_ key: String)   -> Bool?   { state[key]?.boolValue }

    // MARK: - REST

    func refreshDevices() async {
        struct Resp: Decodable { let devices: [DeviceRecord] }
        do {
            let resp: Resp = try await getJSON("/api/devices")
            self.devices = resp.devices
            self.lastError = nil
        } catch {
            self.lastError = "devices: \(error.localizedDescription)"
        }
    }

    func refreshEvents(limit: Int = 100) async {
        struct Resp: Decodable { let events: [ServerEvent] }
        do {
            let resp: Resp = try await getJSON("/api/events?limit=\(limit)")
            self.events = resp.events
        } catch {
            // Event log is best-effort — don't clobber lastError.
        }
    }

    /// Fetch a time-series history window for a given state key.
    /// The server exposes /api/history/{key}?range=<seconds>.
    func fetchHistory(key: String, rangeSeconds: Int) async -> [HistoryPoint] {
        struct Resp: Decodable { let points: [HistoryPoint] }
        do {
            let resp: Resp = try await getJSON(
                "/api/history/\(key)?range=\(rangeSeconds)"
            )
            return resp.points
        } catch {
            return []
        }
    }

    func enableWan(deviceId: String, wanIndex: Int, enabled: Bool) async throws {
        let verb = enabled ? "enable" : "disable"
        _ = try await postJSON(
            "/api/devices/\(deviceId)/wan/\(wanIndex)/\(verb)",
            body: [String: JSONValue]()
        )
    }

    // MARK: - REST helpers

    private func getJSON<T: Decodable>(_ path: String) async throws -> T {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        if let token = apiToken {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let (data, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder().decode(T.self, from: data)
    }

    @discardableResult
    private func postJSON(_ path: String, body: [String: JSONValue]) async throws -> Data {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let token = apiToken {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if !body.isEmpty {
            req.httpBody = try JSONEncoder().encode(body)
        }
        let (data, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        return data
    }

    // MARK: - WebSocket

    private func reconnectWebSocket() async {
        wsReceiveTask?.cancel()
        pingTask?.cancel()
        wsTask?.cancel(with: .goingAway, reason: nil)

        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        comps.scheme = "ws"
        comps.path = "/ws"
        if let token = apiToken {
            comps.queryItems = [URLQueryItem(name: "token", value: token)]
        }
        guard let url = comps.url else { return }
        let task = session.webSocketTask(with: url)
        self.wsTask = task
        task.resume()

        wsReceiveTask = Task { [weak self] in
            await self?.runReceiveLoop(task: task)
        }
        pingTask = Task { [weak self] in
            await self?.runPingLoop(task: task)
        }
    }

    private func runReceiveLoop(task: URLSessionWebSocketTask) async {
        while !Task.isCancelled {
            do {
                let msg = try await task.receive()
                switch msg {
                case .string(let s):
                    handleIncoming(s)
                case .data(let d):
                    if let s = String(data: d, encoding: .utf8) {
                        handleIncoming(s)
                    }
                @unknown default: break
                }
                if !isConnected {
                    isConnected = true
                    lastError = nil
                }
            } catch {
                isConnected = false
                lastError = "ws: \(error.localizedDescription)"
                try? await Task.sleep(for: .seconds(2))
                await reconnectWebSocket()
                return
            }
        }
    }

    private func runPingLoop(task: URLSessionWebSocketTask) async {
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(20))
            if Task.isCancelled { return }
            try? await task.send(.string(#"{"type":"ping"}"#))
        }
    }

    private func handleIncoming(_ text: String) {
        guard let data = text.data(using: .utf8) else { return }
        guard let obj = try? JSONDecoder().decode([String: JSONValue].self, from: data)
        else { return }
        // Server messages come in a few shapes:
        //   {"type":"state", "delta": {...}} or {"type":"state", "full": {...}}
        //   {"type":"pong"} (ignored)
        //   {"type":"event", "event": {...}}
        let type = obj["type"]?.stringValue
        switch type {
        case "state":
            if case .object(let delta)? = obj["delta"] {
                for (k, v) in delta { state[k] = v }
                lastUpdate = Date()
            } else if case .object(let full)? = obj["full"] {
                state = full
                lastUpdate = Date()
            }
        case "event":
            if case .object(let e)? = obj["event"],
               let decoded = try? JSONDecoder().decode(
                   ServerEvent.self,
                   from: JSONEncoder().encode(JSONValue.object(e))
               ) {
                events.insert(decoded, at: 0)
                if events.count > 500 { events.removeLast(events.count - 500) }
            }
        default:
            break
        }
    }
}


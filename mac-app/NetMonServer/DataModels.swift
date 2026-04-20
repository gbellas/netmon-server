import Foundation

/// One entry in /api/devices. Kept deliberately minimal — the Mac UI
/// uses fewer fields than the iPhone app because it has more screen
/// real estate to show the raw state dict directly.
struct DeviceRecord: Codable, Hashable, Identifiable {
    let id: String
    let kind: String
    let displayName: String
    let host: String
    let isMobile: Bool
    let capabilities: [String]

    enum CodingKeys: String, CodingKey {
        case id, kind, host, capabilities
        case displayName = "display_name"
        case isMobile    = "is_mobile"
    }

    var kindLabel: String {
        let base = kind.replacingOccurrences(of: "legacy_", with: "")
        switch base {
        case "peplink_router": return "Peplink"
        case "unifi_network":  return "UniFi"
        case "icmp_ping":      return "ICMP"
        default:
            return base
                .split(separator: "_")
                .map { $0.capitalized }
                .joined(separator: " ")
        }
    }

    var iconName: String {
        switch kind.replacingOccurrences(of: "legacy_", with: "") {
        case "peplink_router":  return isMobile ? "antenna.radiowaves.left.and.right" : "wifi.router"
        case "unifi_network":   return "network"
        case "icmp_ping":       return "dot.radiowaves.left.and.right"
        default:                return "cube"
        }
    }
}

/// One entry in /api/events. Fields mirror server.py's EventLog
/// schema. `ts` arrives as a Unix epoch in seconds.
struct ServerEvent: Codable, Identifiable, Hashable {
    let id: String
    let ts: Double
    let kind: String
    let message: String
    let severity: String?
    let device: String?

    var date: Date { Date(timeIntervalSince1970: ts) }

    /// SwiftUI color name. The Mac app doesn't pull in asset catalogs
    /// for these — views map the severity to an SF Symbol + Color at
    /// render time.
    var severityIcon: String {
        switch severity {
        case "critical", "error": return "exclamationmark.octagon.fill"
        case "warning":           return "exclamationmark.triangle.fill"
        case "info":              return "info.circle"
        default:                  return "circle.dashed"
        }
    }
}

/// One point in a /api/history response. `ts` is a Unix epoch.
struct HistoryPoint: Codable, Hashable {
    let ts: Double
    let value: Double

    var date: Date { Date(timeIntervalSince1970: ts) }
}

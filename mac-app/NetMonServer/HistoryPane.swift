import SwiftUI
import Charts

/// Time-series view for any state key. User picks a key from the
/// published state dict + a time range; we fetch /api/history and
/// render a Swift Chart. Works for anything numeric — WAN throughput,
/// latency, loss, CPU load, etc.
struct HistoryPane: View {
    @EnvironmentObject private var api: LocalAPIClient

    @State private var selectedKey: String = ""
    @State private var rangeMinutes: Int = 60
    @State private var points: [HistoryPoint] = []
    @State private var loading: Bool = false

    /// Numeric state keys — those whose last value is Int or Double.
    /// We don't try to plot strings / bools. Sorted alphabetically so
    /// the picker stays predictable.
    private var numericKeys: [String] {
        api.state
            .compactMap { key, value -> String? in
                switch value {
                case .int, .double: return key
                default:            return nil
                }
            }
            .sorted()
    }

    private let ranges: [(label: String, minutes: Int)] = [
        ("15m", 15), ("1h", 60), ("6h", 360), ("24h", 1440), ("7d", 10080),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            controls
            Divider()
            chart
        }
        .padding(16)
        .navigationTitle("History")
        .onChange(of: selectedKey) { _, _ in reload() }
        .onChange(of: rangeMinutes) { _, _ in reload() }
        .onAppear {
            // Default to a sensible first key if we have any. The
            // dashboard is the first place most users land, so picking
            // a throughput-looking key here gives a non-empty chart.
            if selectedKey.isEmpty, let first = numericKeys.first(where: {
                $0.contains("rx_bps") || $0.contains("latency")
            }) ?? numericKeys.first {
                selectedKey = first
            }
        }
    }

    private var controls: some View {
        HStack(spacing: 12) {
            Picker("Metric", selection: $selectedKey) {
                if selectedKey.isEmpty {
                    Text("Select a metric…").tag("")
                }
                ForEach(numericKeys, id: \.self) { key in
                    Text(key).tag(key)
                }
            }
            .frame(maxWidth: 320)

            Picker("Range", selection: $rangeMinutes) {
                ForEach(ranges, id: \.minutes) { r in
                    Text(r.label).tag(r.minutes)
                }
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 360)

            Spacer()
            if loading {
                ProgressView().controlSize(.small)
            }
        }
    }

    @ViewBuilder
    private var chart: some View {
        if selectedKey.isEmpty {
            ContentUnavailableView(
                "Pick a metric",
                systemImage: "chart.xyaxis.line",
                description: Text("Only numeric state keys are plottable.")
            )
        } else if points.isEmpty && !loading {
            ContentUnavailableView(
                "No history yet",
                systemImage: "tray",
                description: Text("The server hasn't recorded history for \(selectedKey) in this window.")
            )
        } else {
            Chart(points, id: \.ts) { point in
                LineMark(
                    x: .value("Time", point.date),
                    y: .value(selectedKey, point.value)
                )
                .interpolationMethod(.monotone)
                .foregroundStyle(.tint)
            }
            .chartXAxis {
                AxisMarks(values: .automatic) { value in
                    AxisGridLine()
                    AxisValueLabel(format: .dateTime.hour().minute())
                }
            }
            .frame(minHeight: 300)
        }
    }

    private func reload() {
        guard !selectedKey.isEmpty else { return }
        loading = true
        Task {
            let pts = await api.fetchHistory(
                key: selectedKey,
                rangeSeconds: rangeMinutes * 60
            )
            self.points = pts
            self.loading = false
        }
    }
}

import SwiftUI
import CoreImage.CIFilterBuiltins

/// First-run setup. Three steps on one screen:
///
///   1. Generate the API token (one tap — server-side no-op if already set)
///   2. Show server URL + token as a QR code for the iPhone to scan
///   3. Start the server
///
/// When the user finishes, the app becomes a background menu-bar utility.
/// They'll only come back here to change the port or re-copy the token.
struct SetupView: View {
    @EnvironmentObject private var controller: ServerController
    @Environment(\.dismissWindow) private var dismissWindow

    @State private var step: Int = 0     // 0 = intro, 1 = token, 2 = pair, 3 = done
    @State private var token: String = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Welcome to NetMon Server")
                .font(.largeTitle.bold())
            Text("Three quick steps and your iPhone will start receiving live network data.")
                .foregroundStyle(.secondary)

            Divider()

            stepBlock(
                index: 1,
                title: "Generate an API token",
                body: step >= 1 ? tokenReveal : AnyView(generateTokenButton)
            )

            stepBlock(
                index: 2,
                title: "Pair with your iPhone",
                body: step >= 2 ? AnyView(pairingBlock) : AnyView(
                    Text("Once you generate a token, scan the QR with your iPhone's NetMon app.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                )
            )

            stepBlock(
                index: 3,
                title: "Start polling",
                body: step >= 2 ? AnyView(startBlock) : AnyView(
                    Text("After pairing, the server starts and the app fills with live data.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                )
            )

            Spacer()

            HStack {
                Spacer()
                Button("Close") {
                    dismissWindow(id: "setup")
                }
                .controlSize(.large)
                Button("Start server now") {
                    controller.start()
                    dismissWindow(id: "setup")
                }
                .controlSize(.large)
                .buttonStyle(.borderedProminent)
                .disabled(token.isEmpty)
            }
        }
        .padding(24)
        .onAppear {
            // If a token already exists on disk, skip straight to step 2.
            if let existing = controller.apiToken, !existing.isEmpty {
                self.token = existing
                self.step = 2
            }
        }
    }

    // MARK: - Step 1 content

    private var generateTokenButton: AnyView {
        AnyView(
            VStack(alignment: .leading, spacing: 8) {
                Text("The API token is a shared secret between this server and your iPhone. It's generated once and stored in `.env` with 0600 permissions.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("Generate token") {
                    self.token = controller.generateApiToken()
                    self.step = 2
                }
                .controlSize(.large)
                .buttonStyle(.borderedProminent)
            }
        )
    }

    private var tokenReveal: AnyView {
        AnyView(
            HStack(spacing: 8) {
                Text(token)
                    .font(.system(.caption, design: .monospaced))
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(token, forType: .string)
                }
                .controlSize(.small)
            }
        )
    }

    // MARK: - Step 2 content

    private var pairingBlock: some View {
        HStack(alignment: .top, spacing: 20) {
            QRCodeView(payload: pairingURL)
                .frame(width: 180, height: 180)
                .background(Color.white)
                .cornerRadius(8)

            VStack(alignment: .leading, spacing: 6) {
                Text("Scan with your iPhone:")
                    .font(.subheadline.weight(.semibold))
                Text("1. Open NetMon on your iPhone")
                Text("2. Tap the gear icon")
                Text("3. Tap the QR scanner (top-right) and point the camera at this code")
                Text("\nOr paste manually:")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.top, 4)
                Text("URL: \(controller.serverURL)")
                    .font(.caption.monospaced())
                Text("Token: \(token.prefix(16))…")
                    .font(.caption.monospaced())
            }
            .font(.caption)
        }
    }

    /// URL format the iPhone app's (future) QR scanner knows how to parse.
    /// Scheme: `netmon://pair?url=<base64>&token=<base64>`. Choosing a
    /// custom scheme over embedding in the URL as a bare http query
    /// keeps the iPhone from trying to open Safari if the scanner
    /// hands off via `UIApplication.open(_:)` inadvertently.
    private var pairingURL: String {
        let urlEnc = Data(controller.serverURL.utf8).base64EncodedString()
        let tokenEnc = Data(token.utf8).base64EncodedString()
        return "netmon://pair?url=\(urlEnc)&token=\(tokenEnc)"
    }

    // MARK: - Step 3 content

    private var startBlock: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("When you click Start server, the Python FastAPI app launches in the background. It listens on port 8077 and polls any devices you configure through the iPhone app's Settings → Devices.")
                .font(.caption)
                .foregroundStyle(.secondary)
            if controller.status.isRunning {
                Label("Server is running", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                    .font(.subheadline)
            } else if case .crashed(let err) = controller.status {
                Label(err, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
                    .font(.caption)
            }
        }
    }

    // MARK: - Layout helper

    private func stepBlock(index: Int, title: String,
                           body: @autoclosure () -> some View) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Text("\(index)")
                .font(.title2.weight(.bold).monospacedDigit())
                .frame(width: 30, height: 30)
                .background(Circle().fill(Color.accentColor.opacity(0.2)))
            VStack(alignment: .leading, spacing: 6) {
                Text(title).font(.headline)
                body()
            }
        }
    }
}

// MARK: - QR code generator

struct QRCodeView: View {
    let payload: String

    var body: some View {
        let ciCtx = CIContext()
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(payload.utf8)
        filter.correctionLevel = "M"
        if let outputImage = filter.outputImage,
           let scaled = upscale(outputImage),
           let cgImage = ciCtx.createCGImage(scaled, from: scaled.extent) {
            return AnyView(Image(decorative: cgImage, scale: 1.0)
                .resizable()
                .interpolation(.none))
        }
        return AnyView(
            Text("QR generation failed")
                .foregroundStyle(.red)
        )
    }

    /// QR core outputs are tiny; without pixel-perfect upscale the
    /// SwiftUI scale handler blurs them. Scale to ~180px before handing
    /// off so Image().resizable() has something crisp to work with.
    private func upscale(_ img: CIImage) -> CIImage? {
        let targetSize: CGFloat = 720   // 4x the rendered frame for retina
        let scaleX = targetSize / img.extent.width
        let scaleY = targetSize / img.extent.height
        return img.transformed(
            by: CGAffineTransform(scaleX: scaleX, y: scaleY)
        )
    }
}

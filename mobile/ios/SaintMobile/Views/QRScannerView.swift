import SwiftUI
import AVFoundation

/// Scans the QR code the PC shows when you pair (saint://pair?…).
struct QRScannerView: View {
    let onFound: (String) -> Void
    let onCancel: () -> Void

    var body: some View {
        ZStack(alignment: .topTrailing) {
            ScannerRepresentable(onFound: onFound).ignoresSafeArea()
            VStack {
                Spacer()
                Text("Point the camera at the code on your PC")
                    .font(.system(.subheadline, design: .rounded, weight: .medium))
                    .padding(.horizontal, 16).padding(.vertical, 10)
                    .glassCard(radius: 18)
                    .padding(.bottom, 40)
            }
            .frame(maxWidth: .infinity)
            Button { onCancel() } label: {
                Image(systemName: "xmark.circle.fill").font(.system(size: 30)).symbolRenderingMode(.hierarchical)
            }
            .padding(20)
            .accessibilityLabel("Close scanner")
        }
        .background(Color.black.ignoresSafeArea())
    }
}

private struct ScannerRepresentable: UIViewControllerRepresentable {
    let onFound: (String) -> Void

    func makeUIViewController(context: Context) -> ScannerController {
        let controller = ScannerController()
        controller.onFound = onFound
        return controller
    }

    func updateUIViewController(_ controller: ScannerController, context: Context) {}
}

private final class ScannerController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    var onFound: ((String) -> Void)?
    private let session = AVCaptureSession()
    private var preview: AVCaptureVideoPreviewLayer?
    private var finished = false

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
            DispatchQueue.main.async {
                if granted { self?.configure() } else { self?.showDenied() }
            }
        }
    }

    private func configure() {
        guard let device = AVCaptureDevice.default(for: .video), let input = try? AVCaptureDeviceInput(device: device),
              session.canAddInput(input) else {
            showDenied()
            return
        }
        session.addInput(input)
        let output = AVCaptureMetadataOutput()
        if session.canAddOutput(output) {
            session.addOutput(output)
            output.setMetadataObjectsDelegate(self, queue: .main)
            output.metadataObjectTypes = [.qr]
        }
        let layer = AVCaptureVideoPreviewLayer(session: session)
        layer.videoGravity = .resizeAspectFill
        layer.frame = view.bounds
        view.layer.addSublayer(layer)
        preview = layer
        DispatchQueue.global(qos: .userInitiated).async { [session] in session.startRunning() }
    }

    private func showDenied() {
        let label = UILabel()
        label.text = "Camera access is off. Allow it in Settings → SAINT, or type the pairing code instead."
        label.textColor = .white
        label.numberOfLines = 0
        label.textAlignment = .center
        label.frame = view.bounds.insetBy(dx: 30, dy: 0)
        view.addSubview(label)
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        preview?.frame = view.bounds
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        if session.isRunning { session.stopRunning() }
    }

    func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput objects: [AVMetadataObject], from connection: AVCaptureConnection) {
        guard !finished, let code = objects.compactMap({ $0 as? AVMetadataMachineReadableCodeObject }).first?.stringValue,
              code.hasPrefix("saint://") else { return }
        finished = true
        UINotificationFeedbackGenerator().notificationOccurred(.success)
        session.stopRunning()
        onFound?(code)
    }
}

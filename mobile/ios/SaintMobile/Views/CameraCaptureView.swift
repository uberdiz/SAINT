import SwiftUI
import UIKit
import MessageUI
import AVFoundation

/// "Take a picture": SAINT's own camera. With ``autoShoot`` it counts down 3-2-1 and takes the photo itself
/// (iOS doesn't let an app press the Camera app's shutter), then saves it to Photos.
struct CameraCaptureView: UIViewControllerRepresentable {
    let request: CameraRequest
    let onDone: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onDone: onDone) }

    func makeUIViewController(context: Context) -> UIViewController {
        guard UIImagePickerController.isSourceTypeAvailable(.camera) else {
            let alert = UIAlertController(title: "No camera", message: "This device has no camera SAINT can use.", preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in onDone() })
            return alert
        }
        let picker = UIImagePickerController()
        picker.sourceType = .camera
        picker.delegate = context.coordinator
        if request.mode == .video {
            picker.mediaTypes = ["public.movie"]
            picker.cameraCaptureMode = .video
        } else {
            picker.cameraCaptureMode = .photo
        }
        if request.front && UIImagePickerController.isCameraDeviceAvailable(.front) { picker.cameraDevice = .front }
        if request.autoShoot && request.mode == .photo {
            picker.showsCameraControls = false
            let overlay = CountdownOverlay(frame: UIScreen.main.bounds)
            picker.cameraOverlayView = overlay
            overlay.start(from: 3) { [weak picker] in picker?.takePicture() }
        }
        return picker
    }

    func updateUIViewController(_ controller: UIViewController, context: Context) {}

    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let onDone: () -> Void
        init(onDone: @escaping () -> Void) { self.onDone = onDone }

        func imagePickerController(_ picker: UIImagePickerController, didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            if let image = info[.originalImage] as? UIImage {
                UIImageWriteToSavedPhotosAlbum(image, nil, nil, nil)
                Task { @MainActor in AppModel.shared.cameraSaved("photo") }
            } else if let url = info[.mediaURL] as? URL, UIVideoAtPathIsCompatibleWithSavedPhotosAlbum(url.path) {
                UISaveVideoAtPathToSavedPhotosAlbum(url.path, nil, nil, nil)
                Task { @MainActor in AppModel.shared.cameraSaved("video") }
            }
            onDone()
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) { onDone() }
    }
}

/// 3 · 2 · 1, big and orange, over the camera preview.
final class CountdownOverlay: UIView {
    private let label = UILabel()
    private var timer: Timer?

    override init(frame: CGRect) {
        super.init(frame: frame)
        isUserInteractionEnabled = false
        label.font = .systemFont(ofSize: 120, weight: .bold)
        label.textColor = UIColor(red: 0.996, green: 0.667, blue: 0.204, alpha: 1)
        label.textAlignment = .center
        label.frame = bounds
        label.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        addSubview(label)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func start(from n: Int, then shoot: @escaping () -> Void) {
        var remaining = n
        label.text = "\(remaining)"
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] t in
            remaining -= 1
            if remaining <= 0 {
                t.invalidate()
                self?.label.text = ""
                UIImpactFeedbackGenerator(style: .rigid).impactOccurred()
                shoot()
            } else {
                self?.label.text = "\(remaining)"
            }
        }
    }
}

/// Apple's message sheet, filled in by SAINT: you check it and tap Send.
struct MessageComposeView: UIViewControllerRepresentable {
    let draft: MessageDraft
    let onFinish: (MessageComposeResult) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onFinish: onFinish) }

    func makeUIViewController(context: Context) -> MFMessageComposeViewController {
        let vc = MFMessageComposeViewController()
        vc.recipients = [draft.recipient]
        vc.body = draft.body
        vc.messageComposeDelegate = context.coordinator
        return vc
    }

    func updateUIViewController(_ vc: MFMessageComposeViewController, context: Context) {}

    final class Coordinator: NSObject, MFMessageComposeViewControllerDelegate {
        let onFinish: (MessageComposeResult) -> Void
        init(onFinish: @escaping (MessageComposeResult) -> Void) { self.onFinish = onFinish }
        func messageComposeViewController(_ controller: MFMessageComposeViewController, didFinishWith result: MessageComposeResult) {
            onFinish(result)
        }
    }
}

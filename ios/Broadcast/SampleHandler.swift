import CoreImage
import ReplayKit
import UIKit

/// Runs while the user has started 「画面収録」 for Karry 作業用 (red status-bar indicator).
/// Sends a ~720px-wide JPEG to the monitor every 3 s and a heartbeat every 15 s, like the Android app.
final class SampleHandler: RPBroadcastSampleHandler {
    private static let maxWidth: CGFloat = 720
    private static let interval: TimeInterval = 3
    private static let heartbeatTicks = 5

    private let queue = DispatchQueue(label: "karry.share")
    private let context = CIContext()
    private var timer: DispatchSourceTimer?
    private var lastEncoded = Date.distantPast
    private var lastFrame: Data?
    private var ticks = 0
    private var finished = false

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        guard SharedStore.token != nil else {
            end("先に「Karry 作業用」アプリを開いて「利用開始」で登録してください。")
            return
        }
        SharedStore.clearStop()
        SharedStore.markSharing()
        UIDevice.current.isBatteryMonitoringEnabled = true
        queue.async { self.heartbeat(sharing: true) }
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 1, repeating: Self.interval)
        timer.setEventHandler { [weak self] in self?.tick() }
        timer.resume()
        self.timer = timer
    }

    override func processSampleBuffer(_ sampleBuffer: CMSampleBuffer, with type: RPSampleBufferType) {
        guard type == .video, Date().timeIntervalSince(lastEncoded) >= Self.interval - 0.5,
              let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        lastEncoded = Date()
        var image = CIImage(cvPixelBuffer: pixels)
        if let raw = CMGetAttachment(sampleBuffer, key: RPVideoSampleOrientationKey as CFString, attachmentModeOut: nil) as? NSNumber,
           let orientation = CGImagePropertyOrientation(rawValue: raw.uint32Value) {
            image = image.oriented(orientation)
        }
        let scale = min(1, Self.maxWidth / max(image.extent.width, 1))
        image = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        let quality = [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.6]
        guard let jpeg = context.jpegRepresentation(of: image, colorSpace: CGColorSpaceCreateDeviceRGB(), options: quality) else { return }
        queue.async { self.lastFrame = jpeg }
    }

    override func broadcastFinished() {
        queue.sync {
            guard !finished else { return }
            finished = true
            timer?.cancel()
            SharedStore.clearSharing()
            heartbeat(sharing: false, wait: true)
        }
    }

    private func tick() {
        guard !finished else { return }
        if SharedStore.stopRequested {
            end("作業用アプリから画面の共有を止めました。")
            return
        }
        SharedStore.markSharing()
        if let frame = lastFrame { send("PUT", "/api/autonomy/device/frame", type: "image/jpeg", body: frame) }
        ticks += 1
        if ticks % Self.heartbeatTicks == 0 { heartbeat(sharing: true) }
    }

    /// Ends the broadcast with a message iOS shows to the user.
    private func end(_ message: String) {
        queue.async {
            guard !self.finished else { return }
            self.finished = true
            self.timer?.cancel()
            SharedStore.clearSharing()
            SharedStore.clearStop()
            self.heartbeat(sharing: false, wait: true)
            let error = NSError(domain: "Karry", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
            DispatchQueue.main.async { self.finishBroadcastWithError(error) }
        }
    }

    private func heartbeat(sharing: Bool, wait: Bool = false) {
        var status: [String: Any] = ["sharing": sharing, "visible": true, "app": "作業用アプリ",
                                     "platform": "iOS \(UIDevice.current.systemVersion)"]
        let level = UIDevice.current.batteryLevel
        if level >= 0 {
            status["battery"] = Int((level * 100).rounded())
            status["charging"] = UIDevice.current.batteryState == .charging || UIDevice.current.batteryState == .full
        }
        guard let body = try? JSONSerialization.data(withJSONObject: status) else { return }
        send("POST", "/api/autonomy/device/heartbeat", type: "application/json", body: body, wait: wait)
    }

    private func send(_ method: String, _ path: String, type: String, body: Data, wait: Bool = false) {
        guard let token = SharedStore.token, let url = URL(string: path, relativeTo: SharedStore.serverURL) else { return }
        var request = URLRequest(url: url, timeoutInterval: 8)
        request.httpMethod = method
        request.httpBody = body
        request.setValue(type, forHTTPHeaderField: "Content-Type")
        request.setValue(token, forHTTPHeaderField: "X-Device-Token")
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            defer { done.signal() }
            // 401: the monitor revoked this device, so sharing must stop.
            if (response as? HTTPURLResponse)?.statusCode == 401 {
                SharedStore.token = nil
                self?.end("この端末の登録が解除されたため、画面の共有を止めました。")
            }
        }.resume()
        if wait { _ = done.wait(timeout: .now() + 3) }
    }
}

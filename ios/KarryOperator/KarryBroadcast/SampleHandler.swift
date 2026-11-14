import CoreImage
import ReplayKit
import UIKit

final class SampleHandler: RPBroadcastSampleHandler {
    private static let maxWidth: CGFloat = 720
    private static let sendInterval: TimeInterval = 3

    private let queue = DispatchQueue(label: "karry.mobile.screen-share")
    private let context = CIContext()
    private var lastSent = Date.distantPast
    private var stopped = false

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        guard ScreenShareStore.token != nil,
              ScreenShareStore.serverURL != nil,
              ScreenShareStore.deviceID != nil else {
            finish("先にKarry-Agentを開いて端末登録を完了してください。")
            return
        }
        ScreenShareStore.markSharing()
    }

    override func processSampleBuffer(_ sampleBuffer: CMSampleBuffer, with type: RPSampleBufferType) {
        guard type == .video,
              !stopped,
              Date().timeIntervalSince(lastSent) >= Self.sendInterval,
              let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else {
            return
        }
        lastSent = Date()
        var image = CIImage(cvPixelBuffer: pixels)
        if let raw = CMGetAttachment(
            sampleBuffer,
            key: RPVideoSampleOrientationKey as CFString,
            attachmentModeOut: nil
        ) as? NSNumber,
           let orientation = CGImagePropertyOrientation(rawValue: raw.uint32Value) {
            image = image.oriented(orientation)
        }
        let scale = min(1, Self.maxWidth / max(image.extent.width, 1))
        image = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        let options = [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.6]
        guard let jpeg = context.jpegRepresentation(
            of: image,
            colorSpace: CGColorSpaceCreateDeviceRGB(),
            options: options
        ) else {
            return
        }
        queue.async { [weak self] in self?.send(jpeg) }
    }

    override func broadcastFinished() {
        stopped = true
        ScreenShareStore.clearSharing()
    }

    private func send(_ jpeg: Data) {
        guard !stopped,
              let token = ScreenShareStore.token,
              let server = ScreenShareStore.serverURL,
              let deviceID = ScreenShareStore.deviceID else {
            return
        }
        ScreenShareStore.markSharing()
        let url = server
            .appendingPathComponent("api")
            .appendingPathComponent("mobile-agent")
            .appendingPathComponent("devices")
            .appendingPathComponent(deviceID)
            .appendingPathComponent("frame")
        var request = URLRequest(url: url, timeoutInterval: 8)
        request.httpMethod = "PUT"
        request.httpBody = jpeg
        request.setValue("image/jpeg", forHTTPHeaderField: "Content-Type")
        request.setValue(token, forHTTPHeaderField: "X-Device-Token")
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            if (response as? HTTPURLResponse)?.statusCode == 401 {
                self?.finish("端末登録が無効になったため、画面共有を終了しました。")
            }
        }.resume()
    }

    private func finish(_ message: String) {
        queue.async {
            guard !self.stopped else { return }
            self.stopped = true
            ScreenShareStore.clearSharing()
            let error = NSError(
                domain: "KarryAgentScreenShare",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: message]
            )
            DispatchQueue.main.async { self.finishBroadcastWithError(error) }
        }
    }
}

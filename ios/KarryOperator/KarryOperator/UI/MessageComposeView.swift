import MessageUI
import SwiftUI

/// System message composer. The user decides whether to send; the app never sends on its own.
struct MessageComposeView: UIViewControllerRepresentable {
    let recipients: [String]
    let body: String
    let onFinish: (PromptResult) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onFinish: onFinish) }

    func makeUIViewController(context: Context) -> MFMessageComposeViewController {
        let controller = MFMessageComposeViewController()
        controller.recipients = recipients
        controller.body = body
        controller.messageComposeDelegate = context.coordinator
        return controller
    }

    func updateUIViewController(_ controller: MFMessageComposeViewController, context: Context) {}

    final class Coordinator: NSObject, MFMessageComposeViewControllerDelegate {
        let onFinish: (PromptResult) -> Void

        init(onFinish: @escaping (PromptResult) -> Void) {
            self.onFinish = onFinish
        }

        func messageComposeViewController(_ controller: MFMessageComposeViewController, didFinishWith result: MessageComposeResult) {
            switch result {
            case .sent: onFinish(.messageSent)
            case .cancelled: onFinish(.cancelled)
            default: onFinish(.failed)
            }
        }
    }
}

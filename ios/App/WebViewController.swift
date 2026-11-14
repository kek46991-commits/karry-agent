import ReplayKit
import UIKit
import WebKit

/// Opens the server's /monitor or /companion page. The 作業用 build adds `window.KarryNative`
/// (same API as the Android app) backed by a ReplayKit broadcast extension for whole-screen sharing.
final class WebViewController: UIViewController, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private let role = Bundle.main.object(forInfoDictionaryKey: "KarryRole") as? String ?? "monitor"
    private let startPath = Bundle.main.object(forInfoDictionaryKey: "KarryStartPath") as? String ?? "/monitor"
    private let server = SharedStore.serverURL
    private var isWorker: Bool { role == "worker" }
    private var web: WKWebView!
    private var picker: RPSystemBroadcastPickerView?
    private var stateTimer: Timer?
    private var shownSharing = false

    private static let bridge = """
    (() => {
      let sharing = false;
      window.KarryNative = {
        isSharing: () => sharing,
        startScreenShare: () => window.webkit.messageHandlers.karry.postMessage('start'),
        stopScreenShare: () => window.webkit.messageHandlers.karry.postMessage('stop'),
        _set: (on) => { sharing = !!on; if (window.karryNativeChanged) window.karryNativeChanged(); }
      };
    })();
    """

    override func loadView() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.applicationNameForUserAgent = "Mobile KarryDevice/\(role)"
        if isWorker {
            config.userContentController.addUserScript(
                WKUserScript(source: Self.bridge, injectionTime: .atDocumentStart, forMainFrameOnly: true))
            config.userContentController.add(WeakMessageHandler(self), name: "karry")
        }
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = true
        view = web
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        load()
        guard isWorker else { return }
        let picker = RPSystemBroadcastPickerView(frame: CGRect(x: 0, y: 0, width: 44, height: 44))
        picker.preferredExtension = (Bundle.main.bundleIdentifier ?? "") + ".broadcast"
        picker.showsMicrophoneButton = false
        picker.alpha = 0.011
        view.addSubview(picker)
        self.picker = picker
        stateTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.pushSharingState(force: false) }
        NotificationCenter.default.addObserver(forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in
            self?.pushSharingState(force: true)
        }
    }

    private func load() {
        web.load(URLRequest(url: URL(string: startPath, relativeTo: server)!))
    }

    private func sameOrigin(_ url: URL) -> Bool {
        url.scheme == server.scheme && url.host == server.host && url.port == server.port
    }

    // MARK: Native bridge (作業用 only)

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, let url = message.frameInfo.request.url, sameOrigin(url),
              let action = message.body as? String else { return }
        switch action {
        case "start": startBroadcast()
        case "stop":
            SharedStore.requestStop()
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in self?.pushSharingState(force: true) }
        default: break
        }
    }

    private func startBroadcast() {
        syncToken { [weak self] found in
            guard let self else { return }
            guard found else {
                self.alert("先に「利用開始」で、この端末を登録してください。")
                return
            }
            SharedStore.clearStop()
            // Opens the system "画面収録" sheet; the user must still tap 「ブロードキャストを開始」.
            for case let button as UIButton in self.picker?.subviews ?? [] {
                button.sendActions(for: .allEvents)
            }
        }
    }

    /// Copies the HttpOnly `karry_device` cookie into the shared keychain so the extension can authenticate.
    private func syncToken(_ done: ((Bool) -> Void)? = nil) {
        guard isWorker else { done?(false); return }
        web.configuration.websiteDataStore.httpCookieStore.getAllCookies { [server] cookies in
            let host = server.host ?? ""
            let cookie = cookies.first { $0.name == "karry_device" && ($0.domain == host || $0.domain == "." + host) }
            SharedStore.token = cookie?.value
            done?(cookie != nil)
        }
    }

    private func pushSharingState(force: Bool) {
        let sharing = SharedStore.isSharing
        guard force || sharing != shownSharing else { return }
        shownSharing = sharing
        web.evaluateJavaScript("window.KarryNative && window.KarryNative._set(\(sharing))")
    }

    // MARK: Navigation

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if url.scheme == "about" || sameOrigin(url) { return decisionHandler(.allow) }
        if action.targetFrame?.isMainFrame ?? true { UIApplication.shared.open(url) }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if response.isForMainFrame, let http = response.response as? HTTPURLResponse, http.statusCode >= 400 {
            decisionHandler(.cancel)
            showProblem(http.statusCode)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if let url = webView.url, sameOrigin(url) { syncToken() }
        pushSharingState(force: true)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let error = error as NSError
        if error.code == NSURLErrorCancelled || (error.domain == "WebKitErrorDomain" && error.code == 102) { return }
        showProblem(0)
    }

    /// Replaces raw server errors (e.g. {"detail":"Not Found"}) with a plain explanation and a retry button.
    private func showProblem(_ status: Int) {
        let message: String
        switch status {
        case 404: message = "サーバーで見守り機能がまだ有効になっていません。<br>管理者に「KARRY_EXPERIMENTAL_FEATURES=1」の設定を頼んでください。"
        case 0: message = "インターネットにつながっていないか、サーバーに届きませんでした。<br>電波を確認してください。"
        default: message = "サーバーが起動中か、混み合っています（\(status)）。<br>1分ほど待ってからもう一度お試しください。"
        }
        let name = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "Karry"
        let retry = URL(string: startPath, relativeTo: server)!.absoluteString
        let html = """
        <!doctype html><html lang=ja><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
        <body style='font-family:-apple-system,sans-serif;padding:32px 20px;line-height:1.7;color:#1f2933'>
        <h2 style='margin-top:0'>\(name)</h2><p style='font-size:18px'>\(message)</p>
        <p><a href='\(retry)' style='display:block;text-align:center;padding:16px;background:#1f6feb;color:#fff;border-radius:12px;font-size:20px;text-decoration:none'>もう一度試す</a></p>
        <p style='color:#616e7c;font-size:14px'>つなぎ先: \(server.absoluteString)</p></body></html>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    // MARK: JavaScript dialogs (confirm() is used before revoking a device or stopping a task)

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
        present(alert, animated: true)
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "キャンセル", style: .cancel) { _ in completionHandler(false) })
        alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
        present(alert, animated: true)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url {
            if sameOrigin(url) { webView.load(action.request) } else { UIApplication.shared.open(url) }
        }
        return nil
    }

    private func alert(_ message: String) {
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default))
        present(alert, animated: true)
    }
}

/// Avoids the retain cycle WKUserContentController creates with its message handlers.
private final class WeakMessageHandler: NSObject, WKScriptMessageHandler {
    private weak var target: WKScriptMessageHandler?
    init(_ target: WKScriptMessageHandler) { self.target = target }
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        target?.userContentController(controller, didReceive: message)
    }
}

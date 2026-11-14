import Foundation

@MainActor
enum OperatorRuntime {
    /// Lets the App Intent reach the running app's model.
    static var model: OperatorViewModel?
}

@MainActor
final class OperatorViewModel: ObservableObject {
    enum Connection: Equatable {
        case notEnrolled
        case ready
        case checking
        case connected(Date)
        case reenrollRequired
        case error(String)

        var label: String {
            switch self {
            case .notEnrolled: return "未登録"
            case .ready: return "登録済み（未確認）"
            case .checking: return "確認中…"
            case .connected: return "接続OK"
            case .reenrollRequired: return "再登録が必要"
            case .error: return "接続エラー"
            }
        }
    }

    struct ClaimedAction: Equatable {
        let id: String
        let name: String
        let detail: String?
        var state: String
    }

    struct LocalResult: Equatable {
        let success: Bool
        let message: String
        let errorCode: String
        var reported: Bool
        let finishedAt: Date
    }

    static let pollInterval: UInt64 = 10_000_000_000

    @Published private(set) var connection: Connection
    @Published private(set) var claimed: ClaimedAction?
    @Published private(set) var lastResult: LocalResult?
    @Published private(set) var isBusy = false
    @Published private(set) var hasToken: Bool
    @Published var baseURL: String {
        didSet { settings.baseURL = baseURL }
    }
    @Published var deviceID: String {
        didSet { settings.deviceID = deviceID }
    }
    @Published var autoCheck: Bool {
        didSet {
            settings.autoCheck = autoCheck
            autoCheck ? startPolling() : stopPolling()
        }
    }
    @Published private(set) var setupLines: [String] = []
    @Published private(set) var setupPrompt: SetupPrompt?
    private var skippedSteps: Set<SetupStep> = []

    private let settings: OperatorSettings
    private let tokenStore: TokenStoring
    private let executor: ActionPerforming
    private let permissions: PermissionServicing
    private let makeClient: (URL) throws -> BackendServing
    private var pendingReport: (eventID: String, report: ActionReport)?
    private var pollTask: Task<Void, Never>?

    init(
        settings: OperatorSettings,
        tokenStore: TokenStoring,
        executor: ActionPerforming,
        permissions: PermissionServicing = UnavailablePermissionService(),
        makeClient: @escaping (URL) throws -> BackendServing = { try BackendClient(baseURL: $0) }
    ) {
        self.settings = settings
        self.tokenStore = tokenStore
        self.executor = executor
        self.permissions = permissions
        self.makeClient = makeClient
        baseURL = settings.baseURL
        deviceID = settings.deviceID
        autoCheck = settings.autoCheck
        let hasToken = tokenStore.readToken() != nil
        self.hasToken = hasToken
        connection = hasToken ? .ready : .notEnrolled
        refreshSetup()
    }

    var setupSnapshot: SetupSnapshot {
        SetupSnapshot(
            serverURLValid: URL(string: baseURL.trimmingCharacters(in: .whitespacesAndNewlines))?.host != nil,
            deviceIDValid: BackendClient.isValidDeviceID(deviceID),
            enrolled: hasToken,
            calendar: permissions.calendarState(),
            reminders: permissions.remindersState(),
            location: permissions.locationState(),
            messagesCompose: permissions.canComposeMessages,
            autoCheck: autoCheck
        )
    }

    /// Rebuilds the raw yes/no setup log and the next Yes/No question.
    func refreshSetup() {
        let snapshot = setupSnapshot
        setupLines = SetupStatus.lines(snapshot, skipped: skippedSteps)
        setupPrompt = SetupStatus.nextPrompt(snapshot, skipped: skippedSteps)
    }

    /// Yes routes straight to the system prompt (or Settings); No skips that step.
    func answerSetup(yes: Bool) async {
        guard let prompt = setupPrompt else { return }
        if !yes {
            skippedSteps.insert(prompt.step)
        } else if prompt.opensSettings {
            skippedSteps.insert(prompt.step)
            await permissions.openSettings()
        } else {
            await permissions.request(prompt.step)
        }
        refreshSetup()
    }

    private func client() throws -> BackendServing {
        let trimmed = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed) else { throw BackendError.invalidBaseURL }
        return try makeClient(url)
    }

    /// The owner token is used for this one request and never persisted.
    func enroll(ownerToken: String) async {
        let owner = ownerToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !owner.isEmpty else {
            connection = .error("オーナートークンを入力してください")
            return
        }
        guard BackendClient.isValidDeviceID(deviceID) else {
            connection = .error(BackendError.invalidIdentifier.localizedMessage)
            return
        }
        isBusy = true
        defer { isBusy = false }
        do {
            let token = try await client().enroll(deviceID: deviceID, ownerToken: owner)
            try tokenStore.saveToken(token)
            hasToken = true
            pendingReport = nil
            connection = .ready
        } catch let error as BackendError {
            connection = .error(error.localizedMessage)
        } catch {
            connection = .error("端末トークンをキーチェーンに保存できませんでした")
        }
        refreshSetup()
    }

    /// For a device token issued elsewhere (e.g. by the owner with curl).
    func saveDeviceToken(_ token: String) {
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.count <= 256 else {
            connection = .error("端末トークンが不正です")
            return
        }
        do {
            try tokenStore.saveToken(trimmed)
            hasToken = true
            connection = .ready
        } catch {
            connection = .error("端末トークンをキーチェーンに保存できませんでした")
        }
        refreshSetup()
    }

    func unenroll() {
        stopPolling()
        try? tokenStore.deleteToken()
        hasToken = false
        pendingReport = nil
        claimed = nil
        connection = .notEnrolled
        refreshSetup()
    }

    /// Claims at most one action, executes it locally, and reports a bounded result.
    @discardableResult
    func checkNow() async -> String {
        guard !isBusy else { return "処理中です" }
        guard let token = tokenStore.readToken() else {
            connection = .notEnrolled
            return "端末が未登録です"
        }
        let backend: BackendServing
        do {
            backend = try client()
        } catch {
            connection = .error(BackendError.invalidBaseURL.localizedMessage)
            return BackendError.invalidBaseURL.localizedMessage
        }
        isBusy = true
        defer { isBusy = false }
        connection = .checking

        if let pending = pendingReport {
            guard await deliver(pending.report, eventID: pending.eventID, backend: backend, token: token) else {
                return "前回の結果を再送できませんでした"
            }
        }

        let action: MobileAction?
        do {
            action = try await backend.claim(deviceID: deviceID, deviceToken: token)
        } catch let error as BackendError {
            connection = error == .unauthorized ? .reenrollRequired : .error(error.localizedMessage)
            return error.localizedMessage
        } catch {
            connection = .error(BackendError.invalidResponse.localizedMessage)
            return BackendError.invalidResponse.localizedMessage
        }
        connection = .connected(Date())
        guard let action else { return "実行待ちのアクションはありません" }

        claimed = ClaimedAction(id: action.id, name: action.displayName, detail: Self.visibleDetail(for: action), state: "実行中")
        let report: ActionReport
        do {
            let plan = try ActionPlanner.plan(for: action)
            let summary = try await executor.perform(plan)
            report = .success(deviceID: deviceID, summary: summary)
        } catch let failure as ActionFailure {
            report = .failure(deviceID: deviceID, failure: failure)
        } catch {
            report = .failure(deviceID: deviceID, failure: ActionFailure(.executionFailed, "実行に失敗しました"))
        }
        claimed?.state = report.success ? "完了" : "失敗"
        lastResult = LocalResult(success: report.success, message: report.output, errorCode: report.errorCode, reported: false, finishedAt: Date())
        await deliver(report, eventID: action.id, backend: backend, token: token)
        return report.output
    }

    @discardableResult
    private func deliver(_ report: ActionReport, eventID: String, backend: BackendServing, token: String) async -> Bool {
        do {
            try await backend.report(eventID: eventID, deviceToken: token, report: report)
            pendingReport = nil
            if claimed?.id == eventID { lastResult?.reported = true }
            return true
        } catch BackendError.unauthorized {
            pendingReport = (eventID, report)
            connection = .reenrollRequired
        } catch {
            pendingReport = (eventID, report)
            connection = .error("結果を送信できませんでした。次回の確認で再送します")
        }
        return false
    }

    /// Only the location purpose is shown, so the user knows why the prompt appears.
    private static func visibleDetail(for action: MobileAction) -> String? {
        guard action.actionName == .requestLocation, let purpose = action.payload["purpose"]?.stringValue else { return nil }
        return "目的: " + purpose.prefix(120)
    }

    func sceneBecameActive(_ active: Bool) {
        if active { refreshSetup() }
        if active && autoCheck { startPolling() } else { stopPolling() }
    }

    private func startPolling() {
        guard pollTask == nil, hasToken else { return }
        pollTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                guard let self, self.autoCheck else { return }
                await self.checkNow()
                try? await Task.sleep(nanoseconds: Self.pollInterval)
            }
        }
    }

    private func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }
}

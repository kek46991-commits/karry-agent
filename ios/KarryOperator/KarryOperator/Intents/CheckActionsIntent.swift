import AppIntents

/// Fixed App Intent: claim and run at most one approved action in the foreground app.
struct CheckKarryActionsIntent: AppIntent {
    static let title: LocalizedStringResource = "Karryのアクションを確認"
    static let description = IntentDescription("承認済みのアクションを1件受け取り、iPhoneで実行して結果を報告します。")
    static let openAppWhenRun = true

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard let model = OperatorRuntime.model else {
            return .result(dialog: "Karry操作アプリを開いてから再実行してください")
        }
        let message = await model.checkNow()
        return .result(dialog: IntentDialog(stringLiteral: message))
    }
}

struct KarryOperatorShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: CheckKarryActionsIntent(),
            phrases: ["\(.applicationName)でアクションを確認"],
            shortTitle: "アクション確認",
            systemImageName: "iphone.radiowaves.left.and.right"
        )
    }
}

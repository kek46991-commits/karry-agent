import Foundation
import UniformTypeIdentifiers

/// Interactive system UI that only the user can complete.
enum InteractivePrompt: Equatable {
    case chooseFile(contentTypes: [UTType])
    case choosePhoto(limit: Int)
    case draftMessage(recipients: [String], body: String)
}

enum PromptResult: Equatable {
    /// Number of selected items and their file extensions (never names or contents).
    case selected(count: Int, kinds: [String])
    case messageSent
    case messageSaved
    case cancelled
    case failed
}

@MainActor
protocol URLOpening {
    func open(_ url: URL) async -> Bool
}

@MainActor
protocol EventWriting {
    func createEvent(_ draft: CalendarEventDraft) async throws
    func createReminder(_ draft: ReminderDraft) async throws
}

@MainActor
protocol LocationProviding {
    /// Returns only the horizontal accuracy in meters; coordinates stay on device.
    func requestOneLocation() async throws -> Double
}

@MainActor
protocol PromptPresenting {
    var canComposeMessages: Bool { get }
    func present(_ prompt: InteractivePrompt) async -> PromptResult
}

@MainActor
protocol ActionPerforming {
    func perform(_ plan: ActionPlan) async throws -> String
}

/// Maps each validated plan to one fixed iOS API. Returns a bounded, non-sensitive summary.
@MainActor
struct SystemActionExecutor: ActionPerforming {
    let urlOpener: URLOpening
    let events: EventWriting
    let location: LocationProviding
    let prompts: PromptPresenting

    func perform(_ plan: ActionPlan) async throws -> String {
        switch plan {
        case .openApp(let bundleID, let url):
            guard let url else { return "\(AppIdentity.displayName) は既に表示中です" }
            guard await urlOpener.open(url) else {
                throw ActionFailure(.appUnavailable, "アプリを開けませんでした（\(bundleID)）")
            }
            return "アプリを開きました（\(bundleID)）"

        case .openURL(let url):
            guard await urlOpener.open(url) else {
                throw ActionFailure(.appUnavailable, "URLを開けませんでした")
            }
            return "URLを開きました（\(url.host ?? "")）"

        case .createCalendarEvent(let draft):
            try await events.createEvent(draft)
            return "カレンダー予定を作成しました"

        case .createReminder(let draft):
            try await events.createReminder(draft)
            return "リマインダーを作成しました"

        case .requestLocation:
            let accuracy = try await location.requestOneLocation()
            return "現在地を取得しました（精度 約\(Int(accuracy.rounded()))m・座標は端末内のみ）"

        case .chooseFile(let types):
            return try summarize(await prompts.present(.chooseFile(contentTypes: types)), noun: "ファイル")

        case .choosePhoto(let limit):
            return try summarize(await prompts.present(.choosePhoto(limit: limit)), noun: "写真")

        case .draftMessage(let recipients, let body):
            guard prompts.canComposeMessages else {
                throw ActionFailure(.intentUnavailable, "この端末ではメッセージを作成できません")
            }
            switch await prompts.present(.draftMessage(recipients: recipients, body: body)) {
            case .messageSent: return "ユーザーがメッセージを送信しました"
            case .messageSaved: return "メッセージ下書きを表示しました"
            case .cancelled: throw ActionFailure(.userCancelled, "ユーザーが下書きを閉じました")
            default: throw ActionFailure(.executionFailed, "メッセージ作成に失敗しました")
            }
        }
    }

    private func summarize(_ result: PromptResult, noun: String) throws -> String {
        switch result {
        case .selected(let count, let kinds) where count > 0:
            let unique = Array(Set(kinds)).sorted().prefix(5)
            let suffix = unique.isEmpty ? "" : "（種類: \(unique.joined(separator: ", "))）"
            return "\(noun)を\(count)件選択しました\(suffix)"
        case .selected, .cancelled:
            throw ActionFailure(.userCancelled, "ユーザーが\(noun)を選択しませんでした")
        default:
            throw ActionFailure(.executionFailed, "\(noun)の選択に失敗しました")
        }
    }
}

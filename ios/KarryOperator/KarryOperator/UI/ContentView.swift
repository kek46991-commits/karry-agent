import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @ObservedObject var model: OperatorViewModel
    @ObservedObject var prompts: PromptCoordinator
    @State private var ownerToken = ""
    @State private var manualDeviceToken = ""
    @State private var photoSelection: [PhotosPickerItem] = []

    var body: some View {
        NavigationStack {
            Form {
                setupSection
                connectionSection
                if model.hasToken {
                    operationSection
                } else {
                    enrollmentSection
                }
                claimedSection
                resultSection
            }
            .navigationTitle(AppIdentity.displayName)
            .disabled(model.isBusy && prompts.prompt == nil)
        }
        .fileImporter(isPresented: fileBinding, allowedContentTypes: fileTypes, allowsMultipleSelection: false) { result in
            switch result {
            case .success(let urls):
                prompts.finish(.selected(count: urls.count, kinds: urls.map { $0.pathExtension.lowercased() }.filter { !$0.isEmpty }))
            case .failure:
                prompts.finish(.failed)
            }
        }
        .photosPicker(isPresented: photoBinding, selection: $photoSelection, maxSelectionCount: photoLimit, matching: .images)
        .sheet(isPresented: messageBinding) {
            if case .draftMessage(let recipients, let body) = prompts.prompt {
                MessageComposeView(recipients: recipients, body: body) { prompts.finish($0) }
                    .ignoresSafeArea()
            }
        }
    }

    private var setupSection: some View {
        Section("setup") {
            Text(model.setupLines.joined(separator: "\n"))
                .font(.system(.footnote, design: .monospaced))
                .textSelection(.enabled)
                .accessibilityIdentifier("setupLog")
            if let prompt = model.setupPrompt {
                Text(prompt.text)
                    .font(.system(.footnote, design: .monospaced))
                    .accessibilityIdentifier("setupPrompt")
                HStack {
                    Button("Yes") { Task { await model.answerSetup(yes: true) } }
                        .buttonStyle(.borderedProminent)
                        .accessibilityIdentifier("setupYes")
                    Button("No") { Task { await model.answerSetup(yes: false) } }
                        .buttonStyle(.bordered)
                        .accessibilityIdentifier("setupNo")
                }
            }
        }
        .onAppear { model.refreshSetup() }
    }

    private var connectionSection: some View {
        Section("接続") {
            HStack {
                Text("状態")
                Spacer()
                Label(model.connection.label, systemImage: connectionIcon)
                    .foregroundStyle(connectionColor)
                    .accessibilityIdentifier("connectionStatus")
            }
            if case .connected(let date) = model.connection {
                LabeledContent("最終確認", value: date.formatted(date: .omitted, time: .standard))
            }
            if case .error(let message) = model.connection {
                Text(message).font(.footnote).foregroundStyle(.red)
            }
            TextField("サーバーURL", text: $model.baseURL)
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .disabled(model.hasToken)
                .accessibilityIdentifier("baseURL")
            TextField("端末ID", text: $model.deviceID)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .disabled(model.hasToken)
                .accessibilityIdentifier("deviceID")
        }
    }

    private var enrollmentSection: some View {
        Section {
            SecureField("オーナートークン（KARRY_API_TOKEN）", text: $ownerToken)
                .textInputAutocapitalization(.never)
                .accessibilityIdentifier("ownerToken")
            Button("この端末を登録") {
                let token = ownerToken
                ownerToken = ""
                Task { await model.enroll(ownerToken: token) }
            }
            .disabled(ownerToken.isEmpty)
            .accessibilityIdentifier("enroll")
            DisclosureGroup("発行済みの端末トークンを入力") {
                SecureField("X-Device-Token", text: $manualDeviceToken)
                    .textInputAutocapitalization(.never)
                Button("キーチェーンに保存") {
                    model.saveDeviceToken(manualDeviceToken)
                    manualDeviceToken = ""
                }
                .disabled(manualDeviceToken.isEmpty)
            }
        } header: {
            Text("端末登録")
        } footer: {
            Text("オーナートークンは登録リクエストにだけ使い、保存しません。キーチェーンには端末専用トークンのみ保存します。")
        }
    }

    private var operationSection: some View {
        Section("操作") {
            Button {
                Task { await model.checkNow() }
            } label: {
                HStack {
                    Text("今すぐアクションを確認")
                    if model.isBusy {
                        Spacer()
                        ProgressView()
                    }
                }
            }
            .accessibilityIdentifier("checkNow")
            Toggle("自動確認（アプリ表示中・10秒ごと）", isOn: $model.autoCheck)
            Button("登録を解除", role: .destructive) { model.unenroll() }
        }
    }

    private var claimedSection: some View {
        Section("受け取ったアクション") {
            if let claimed = model.claimed {
                LabeledContent("内容", value: claimed.name)
                LabeledContent("状態", value: claimed.state)
                if let detail = claimed.detail {
                    Text(detail).font(.footnote)
                }
                LabeledContent("ID", value: String(claimed.id.prefix(8)))
                    .font(.footnote)
            } else {
                Text("まだありません").foregroundStyle(.secondary)
            }
        }
        .accessibilityIdentifier("claimedSection")
    }

    private var resultSection: some View {
        Section("この端末での実行結果") {
            if let result = model.lastResult {
                Label(result.success ? "成功" : "失敗", systemImage: result.success ? "checkmark.circle.fill" : "xmark.octagon.fill")
                    .foregroundStyle(result.success ? .green : .red)
                Text(result.message)
                if !result.errorCode.isEmpty {
                    LabeledContent("エラーコード", value: result.errorCode).font(.footnote)
                }
                LabeledContent("サーバーへの報告", value: result.reported ? "送信済み" : "未送信（次回再送）")
                    .font(.footnote)
            } else {
                Text("まだありません").foregroundStyle(.secondary)
            }
        }
        .accessibilityIdentifier("resultSection")
    }

    private var connectionIcon: String {
        switch model.connection {
        case .connected: return "checkmark.circle.fill"
        case .checking: return "arrow.triangle.2.circlepath"
        case .error, .reenrollRequired: return "exclamationmark.triangle.fill"
        case .notEnrolled, .ready: return "circle"
        }
    }

    private var connectionColor: Color {
        switch model.connection {
        case .connected: return .green
        case .error, .reenrollRequired: return .red
        default: return .secondary
        }
    }

    private var fileTypes: [UTType] {
        if case .chooseFile(let types) = prompts.prompt { return types }
        return [.item]
    }

    private var photoLimit: Int {
        if case .choosePhoto(let limit) = prompts.prompt { return limit }
        return 1
    }

    private var fileBinding: Binding<Bool> {
        Binding(
            get: { if case .chooseFile = prompts.prompt { return true } else { return false } },
            set: { if !$0 { prompts.dismissed() } }
        )
    }

    private var photoBinding: Binding<Bool> {
        Binding(
            get: { if case .choosePhoto = prompts.prompt { return true } else { return false } },
            set: { presented in
                guard !presented else { return }
                Task { @MainActor in
                    try? await Task.sleep(nanoseconds: 300_000_000)
                    let kinds = photoSelection.compactMap { $0.supportedContentTypes.first?.preferredFilenameExtension }
                    prompts.finish(photoSelection.isEmpty ? .cancelled : .selected(count: photoSelection.count, kinds: kinds))
                    photoSelection = []
                }
            }
        )
    }

    private var messageBinding: Binding<Bool> {
        Binding(
            get: { if case .draftMessage = prompts.prompt { return true } else { return false } },
            set: { if !$0 { prompts.dismissed() } }
        )
    }
}

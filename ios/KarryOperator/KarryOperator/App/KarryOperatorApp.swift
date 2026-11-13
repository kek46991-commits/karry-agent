import SwiftUI

@main
struct KarryOperatorApp: App {
    @StateObject private var prompts: PromptCoordinator
    @StateObject private var model: OperatorViewModel
    @Environment(\.scenePhase) private var scenePhase

    init() {
        let prompts = PromptCoordinator()
        let executor = SystemActionExecutor(
            urlOpener: SystemURLOpener(),
            events: EventKitWriter(),
            location: OneShotLocationProvider(),
            prompts: prompts
        )
        let model = OperatorViewModel(settings: OperatorSettings(), tokenStore: KeychainTokenStore(), executor: executor)
        _prompts = StateObject(wrappedValue: prompts)
        _model = StateObject(wrappedValue: model)
        OperatorRuntime.model = model
    }

    var body: some Scene {
        WindowGroup {
            ContentView(model: model, prompts: prompts)
        }
        .onChange(of: scenePhase) { _, phase in
            model.sceneBecameActive(phase == .active)
        }
    }
}

import SwiftUI

@main
@MainActor
struct KarryOperatorApp: App {
    @StateObject private var prompts: PromptCoordinator
    @StateObject private var model: OperatorViewModel
    @StateObject private var location: BackgroundLocationService
    @Environment(\.scenePhase) private var scenePhase

    init() {
        let prompts = PromptCoordinator()
        let executor = SystemActionExecutor(
            urlOpener: SystemURLOpener(),
            events: EventKitWriter(),
            location: OneShotLocationProvider(),
            prompts: prompts
        )
        let settings = OperatorSettings()
        let tokenStore = KeychainTokenStore()
        let model = OperatorViewModel(
            settings: settings,
            tokenStore: tokenStore,
            executor: executor,
            permissions: SystemPermissionService()
        )
        let location = BackgroundLocationService(settings: settings, tokenStore: tokenStore)
        _prompts = StateObject(wrappedValue: prompts)
        _model = StateObject(wrappedValue: model)
        _location = StateObject(wrappedValue: location)
        OperatorRuntime.model = model
    }

    var body: some Scene {
        WindowGroup {
            ContentView(model: model, prompts: prompts, location: location)
        }
        .onChange(of: scenePhase) { _, phase in
            model.sceneBecameActive(phase == .active)
            if phase == .active { location.refresh() }
        }
    }
}

import SwiftUI

@main
struct SaintApp: App {
    @StateObject private var model = AppModel.shared
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .environmentObject(model.settings)
                .environmentObject(model.spotify)
                .environmentObject(model.voice)
                .environmentObject(model.speaker)
                .task { await model.start() }
                .onOpenURL { model.handleOpen($0) }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { model.appBecameActive() }
        }
    }
}

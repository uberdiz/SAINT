import SwiftUI

/// Five tabs, like the Figma design: Talk, Music, Activity, Devices, Settings. Voice mode (the full-screen
/// Listening view), the camera and the message composer are presented from here so any tab can start them.
struct RootView: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var settings: AppSettings

    var body: some View {
        Group {
            if settings.onboarded {
                TabView(selection: $model.tab) {
                    HomeView()
                        .tabItem { Label("Talk", systemImage: "sparkles") }
                        .tag(AppModel.Tab.talk)
                    MusicView()
                        .tabItem { Label("Music", systemImage: "music.note") }
                        .tag(AppModel.Tab.music)
                    ActivityView()
                        .tabItem { Label("Activity", systemImage: "waveform.path.ecg") }
                        .tag(AppModel.Tab.activity)
                    DevicesView()
                        .tabItem { Label("Devices", systemImage: "laptopcomputer.and.iphone") }
                        .tag(AppModel.Tab.devices)
                    SettingsView()
                        .tabItem { Label("Settings", systemImage: "slider.horizontal.3") }
                        .tag(AppModel.Tab.settings)
                }
                .tint(Theme.accent)
                .fullScreenCover(isPresented: $model.showListening) { ListeningView() }
                .fullScreenCover(item: $model.cameraRequest) { request in
                    CameraCaptureView(request: request) { model.cameraRequest = nil }
                        .ignoresSafeArea()
                }
                .sheet(item: $model.messageDraft) { draft in
                    MessageComposeView(draft: draft) { result in model.messageFinished(draft, result: result) }
                        .ignoresSafeArea()
                }
            } else {
                OnboardingView()
            }
        }
        .preferredColorScheme(.dark)
    }
}

import SwiftUI

/// Five tabs: Talk, Music, History, Memory, Settings — History and Memory like the desktop's pages. Devices opens
/// as a sheet from the PC chip on Talk and from Settings. Voice mode (the full-screen Listening view), the camera
/// and the message composer are presented from here so any tab can start them, and the banner ("Synced with Home
/// PC…") shows on every tab — it used to appear only on Talk, so "Sync now" elsewhere looked like it did nothing.
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
                        .tabItem { Label("History", systemImage: "clock.arrow.circlepath") }
                        .tag(AppModel.Tab.history)
                    LearnedView()
                        .tabItem { Label("Memory", systemImage: "brain.head.profile") }
                        .tag(AppModel.Tab.memory)
                    SettingsView()
                        .tabItem { Label("Settings", systemImage: "slider.horizontal.3") }
                        .tag(AppModel.Tab.settings)
                }
                .tint(Theme.accent)
                .overlay(alignment: .top) { bannerView }
                .sheet(isPresented: $model.showDevices) { DevicesView(asSheet: true) }
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

    @ViewBuilder
    private var bannerView: some View {
        if let text = model.banner {
            Text(text)
                .font(.system(size: 13.5, weight: .medium))
                .foregroundStyle(Theme.text)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                .glassCard(radius: 18, fill: Theme.raised, stroke: Theme.borderStrong)
                .padding(.horizontal, 16)
                .padding(.top, 6)
                .transition(.move(edge: .top).combined(with: .opacity))
                .allowsHitTesting(false)
                .task(id: model.bannerID) {
                    try? await Task.sleep(nanoseconds: 3_500_000_000)
                    withAnimation { model.banner = nil }
                }
        }
    }
}

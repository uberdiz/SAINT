import SwiftUI

struct RootView: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var settings: AppSettings

    var body: some View {
        Group {
            if settings.onboarded {
                TabView {
                    HomeView()
                        .tabItem { Label("SAINT", systemImage: "waveform.circle.fill") }
                    RemindersView()
                        .tabItem { Label("Reminders", systemImage: "bell.badge") }
                    MusicView()
                        .tabItem { Label("Music", systemImage: "music.note") }
                    LearnedView()
                        .tabItem { Label("Learned", systemImage: "brain.head.profile") }
                    DevicesView()
                        .tabItem { Label("Devices", systemImage: "desktopcomputer.and.iphone") }
                }
                .tint(Theme.accent)
            } else {
                OnboardingView()
            }
        }
    }
}

import SwiftUI
import SaintCore

/// Three short pages: what SAINT is, what it needs to hear you, and which languages you speak.
struct OnboardingView: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var settings: AppSettings
    @State private var page = 0
    @State private var asking = false

    var body: some View {
        VStack(spacing: 0) {
            TabView(selection: $page) {
                welcome.tag(0)
                permissions.tag(1)
                languages.tag(2)
            }
            .tabViewStyle(.page(indexDisplayMode: .always))
            .indexViewStyle(.page(backgroundDisplayMode: .interactive))

            Button {
                if page < 2 { withAnimation { page += 1 } } else { finish() }
            } label: {
                Text(page < 2 ? "Continue" : "Start listening").frame(maxWidth: .infinity).padding(.vertical, 6)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .padding(.horizontal, 24)
            .padding(.bottom, 24)
        }
        .saintBackground()
    }

    private var welcome: some View {
        VStack(spacing: 18) {
            Spacer()
            OrbView(phase: .listening, level: 0.2).frame(height: 240)
            Text("Say “SAINT”").font(.system(size: 36, weight: .bold, design: .rounded))
            Text("Keep SAINT open and just talk to it — like Siri, but it's yours. Reminders, music, your PC, in English, Español and more, even mixed in one sentence.")
                .font(.system(.body, design: .rounded)).foregroundStyle(.secondary).multilineTextAlignment(.center).padding(.horizontal, 32)
            Spacer()
        }
    }

    private var permissions: some View {
        VStack(spacing: 18) {
            Spacer()
            Image(systemName: "mic.circle.fill").font(.system(size: 80)).foregroundStyle(Theme.accent)
            Text("To hear you").font(.system(size: 30, weight: .bold, design: .rounded))
            VStack(alignment: .leading, spacing: 12) {
                row("mic", "Microphone", "so it can listen for “SAINT”")
                row("waveform", "Speech recognition", "done on this phone where iOS allows")
                row("bell", "Notifications", "so reminders and timers ring")
                row("wifi", "Local network", "asked when it first talks to your PC")
            }
            .padding(.horizontal, 32)
            Button { Task { asking = true; await model.requestAllPermissions(); asking = false } } label: {
                Text(model.permissionsOK ? "Allowed ✓" : (asking ? "Asking…" : "Allow")).frame(minWidth: 160)
            }
            .buttonStyle(.bordered)
            .controlSize(.large)
            Spacer()
        }
    }

    private func row(_ icon: String, _ title: String, _ detail: String) -> some View {
        HStack(spacing: 14) {
            Image(systemName: icon).font(.title3).frame(width: 30).foregroundStyle(Theme.accent)
            VStack(alignment: .leading) {
                Text(title).font(.system(.headline, design: .rounded))
                Text(detail).font(.subheadline).foregroundStyle(.secondary)
            }
        }
    }

    private let choices: [(code: String, name: String)] = [
        ("en", "English"), ("es", "Español"), ("fr", "Français"), ("pt", "Português"), ("de", "Deutsch"), ("it", "Italiano"),
    ]

    private var languages: some View {
        VStack(spacing: 16) {
            Spacer()
            Image(systemName: "globe").font(.system(size: 70)).foregroundStyle(Theme.accent)
            Text("Which languages?").font(.system(size: 30, weight: .bold, design: .rounded))
            Text("Pick up to two. SAINT understands both at once, and answers in the one you spoke.")
                .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center).padding(.horizontal, 32)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 120), spacing: 10)], spacing: 10) {
                ForEach(choices, id: \.code) { choice in
                    let on = settings.listenLanguages.contains(choice.code)
                    Button { toggle(choice.code) } label: {
                        Text(choice.name).frame(maxWidth: .infinity).padding(.vertical, 8)
                    }
                    .buttonStyle(.bordered)
                    .tint(on ? Theme.accent : .gray)
                    .overlay(alignment: .topTrailing) {
                        if on { Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.accent).offset(x: 4, y: -4) }
                    }
                }
            }
            .padding(.horizontal, 32)
            Spacer()
        }
    }

    private func toggle(_ code: String) {
        var list = settings.listenLanguages
        if let i = list.firstIndex(of: code) {
            if list.count > 1 { list.remove(at: i) }
        } else {
            list.append(code)
            if list.count > 2 { list.removeFirst() }
        }
        model.setLanguages(list)
    }

    private func finish() {
        settings.onboarded = true
        if model.permissionsOK && settings.alwaysListening { model.voice.start() }
        Task { await model.start() }
    }
}

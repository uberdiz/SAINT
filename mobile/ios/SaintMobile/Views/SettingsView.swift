import SwiftUI
import AVFoundation
import SaintCore

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var settings: AppSettings
    @EnvironmentObject var spotify: SpotifyService
    @EnvironmentObject var speaker: Speaker
    @Environment(\.dismiss) private var dismiss

    @State private var mixedMode = "mirror"
    @State private var replyInUserLanguage = true
    @State private var claudeKey = ""

    private let languages: [(code: String, name: String)] = [
        ("en", "English"), ("es", "Español"), ("fr", "Français"), ("pt", "Português"), ("de", "Deutsch"), ("it", "Italiano"),
    ]

    var body: some View {
        NavigationStack {
            Form {
                listening
                languagesSection
                voiceSection
                aiSection
                spotifySection
                deviceSection
                aboutSection
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .onAppear {
                mixedMode = model.brain.settings.mixedMode
                replyInUserLanguage = model.brain.settings.replyInUserLanguage
                claudeKey = settings.claudeKey
            }
        }
    }

    // MARK: listening

    private var listening: some View {
        Section {
            Toggle("Always listening for “SAINT”", isOn: Binding(get: { settings.alwaysListening }, set: { model.setAlwaysListening($0) }))
            Toggle("Keep the screen on while SAINT is open", isOn: $settings.keepScreenOn)
            Toggle("Chime when I'm heard", isOn: $settings.chime)
            Toggle("Haptics", isOn: $settings.haptics)
            VStack(alignment: .leading) {
                Text("Wait this long after I stop talking: \(settings.endpointSeconds, specifier: "%.1f")s")
                Slider(value: $settings.endpointSeconds, in: 0.7...2.5, step: 0.1)
            }
            TextField("Extra wake words (comma separated)", text: $settings.customWakeWords)
                .textInputAutocapitalization(.never).autocorrectionDisabled()
        } header: {
            Text("Listening")
        } footer: {
            Text("SAINT keeps the microphone open so it can hear its name, like “Hey Siri”. Speech is recognised on this phone where iOS allows it. "
                 + "Siri's own wake phrase can't be changed — SAINT's word only works while SAINT is running. "
                 + "Locked screen: it keeps listening as long as SAINT stays open in the background.")
        }
    }

    private var languagesSection: some View {
        Section {
            ForEach(0..<2, id: \.self) { slot in
                Picker(slot == 0 ? "Main language" : "Also understand", selection: languageBinding(slot)) {
                    if slot == 1 { Text("None").tag("") }
                    ForEach(languages, id: \.code) { Text($0.name).tag($0.code) }
                }
            }
            Toggle("Answer in the language I spoke", isOn: Binding(get: { replyInUserLanguage }, set: {
                replyInUserLanguage = $0
                model.brain.settings.update(replyInUserLanguage: $0)
                model.brain.reloadSettings()
            }))
            Picker("When I mix languages", selection: Binding(get: { mixedMode }, set: {
                mixedMode = $0
                model.brain.settings.update(mixedMode: $0)
                model.brain.reloadSettings()
            })) {
                Text("Answer the same mix").tag("mirror")
                Text("Answer in my main language").tag("dominant")
            }
        } header: {
            Text("Languages")
        } footer: {
            Text("Say things in Spanish, English or both in one sentence — “pon some jazz”. These preferences are shared with your PC.")
        }
    }

    private func languageBinding(_ slot: Int) -> Binding<String> {
        Binding(get: {
            let list = settings.listenLanguages
            return slot < list.count ? list[slot] : ""
        }, set: { value in
            var list = settings.listenLanguages
            while list.count < 2 { list.append("") }
            list[slot] = value
            let cleaned = list.filter { !$0.isEmpty }.reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
            model.setLanguages(cleaned.isEmpty ? ["en"] : cleaned)
        })
    }

    // MARK: voice

    private var voiceSection: some View {
        Section("SAINT's voice") {
            Toggle("Speak replies", isOn: $settings.speakReplies)
            VStack(alignment: .leading) {
                Text("Speed")
                Slider(value: $settings.speechRate, in: 0.35...0.6)
            }
            Button("Hear it") {
                speaker.speak("Hi, I'm SAINT. Hola, soy SAINT.", language: "en")
            }
            Text("Better voices: iOS Settings → Accessibility → Spoken Content → Voices.").font(.footnote).foregroundStyle(.secondary)
        }
    }

    // MARK: models

    private var aiSection: some View {
        Section {
            Toggle("Ask my PC first for anything else", isOn: $settings.preferPC)
            HStack {
                Text("On-device model")
                Spacer()
                Text(OnDeviceModel.isAvailable ? "Available" : "Not on this phone").foregroundStyle(.secondary)
            }
            Toggle("Also use Claude (needs internet)", isOn: $settings.useClaude)
            if settings.useClaude {
                SecureField("Claude API key", text: $claudeKey)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    .onChange(of: claudeKey) { _, new in settings.claudeKey = new }
                TextField("Model", text: $settings.claudeModel).textInputAutocapitalization(.never).autocorrectionDisabled()
            }
        } header: {
            Text("Answers")
        } footer: {
            Text("Commands, reminders, memory and music work on the phone alone. Open questions go to your PC's SAINT when it's connected, "
                 + "then to Apple's on-device model (iOS 26 with Apple Intelligence), then to Claude if you add a key. The key stays in the Keychain.")
        }
    }

    // MARK: spotify

    private var spotifySection: some View {
        Section {
            TextField("Spotify client ID", text: $settings.spotifyClientID)
                .textInputAutocapitalization(.never).autocorrectionDisabled()
            HStack {
                Text("Redirect URI")
                Spacer()
                Text(SpotifyService.redirectURI).font(.footnote).foregroundStyle(.secondary).textSelection(.enabled)
            }
            if spotify.connected {
                Button("Sign out of Spotify", role: .destructive) { spotify.disconnect() }
            } else {
                Button("Sign in to Spotify") { Task { await spotify.connect() } }
                    .disabled(settings.spotifyClientID.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        } header: {
            Text("Spotify")
        } footer: {
            Text("Create a free app at developer.spotify.com/dashboard, add the redirect URI above, and paste its client ID. Controlling playback needs Spotify Premium.")
        }
    }

    // MARK: this device

    private var deviceSection: some View {
        Section {
            TextField("Name shown on your other devices", text: $settings.deviceName)
            Toggle("Share what I just did with my other devices", isOn: $settings.shareContext)
        } header: {
            Text("This phone")
        } footer: {
            Text("With sharing on, “what did I just ask?” works on whichever device you're using. Recent turns are kept in memory only, for half an hour.")
        }
    }

    private var aboutSection: some View {
        Section("Permissions") {
            Button("Allow microphone, speech & notifications") { Task { await model.requestAllPermissions() } }
            Button("Open iOS Settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
            }
            Text("Local Network access (for finding and talking to your PC) is asked the first time SAINT connects.")
                .font(.footnote).foregroundStyle(.secondary)
        }
    }
}

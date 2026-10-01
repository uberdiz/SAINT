import SwiftUI
import AVFoundation
import Contacts
import Photos
import SaintCore

/// The Settings tab, in the desktop's style: cards of rows, orange switches.
struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var settings: AppSettings
    @EnvironmentObject var spotify: SpotifyService
    @EnvironmentObject var speaker: Speaker
    @EnvironmentObject var voice: VoiceEngine

    @State private var mixedMode = "mirror"
    @State private var replyInUserLanguage = true
    @State private var claudeKey = ""
    @State private var showReminders = false
    @State private var showLearned = false
    @State private var showShortcuts = false
    @State private var phonePermissions = PhonePermissions.current()

    private let languages: [(code: String, name: String)] = [
        ("en", "English"), ("es", "Español"), ("fr", "Français"), ("pt", "Português"), ("de", "Deutsch"), ("it", "Italiano"),
    ]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    header
                    audioSection
                    listeningSection
                    voiceSection
                    phoneSection
                    languagesSection
                    answersSection
                    spotifySection
                    dataSection
                    deviceSection
                    permissionsSection
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 32)
            }
            .scrollDismissesKeyboard(.interactively)
            .dismissKeyboardOnDragDown()
            .saintBackground()
            .navigationTitle("Settings")
            .onAppear {
                mixedMode = model.brain.settings.mixedMode
                replyInUserLanguage = model.brain.settings.replyInUserLanguage
                claudeKey = settings.claudeKey
                phonePermissions = PhonePermissions.current()
            }
            .sheet(isPresented: $showReminders) { RemindersView() }
            .sheet(isPresented: $showLearned) { LearnedView() }
            .sheet(isPresented: $showShortcuts) { ShortcutsHelpView() }
        }
    }

    // MARK: header

    private var header: some View {
        HStack(spacing: 14) {
            SaintLogo(size: 48)
            VStack(alignment: .leading, spacing: 3) {
                Text(settings.deviceName).font(.system(size: 17, weight: .semibold)).foregroundStyle(Theme.text)
                Text("SAINT Mobile \(Self.version)").font(.system(size: 12.5)).foregroundStyle(Theme.muted)
            }
            Spacer()
            if let pc = model.brain.ownPC() {
                Pill(text: pc.online ? "PC connected" : "PC offline", icon: "desktopcomputer",
                     tint: pc.online ? Theme.success : Theme.muted, fill: pc.online ? Theme.successSoft : Theme.surface2)
            }
        }
        .padding(16)
        .glassCard(radius: 20)
        .padding(.top, 4)
    }

    static var version: String {
        (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? ""
    }

    // MARK: audio

    private var audioSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle("Audio")
            CardList {
                SettingRow(icon: "speaker.wave.2.fill", title: "Output", subtitle: "Now: \(voice.outputName)",
                           iconTint: Theme.accent, tile: Theme.accentSoft) {
                    Picker("Output", selection: $settings.audioOutput) {
                        Text("Headphones when connected").tag("auto")
                        Text("iPhone speaker").tag("speaker")
                        Text("Earpiece").tag("earpiece")
                    }
                    .labelsHidden()
                    .tint(Theme.accent)
                }
                RowDivider()
                SettingRow(icon: "mic.fill", title: "Use my headphones' microphone",
                           subtitle: settings.audioOutput == "auto"
                               ? "Now: \(voice.inputName). AirPods switch to call quality while their mic is on."
                               : "Only with “Headphones when connected”. Now: \(voice.inputName).") {
                    toggle($settings.useHeadsetMic).disabled(settings.audioOutput != "auto")
                }
            }
            footnote("SAINT talks through your headphones when they're connected, unless you pick the speaker. "
                     + "The earpiece keeps replies private, like a phone call. Voice mode lets you switch too.")
        }
    }

    // MARK: listening

    private var listeningSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle("Listening")
            CardList {
                SettingRow(icon: "ear", title: "Always listening for “SAINT”") {
                    toggle(Binding(get: { settings.alwaysListening }, set: { model.setAlwaysListening($0) }))
                }
                RowDivider()
                sliderRow(icon: "dot.radiowaves.left.and.right", title: "Wake-word sensitivity",
                          value: "\(Int((settings.wakeSensitivity * 100).rounded()))%") {
                    Slider(value: $settings.wakeSensitivity, in: 0...1, step: 0.05).tint(Theme.accent)
                }
                RowDivider()
                sliderRow(icon: "timer", title: "Wait after I stop talking",
                          value: String(format: "%.1fs", settings.endpointSeconds)) {
                    Slider(value: $settings.endpointSeconds, in: 0.7...2.5, step: 0.1).tint(Theme.accent)
                }
                RowDivider()
                VStack(alignment: .leading, spacing: 6) {
                    Text("Extra wake words").font(.system(size: 15)).foregroundStyle(Theme.text)
                    TextField("e.g. hey saint, jarvis", text: $settings.customWakeWords)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .padding(10)
                        .background(Theme.surface2, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                }
                .padding(.horizontal, 14).padding(.vertical, 11)
                RowDivider()
                SettingRow(icon: "character.bubble", title: "Listen in both languages",
                           subtitle: "Uses two recognisers; some iPhones then miss the wake word.") {
                    toggle(Binding(get: { settings.listenBothLanguages }, set: {
                        settings.listenBothLanguages = $0
                        if voice.phase != .off { voice.start() }
                    }))
                }
                RowDivider()
                SettingRow(icon: "bell", title: "Chime when I'm heard") { toggle($settings.chime) }
                RowDivider()
                SettingRow(icon: "hand.tap", title: "Haptics") { toggle($settings.haptics) }
                RowDivider()
                SettingRow(icon: "sun.max", title: "Keep the screen on while SAINT is open") { toggle($settings.keepScreenOn) }
                RowDivider()
                SettingRow(icon: "zzz", title: "Snooze reminders for \(settings.snoozeMinutes) min") {
                    Stepper("", value: $settings.snoozeMinutes, in: 1...60).labelsHidden()
                }
            }
            footnote("Say “SAINT” — on its own or at the start of a sentence — then your command. Raise the sensitivity "
                     + "if it misses you, lower it if it wakes up by itself. SAINT only hears its name while it's running "
                     + "(also in the background with the screen locked).")
        }
    }

    // MARK: voice

    private var voiceSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle("SAINT's voice")
            CardList {
                SettingRow(icon: "waveform", title: "Speak replies") { toggle($settings.speakReplies) }
                RowDivider()
                sliderRow(icon: "hare", title: "Speed", value: "\(Int((settings.speechRate * 200).rounded()))%") {
                    Slider(value: $settings.speechRate, in: 0.35...0.6).tint(Theme.accent)
                }
                RowDivider()
                Button { speaker.speak("Hi, I'm SAINT. Hola, soy SAINT.", language: "en") } label: {
                    SettingRow(icon: "play.fill", title: "Hear it", subtitle: "Better voices: iOS Settings → Accessibility → Spoken Content → Voices.",
                               iconTint: Theme.accent, tile: Theme.accentSoft)
                }
                .buttonStyle(.plain)
            }
        }
    }

    // MARK: phone control

    private var phoneSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle("Phone control")
            CardList {
                permissionRow(icon: "person.crop.circle", title: "Contacts", subtitle: "“Call mom”, “text Alex I'm on my way”",
                              granted: phonePermissions.contacts)
                RowDivider()
                permissionRow(icon: "camera", title: "Camera", subtitle: "“Take a picture”, “take a selfie”, “flashlight on”",
                              granted: phonePermissions.camera)
                RowDivider()
                permissionRow(icon: "photo.on.rectangle", title: "Save to Photos", subtitle: "Where SAINT's photos and videos go",
                              granted: phonePermissions.photos)
                RowDivider()
                Button { showShortcuts = true } label: {
                    SettingRow(icon: "square.stack.3d.up", title: "Switches iOS keeps to itself",
                               subtitle: "Low Power Mode, Wi-Fi, Bluetooth, Do Not Disturb… one Shortcut each",
                               iconTint: Theme.accent, tile: Theme.accentSoft) {
                        Image(systemName: "chevron.right").font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.faint)
                    }
                }
                .buttonStyle(.plain)
            }
            if !phonePermissions.all {
                Button {
                    Task {
                        await PhonePermissions.requestAll()
                        phonePermissions = PhonePermissions.current()
                    }
                } label: {
                    Text("Allow contacts, camera & photos")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Theme.onAccent)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 12)
                        .background(Theme.accent, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                }
            }
            footnote("SAINT does what iOS lets an app do by itself: the flashlight, brightness, the camera, opening apps, "
                     + "directions, the battery. Calls and texts are filled in for you and you tap Call or Send — iOS never lets "
                     + "an app do that alone. Try “open Instagram”, “brightness 40%”, “navigate home”, “what's my battery?”.")
        }
    }

    private func permissionRow(icon: String, title: String, subtitle: String, granted: Bool) -> some View {
        SettingRow(icon: icon, title: title, subtitle: subtitle) {
            Pill(text: granted ? "Allowed" : "Not yet", icon: granted ? "checkmark" : nil,
                 tint: granted ? Theme.success : Theme.muted, fill: granted ? Theme.successSoft : Theme.surface2)
        }
    }

    // MARK: languages

    private var languagesSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle("Languages")
            CardList {
                ForEach(0..<2, id: \.self) { slot in
                    if slot == 1 { RowDivider() }
                    SettingRow(icon: slot == 0 ? "globe" : "plus.bubble", title: slot == 0 ? "Main language" : "Also understand") {
                        Picker(slot == 0 ? "Main language" : "Also understand", selection: languageBinding(slot)) {
                            if slot == 1 { Text("None").tag("") }
                            ForEach(languages, id: \.code) { Text($0.name).tag($0.code) }
                        }
                        .labelsHidden()
                        .tint(Theme.accent)
                    }
                }
                RowDivider()
                SettingRow(icon: "text.bubble", title: "Answer in the language I spoke") {
                    toggle(Binding(get: { replyInUserLanguage }, set: {
                        replyInUserLanguage = $0
                        model.brain.settings.update(replyInUserLanguage: $0)
                        model.brain.reloadSettings()
                    }))
                }
                RowDivider()
                SettingRow(icon: "arrow.triangle.branch", title: "When I mix languages") {
                    Picker("When I mix languages", selection: Binding(get: { mixedMode }, set: {
                        mixedMode = $0
                        model.brain.settings.update(mixedMode: $0)
                        model.brain.reloadSettings()
                    })) {
                        Text("Same mix").tag("mirror")
                        Text("Main language").tag("dominant")
                    }
                    .labelsHidden()
                    .tint(Theme.accent)
                }
            }
            footnote("Say things in Spanish, English or both in one sentence — “pon some jazz”. Shared with your PC.")
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

    // MARK: answers

    private var answersSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle("Answers")
            CardList {
                SettingRow(icon: "desktopcomputer", title: "Ask my PC first for anything else") { toggle($settings.preferPC) }
                RowDivider()
                SettingRow(icon: "cpu", title: "On-device model") {
                    Text(OnDeviceModel.isAvailable ? "Available" : "Not on this phone")
                        .font(.system(size: 13)).foregroundStyle(Theme.muted)
                }
                RowDivider()
                SettingRow(icon: "sparkles", title: "Also use Claude", subtitle: "Needs internet and an API key") {
                    toggle($settings.useClaude)
                }
                if settings.useClaude {
                    RowDivider()
                    VStack(spacing: 8) {
                        SecureField("Claude API key", text: $claudeKey)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                            .onChange(of: claudeKey) { _, new in settings.claudeKey = new }
                        TextField("Model", text: $settings.claudeModel).textInputAutocapitalization(.never).autocorrectionDisabled()
                    }
                    .padding(10)
                    .background(Theme.surface2, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .padding(.horizontal, 14).padding(.vertical, 10)
                }
            }
            footnote("Commands, reminders, memory, music and phone control work on the phone alone. Open questions go to your PC's "
                     + "SAINT when it's connected, then Apple's on-device model, then Claude if you add a key (kept in the Keychain).")
        }
    }

    // MARK: spotify

    private var spotifySection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle("Spotify")
            CardList {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Client ID").font(.system(size: 15)).foregroundStyle(Theme.text)
                    TextField("From developer.spotify.com/dashboard", text: $settings.spotifyClientID)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .padding(10)
                        .background(Theme.surface2, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                }
                .padding(.horizontal, 14).padding(.vertical, 11)
                RowDivider()
                SettingRow(icon: "link", title: "Redirect URI", subtitle: SpotifyService.redirectURI) {
                    Button { UIPasteboard.general.string = SpotifyService.redirectURI; model.banner = "Copied." } label: {
                        Image(systemName: "doc.on.doc").foregroundStyle(Theme.accent)
                    }
                }
                RowDivider()
                if spotify.connected {
                    Button(role: .destructive) { spotify.disconnect() } label: {
                        SettingRow(icon: "rectangle.portrait.and.arrow.right", title: "Sign out of Spotify", iconTint: Theme.danger, tile: Theme.dangerSoft)
                    }
                    .buttonStyle(.plain)
                } else {
                    Button { Task { await spotify.connect() } } label: {
                        SettingRow(icon: "music.note", title: "Sign in to Spotify", iconTint: Theme.accent, tile: Theme.accentSoft)
                    }
                    .buttonStyle(.plain)
                    .disabled(settings.spotifyClientID.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            footnote("Create a free app at developer.spotify.com/dashboard, add the redirect URI above exactly, and paste its client ID. "
                     + "Controlling playback needs Spotify Premium. If you signed in before this version, sign in again so SAINT can "
                     + "read your queue and recommend songs. " + SpotifyService.redirectHelp)
        }
    }

    // MARK: data

    private var dataSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle("What SAINT knows")
            CardList {
                Button { showReminders = true } label: {
                    SettingRow(icon: "bell.badge", title: "Reminders & timers") { chevron }
                }
                .buttonStyle(.plain)
                RowDivider()
                Button { showLearned = true } label: {
                    SettingRow(icon: "brain.head.profile", title: "Learned", subtitle: "Memories, skills, routines and nicknames") { chevron }
                }
                .buttonStyle(.plain)
                RowDivider()
                Button { model.tab = .activity } label: {
                    SettingRow(icon: "waveform.path.ecg", title: "Activity log", subtitle: "Everything SAINT did; synced to your PC's History") { chevron }
                }
                .buttonStyle(.plain)
            }
        }
    }

    // MARK: this device

    private var deviceSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle("This phone")
            CardList {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Name on your other devices").font(.system(size: 15)).foregroundStyle(Theme.text)
                    TextField("iPhone", text: $settings.deviceName)
                        .padding(10)
                        .background(Theme.surface2, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                }
                .padding(.horizontal, 14).padding(.vertical, 11)
                RowDivider()
                SettingRow(icon: "arrow.left.arrow.right", title: "Share what I just did with my other devices",
                           subtitle: "“What did I just ask?” works anywhere; kept for half an hour") {
                    toggle($settings.shareContext)
                }
            }
        }
    }

    private var permissionsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle("Permissions")
            CardList {
                Button { Task { await model.requestAllPermissions() } } label: {
                    SettingRow(icon: "mic.badge.plus", title: "Allow microphone, speech & notifications",
                               subtitle: model.permissionsOK ? "Allowed" : "Needed to hear “SAINT”") { chevron }
                }
                .buttonStyle(.plain)
                RowDivider()
                Button {
                    if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                } label: {
                    SettingRow(icon: "gear", title: "Open iOS Settings") { chevron }
                }
                .buttonStyle(.plain)
            }
            footnote("Local Network access (for finding your PC) is asked the first time SAINT connects.")
        }
    }

    // MARK: pieces

    private var chevron: some View {
        Image(systemName: "chevron.right").font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.faint)
    }

    private func toggle(_ binding: Binding<Bool>) -> some View {
        Toggle("", isOn: binding).labelsHidden().tint(Theme.accent)
    }

    private func sliderRow<S: View>(icon: String, title: String, value: String, @ViewBuilder slider: () -> S) -> some View {
        VStack(spacing: 4) {
            SettingRow(icon: icon, title: title) {
                Text(value).font(.system(size: 13, weight: .medium).monospacedDigit()).foregroundStyle(Theme.accent)
            }
            slider().padding(.horizontal, 14).padding(.bottom, 10)
        }
    }

    private func footnote(_ text: String) -> some View {
        Text(text).font(.system(size: 12)).foregroundStyle(Theme.faint).padding(.horizontal, 4)
    }
}

/// Contacts, camera and add-to-Photos: what "call mom" and "take a picture" need.
struct PhonePermissions {
    var contacts: Bool
    var camera: Bool
    var photos: Bool
    var all: Bool { contacts && camera && photos }

    static func current() -> PhonePermissions {
        let c = CNContactStore.authorizationStatus(for: .contacts)
        var contacts = c == .authorized
        if #available(iOS 18.0, *), c == .limited { contacts = true }
        let photo = PHPhotoLibrary.authorizationStatus(for: .addOnly)
        return PhonePermissions(contacts: contacts,
                                camera: AVCaptureDevice.authorizationStatus(for: .video) == .authorized,
                                photos: photo == .authorized || photo == .limited)
    }

    static func requestAll() async {
        _ = try? await CNContactStore().requestAccess(for: .contacts)
        _ = await AVCaptureDevice.requestAccess(for: .video)
        _ = await PHPhotoLibrary.requestAuthorization(for: .addOnly)
    }
}

/// How to make the Shortcuts SAINT runs for Low Power Mode, Wi-Fi and the other switches only iOS controls.
struct ShortcutsHelpView: View {
    @Environment(\.dismiss) private var dismiss

    private static let actions: [String: String] = [
        "low power mode": "Set Low Power Mode", "wi-fi": "Set Wi-Fi", "bluetooth": "Set Bluetooth",
        "do not disturb": "Set Focus (Do Not Disturb)", "airplane mode": "Set Airplane Mode", "dark mode": "Set Appearance (Dark / Light)",
        "focus": "Set Focus", "hotspot": "Set Personal Hotspot",
    ]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text("iOS doesn't let any app flip these switches itself. Make one Shortcut for each — once — and SAINT runs it when you ask: “SAINT, turn on low power mode”.")
                        .font(.system(size: 14)).foregroundStyle(Theme.muted)
                    VStack(alignment: .leading, spacing: 6) {
                        step(1, "Open Shortcuts and tap +.")
                        step(2, "Add the action shown below and set it to On (or Off).")
                        step(3, "Name the shortcut exactly as shown. That's it.")
                    }
                    CardList {
                        ForEach(Array(PhoneActions.systemShortcuts.enumerated()), id: \.offset) { i, item in
                            if i > 0 { RowDivider() }
                            VStack(alignment: .leading, spacing: 4) {
                                Text("\(PhoneActions.shortcutName(item.name, on: true))  ·  \(PhoneActions.shortcutName(item.name, on: false))")
                                    .font(.system(size: 14, weight: .semibold)).foregroundStyle(Theme.text)
                                    .textSelection(.enabled)
                                Text("Action: \(Self.actions[item.name] ?? item.title)").font(.system(size: 12)).foregroundStyle(Theme.muted)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 14).padding(.vertical, 11)
                        }
                    }
                    Text("Any other Shortcut works too: “SAINT, run my Good Morning shortcut”.")
                        .font(.system(size: 12)).foregroundStyle(Theme.faint)
                    Button {
                        if let url = URL(string: "shortcuts://") { UIApplication.shared.open(url) }
                    } label: {
                        Text("Open Shortcuts")
                            .font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(Theme.onAccent)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 12)
                            .background(Theme.accent, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    }
                }
                .padding(16)
            }
            .saintBackground()
            .navigationTitle("Phone switches")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() }.tint(Theme.accent) } }
        }
        .preferredColorScheme(.dark)
    }

    private func step(_ n: Int, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Text("\(n)").font(.system(size: 12, weight: .bold)).foregroundStyle(Theme.onAccent)
                .frame(width: 20, height: 20).background(Theme.accent, in: Circle())
            Text(text).font(.system(size: 14)).foregroundStyle(Theme.text)
        }
    }
}

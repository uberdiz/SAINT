import Foundation
import SwiftUI
import SaintCore

/// What you can change in Settings. Everything here is per-phone (UserDefaults); the language preferences that
/// should follow you between devices live in SaintCore's SettingsStore and sync instead.
final class AppSettings: ObservableObject {
    private let defaults = UserDefaults.standard
    private let keychain: Keychain

    @Published var alwaysListening: Bool { didSet { defaults.set(alwaysListening, forKey: "alwaysListening") } }
    @Published var keepScreenOn: Bool { didSet { defaults.set(keepScreenOn, forKey: "keepScreenOn") } }
    @Published var speakReplies: Bool { didSet { defaults.set(speakReplies, forKey: "speakReplies") } }
    @Published var chime: Bool { didSet { defaults.set(chime, forKey: "chime") } }
    @Published var haptics: Bool { didSet { defaults.set(haptics, forKey: "haptics") } }
    @Published var preferPC: Bool { didSet { defaults.set(preferPC, forKey: "preferPC") } }
    @Published var shareContext: Bool { didSet { defaults.set(shareContext, forKey: "shareContext") } }
    @Published var deviceName: String { didSet { defaults.set(deviceName, forKey: "deviceName") } }
    @Published var speechRate: Double { didSet { defaults.set(speechRate, forKey: "speechRate") } }
    /// Languages SAINT listens in, most likely first (two at a time is plenty: "en" and "es").
    @Published var listenLanguages: [String] { didSet { defaults.set(listenLanguages, forKey: "listenLanguages") } }
    @Published var customWakeWords: String { didSet { defaults.set(customWakeWords, forKey: "customWakeWords") } }
    @Published var endpointSeconds: Double { didSet { defaults.set(endpointSeconds, forKey: "endpointSeconds") } }
    @Published var spotifyClientID: String { didSet { defaults.set(spotifyClientID, forKey: "spotifyClientID") } }
    @Published var useClaude: Bool { didSet { defaults.set(useClaude, forKey: "useClaude") } }
    @Published var claudeModel: String { didSet { defaults.set(claudeModel, forKey: "claudeModel") } }
    @Published var onboarded: Bool { didSet { defaults.set(onboarded, forKey: "onboarded") } }
    /// Listen through a Bluetooth headset's own microphone. Off by default: that switches AirPods to the
    /// phone-call profile, which makes music sound like a call (VoiceEngine.configureSession).
    @Published var useHeadsetMic: Bool { didSet { defaults.set(useHeadsetMic, forKey: "useHeadsetMic") } }
    /// Minutes a reminder is snoozed for from its notification.
    @Published var snoozeMinutes: Int { didSet { defaults.set(snoozeMinutes, forKey: "snoozeMinutes") } }
    /// Where SAINT's voice and sounds play: "auto" (headphones when connected), "speaker" or "earpiece".
    @Published var audioOutput: String { didSet { defaults.set(audioOutput, forKey: "audioOutput") } }
    /// 0 (only clear "SAINT"s) … 1 (wakes most easily).
    @Published var wakeSensitivity: Double { didSet { defaults.set(wakeSensitivity, forKey: "wakeSensitivity") } }
    /// Listen in both chosen languages at once (two recognisers; some iPhones can't run two reliably).
    @Published var listenBothLanguages: Bool { didSet { defaults.set(listenBothLanguages, forKey: "listenBothLanguages") } }
    /// The playlist "add this to my playlist" used last.
    @Published var lastPlaylist: String { didSet { defaults.set(lastPlaylist, forKey: "lastPlaylist") } }

    var claudeKey: String {
        get { keychain.string("claude.api.key") ?? "" }
        set {
            keychain.setString(newValue.trimmingCharacters(in: .whitespacesAndNewlines), account: "claude.api.key")
            objectWillChange.send()
        }
    }

    init(keychain: Keychain) {
        self.keychain = keychain
        let d = UserDefaults.standard
        func bool(_ key: String, _ fallback: Bool) -> Bool { d.object(forKey: key) == nil ? fallback : d.bool(forKey: key) }
        alwaysListening = bool("alwaysListening", true)
        keepScreenOn = bool("keepScreenOn", true)
        speakReplies = bool("speakReplies", true)
        chime = bool("chime", true)
        haptics = bool("haptics", true)
        preferPC = bool("preferPC", true)
        shareContext = bool("shareContext", true)
        deviceName = d.string(forKey: "deviceName") ?? AppSettings.defaultDeviceName()
        speechRate = d.object(forKey: "speechRate") == nil ? 0.5 : d.double(forKey: "speechRate")
        listenLanguages = d.stringArray(forKey: "listenLanguages") ?? AppSettings.defaultLanguages()
        customWakeWords = d.string(forKey: "customWakeWords") ?? ""
        endpointSeconds = d.object(forKey: "endpointSeconds") == nil ? 1.2 : d.double(forKey: "endpointSeconds")
        spotifyClientID = d.string(forKey: "spotifyClientID") ?? ""
        useClaude = bool("useClaude", false)
        claudeModel = d.string(forKey: "claudeModel") ?? "claude-haiku-4-5-20251001"
        onboarded = bool("onboarded", false)
        useHeadsetMic = bool("useHeadsetMic", false)
        snoozeMinutes = d.object(forKey: "snoozeMinutes") == nil ? 10 : max(1, d.integer(forKey: "snoozeMinutes"))
        audioOutput = d.string(forKey: "audioOutput") ?? "auto"
        wakeSensitivity = d.object(forKey: "wakeSensitivity") == nil ? 0.5 : d.double(forKey: "wakeSensitivity")
        listenBothLanguages = bool("listenBothLanguages", false)
        lastPlaylist = d.string(forKey: "lastPlaylist") ?? ""
    }

    static func defaultDeviceName() -> String {
        let raw = UIDevice.current.name
        return raw.isEmpty ? "iPhone" : raw
    }

    /// The phone's preferred languages, as the two-letter codes SAINT's language packs use, English first when present.
    static func defaultLanguages() -> [String] {
        var codes: [String] = []
        for identifier in Locale.preferredLanguages {
            let code = String(identifier.prefix(2)).lowercased()
            if !codes.contains(code) { codes.append(code) }
        }
        if !codes.contains("en") { codes.append("en") }
        return Array(codes.prefix(2))
    }

    var wakeVariants: Set<String> {
        let extra = customWakeWords.split(whereSeparator: { ",;\n".contains($0) }).map { String($0) }
        return WakeWord.variants(languages: listenLanguages, extra: extra)
    }

    /// Locale identifiers for speech recognition: "es" -> "es-ES", from the language packs where we have them.
    func recognitionLocale(for code: String) -> Locale {
        if code == "en" {
            let phone = Locale.current
            return phone.language.languageCode?.identifier == "en" ? phone : Locale(identifier: "en-US")
        }
        if let pack = LangPack.pack(code), !pack.locale.isEmpty { return Locale(identifier: pack.locale) }
        return Locale(identifier: code)
    }
}

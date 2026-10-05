import Foundation
import AVFoundation
import SaintCore

/// Speaks SAINT's replies, one voice per language: a sentence that mixes English and Spanish is read in two
/// voices, each in its own accent, instead of one voice mangling the other language.
final class Speaker: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {
    private let synthesizer = AVSpeechSynthesizer()
    private var active = Set<ObjectIdentifier>()       // the utterances of the current reply
    private var onFinish: (() -> Void)?
    @Published private(set) var isSpeaking = false
    var rate: Double = 0.5
    /// A voice identifier chosen per language code ("es" -> "com.apple.voice.premium.es-MX.Paulina").
    var preferredVoices: [String: String] = [:]

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    func speak(_ text: String, language: String, completion: (() -> Void)? = nil) {
        let parts = LangSegments.split(text, hint: language)
        speak(parts.map { (language: $0.language, text: $0.text) }, completion: completion)
    }

    func speak(_ segments: [(language: String, text: String)], completion: (() -> Void)? = nil) {
        stop()
        let spoken = segments.filter { !$0.text.trimmed.isEmpty }
        if spoken.isEmpty {
            completion?()
            return
        }
        onFinish = completion
        DispatchQueue.main.async { self.isSpeaking = true }
        var utterances: [AVSpeechUtterance] = []
        for segment in spoken {
            let utterance = AVSpeechUtterance(string: segment.text)
            utterance.voice = voice(for: segment.language)
            utterance.rate = Float(max(0.3, min(0.65, rate)))
            utterance.pitchMultiplier = 1.0
            utterance.preUtteranceDelay = 0
            utterance.postUtteranceDelay = 0.05
            utterances.append(utterance)
        }
        active = Set(utterances.map { ObjectIdentifier($0) })
        for utterance in utterances { synthesizer.speak(utterance) }
    }

    func stop() {
        onFinish = nil
        active = []                                   // late "cancelled" callbacks from this reply are then ignored
        if synthesizer.isSpeaking { synthesizer.stopSpeaking(at: .immediate) }
        DispatchQueue.main.async { self.isSpeaking = false }
    }

    private func localeIdentifier(_ code: String) -> String {
        if code == "en" { return "en-US" }
        if let pack = LangPack.pack(code), !pack.locale.isEmpty { return pack.locale }
        return code
    }

    /// The best installed voice for a language: the user's choice, else the highest-quality one that matches.
    func voice(for code: String) -> AVSpeechSynthesisVoice? {
        if let id = preferredVoices[code], let chosen = AVSpeechSynthesisVoice(identifier: id) { return chosen }
        let locale = localeIdentifier(code)
        let candidates = AVSpeechSynthesisVoice.speechVoices().filter { $0.language.hasPrefix(String(code.prefix(2))) }
        let exact = candidates.filter { $0.language == locale }
        let pool = exact.isEmpty ? candidates : exact
        let best = pool.max { $0.quality.rawValue < $1.quality.rawValue }
        return best ?? AVSpeechSynthesisVoice(language: locale)
    }

    func voices(for code: String) -> [AVSpeechSynthesisVoice] {
        AVSpeechSynthesisVoice.speechVoices().filter { $0.language.hasPrefix(String(code.prefix(2))) }
            .sorted { $0.quality.rawValue > $1.quality.rawValue }
    }

    // MARK: delegate

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        guard active.remove(ObjectIdentifier(utterance)) != nil, active.isEmpty else { return }
        let done = onFinish
        onFinish = nil
        DispatchQueue.main.async {
            self.isSpeaking = false
            done?()
        }
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        active.remove(ObjectIdentifier(utterance))
    }
}

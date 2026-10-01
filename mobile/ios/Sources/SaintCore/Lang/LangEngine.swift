import Foundation

public struct LangSettings {
    public var autoDetect = true
    public var replyInUserLanguage = true
    public var preferred: [String] = []
    public var mixedMode = "mirror"            // "mirror" or "dominant"
    public var stickyMinutes = 10.0

    public init() {}
}

/// SAINT's English reply, in the language the user spoke (modules/lang/reply.py): the language's phrasebook
/// first, sentence by sentence, with placeholders so titles and names pass through untouched.
public enum LangReply {
    static let enDays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    static let enMonths = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                           "October", "November", "December"]
    private static let placeholder = Rx("\\{(\\w+)(?::(\\w+))?\\}", caseInsensitive: false)!
    private static let sentenceBreak = Rx("(?<=[.!?])\\s+(?=[A-Z“\"'¿¡(])", caseInsensitive: false)!

    static func whenPhrase(_ text: String, pack: LangPack) -> String {
        var out = text
        for (rx, replacement) in pack.whenRules { out = rx.sub(replacement, in: out) }
        for (i, name) in enDays.enumerated() where i < pack.weekdays.count {
            if let rx = Rx("\\b\(name)\\b", caseInsensitive: false) { out = rx.sub(pack.weekdays[i], in: out) }
        }
        for (i, name) in enMonths.enumerated() where i < pack.months.count {
            let month = pack.months[i]
            let short = month.count > 3 ? String(month.prefix(3)).lowercased() : month
            if let rx = Rx("\\b\(name.prefix(3))\\b", caseInsensitive: false) { out = rx.sub(short, in: out) }
        }
        return out
    }

    static func phrase(_ sentence: String, pack: LangPack) -> String? {
        let text = sentence.trimmingCharacters(in: .whitespacesAndNewlines)
        let ns = text as NSString
        for entry in pack.phrasebook {
            guard let m = entry.regex.match(text) else { continue }
            let tns = entry.to as NSString
            let found = placeholder.regex.matches(in: entry.to, options: [], range: NSRange(location: 0, length: tns.length))
            var out = ""
            var last = 0
            for ph in found {
                out += tns.substring(with: NSRange(location: last, length: ph.range.location - last))
                let name = tns.substring(with: ph.range(at: 1))
                let how: String? = ph.range(at: 2).location == NSNotFound ? nil : tns.substring(with: ph.range(at: 2))
                let value = LangNormalize.groupText(m, name, in: ns) ?? ""
                switch how {
                case "when": out += whenPhrase(value, pack: pack)
                case "noun": out += pack.keyNames[value.lowercased()] ?? value
                case "weekday":
                    if let i = enDays.firstIndex(of: value.capitalized), i < pack.weekdays.count {
                        out += pack.weekdays[i].lowercased()
                    } else { out += value }
                case "month":
                    if let i = enMonths.firstIndex(of: value.capitalized), i < pack.months.count {
                        out += pack.months[i]
                    } else { out += value }
                default: out += value
                }
                last = ph.range.location + ph.range.length
            }
            out += tns.substring(from: last)
            return out
        }
        return nil
    }

    static func sentences(_ text: String) -> [String] {
        let ns = text as NSString
        let found = sentenceBreak.regex.matches(in: text, options: [], range: NSRange(location: 0, length: ns.length))
        if found.isEmpty { return [text] }
        var parts: [String] = []
        var last = 0
        for m in found {
            parts.append(ns.substring(with: NSRange(location: last, length: m.range.location - last)))
            last = m.range.location + m.range.length
        }
        parts.append(ns.substring(from: last))
        return parts
    }

    /// ``complete`` is false when part of the reply had no phrasebook entry and was left in English — the
    /// caller may then ask a language model to translate it.
    public static func localize(_ reply: String, code: String) -> (text: String, complete: Bool) {
        if reply.isEmpty || code.isEmpty || code == "en" || code == "und" { return (reply, true) }
        guard let pack = LangPack.pack(code) else { return (reply, false) }
        let trimmed = reply.trimmingCharacters(in: .whitespacesAndNewlines)
        if let whole = phrase(trimmed, pack: pack) { return (whole, true) }
        var out: [String] = []
        var complete = true
        for part in sentences(trimmed) {
            if let t = phrase(part, pack: pack) { out.append(t) } else { complete = false; out.append(part) }
        }
        return (out.joined(separator: " "), complete)
    }
}

/// Split a sentence into stretches of one language so each can be spoken with that language's voice.
public enum LangSegments {
    public static func split(_ text: String, hint: String = "") -> [(language: String, text: String)] {
        if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return [] }
        let det = LangDetect.detect(text, sticky: hint)
        let main = det.primary != "und" ? det.primary : (hint.isEmpty ? "en" : hint)
        if !det.mixed { return [(main, text)] }
        let found = LangDetect.words(in: text)
        if found.count != det.tags.count { return [(main, text)] }
        var langs: [String] = []
        var current = main
        for tag in det.tags {
            if !tag.lang.isEmpty { current = tag.lang }
            langs.append(current)
        }
        if langs.isEmpty { return [(main, text)] }
        var i = 0
        while i < langs.count {                  // a single stray word isn't worth a voice change
            var j = i
            while j < langs.count && langs[j] == langs[i] { j += 1 }
            if j - i == 1 && i > 0 && j < langs.count && langs[i - 1] == langs[j] { langs[i] = langs[i - 1] }
            i = j
        }
        let ns = text as NSString
        var out: [(language: String, text: String)] = []
        var start = 0
        for k in 1...langs.count {
            if k == langs.count || langs[k] != langs[k - 1] {
                let end = k == langs.count ? ns.length : found[k].1.location
                let piece = ns.substring(with: NSRange(location: start, length: end - start))
                if !piece.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    out.append((langs[k - 1], piece))
                }
                start = end
            }
        }
        return out.isEmpty ? [(main, text)] : out
    }
}

/// The result of looking at one utterance.
public struct LangTurn {
    public var text: String
    public var language = "en"               // the language to answer in
    public var secondary = ""
    public var mixed = false
    public var english: String?              // the English command, when the text needed translating
    public var detection: Detection?

    public init(text: String) { self.text = text }

    public var translated: Bool { english != nil && english != text }
    public var routedText: String { translated ? (english ?? text) : text }

    public func directive(mixedMode: String = "mirror") -> String {
        if (language == "" || language == "en" || language == "und") && !mixed { return "" }
        let main = LangPack.languageName(language != "" && language != "und" ? language : "en")
        if mixed && !secondary.isEmpty && mixedMode == "mirror" {
            let other = LangPack.languageName(secondary)
            return "The user mixes \(main) and \(other) in the same sentence. Answer in the same natural mix they used, "
                + "mostly \(main); keep words they said in \(other) (titles, app names, phrases) as they said them. "
                + "Keep it to one short sentence."
        }
        return "The user is speaking \(main). Answer in \(main) only, natural and conversational, in one short sentence. "
            + "Don't translate names, song titles or app names."
    }
}

/// modules/lang/__init__.py: detect the language, translate the command, remember the conversation's language.
public final class LangEngine {
    public var settings = LangSettings()
    private var sticky = ""
    private var stickyAt = Date.distantPast
    public private(set) var replyLanguage = "en"
    public private(set) var replySecondary = ""
    private let lock = NSLock()

    public init(settings: LangSettings = LangSettings()) { self.settings = settings }

    public func currentSticky(now: Date = Date()) -> String {
        lock.lock(); defer { lock.unlock() }
        return now.timeIntervalSince(stickyAt) <= settings.stickyMinutes * 60 ? sticky : ""
    }

    public func reset() {
        lock.lock(); defer { lock.unlock() }
        sticky = ""; stickyAt = .distantPast; replyLanguage = "en"; replySecondary = ""
    }

    public func analyze(_ text: String, hint: String = "", updateState: Bool = true) -> LangTurn {
        if !settings.autoDetect { return LangTurn(text: text) }
        let sticky = currentSticky()
        let det = LangDetect.detect(LangNormalize.stripWake(text), hint: hint, prefer: settings.preferred, sticky: sticky)
        let language = det.primary != "und" ? det.primary : sticky
        var turn = LangTurn(text: text)
        turn.language = language.isEmpty ? "en" : language
        turn.secondary = det.secondary
        turn.mixed = det.mixed
        turn.detection = det

        var tryLanguages: [String] = []
        if !language.isEmpty && language != "en" { tryLanguages.append(language) }
        if !det.secondary.isEmpty && det.secondary != "en" { tryLanguages.append(det.secondary) }
        if det.counts.isEmpty { tryLanguages += det.candidates.filter { $0 != "en" } }
        var seen = Set<String>()
        tryLanguages = tryLanguages.filter { seen.insert($0).inserted }
        if !tryLanguages.isEmpty, let found = LangNormalize.toEnglish(text, languages: tryLanguages) {
            turn.english = found.english
            if found.language != turn.language && (turn.language == "en" || turn.language == "und" || det.counts.isEmpty) {
                turn.language = found.language
            }
        }
        if !settings.replyInUserLanguage { turn.language = "en" }
        lock.lock()
        if updateState {
            if (!det.counts.isEmpty || turn.translated) && turn.language != "" && turn.language != "und" {
                self.sticky = turn.language
                self.stickyAt = Date()
            }
            replyLanguage = turn.language
            replySecondary = turn.mixed ? turn.secondary : ""
        }
        lock.unlock()
        return turn
    }

    public func localize(_ reply: String, for turn: LangTurn) -> (text: String, complete: Bool) {
        if turn.language == "" || turn.language == "en" || turn.language == "und" { return (reply, true) }
        return LangReply.localize(reply, code: turn.language)
    }
}

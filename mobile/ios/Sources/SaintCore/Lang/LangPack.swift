import Foundation

/// A regular expression written for Python (`(?P<name>…)`) compiled for ICU (`(?<name>…)`).
/// The language packs are shared with the desktop app, so they keep the Python spelling.
public struct Rx {
    public let regex: NSRegularExpression

    public init?(_ pattern: String, caseInsensitive: Bool = true) {
        let converted = pattern.replacingOccurrences(of: "(?P<", with: "(?<")
        let options: NSRegularExpression.Options = caseInsensitive ? [.caseInsensitive] : []
        guard let r = try? NSRegularExpression(pattern: converted, options: options) else { return nil }
        regex = r
    }

    /// `re.match` — anchored at the start of ``text``.
    public func match(_ text: String) -> NSTextCheckingResult? {
        let range = NSRange(location: 0, length: (text as NSString).length)
        return regex.firstMatch(in: text, options: [.anchored], range: range)
    }

    public func search(_ text: String) -> NSTextCheckingResult? {
        regex.firstMatch(in: text, options: [], range: NSRange(location: 0, length: (text as NSString).length))
    }

    public func matches(_ text: String) -> Bool { match(text) != nil }

    /// Python's ``rx.sub(template, text)`` with ``\1`` style group references.
    public func sub(_ template: String, in text: String) -> String {
        let ns = text as NSString
        let found = regex.matches(in: text, options: [], range: NSRange(location: 0, length: ns.length))
        if found.isEmpty { return text }
        var result = ""
        var last = 0
        for m in found {
            result += ns.substring(with: NSRange(location: last, length: m.range.location - last))
            result += Rx.expand(template, match: m, in: ns)
            last = m.range.location + m.range.length
        }
        result += ns.substring(from: last)
        return result
    }

    static func expand(_ template: String, match: NSTextCheckingResult, in text: NSString) -> String {
        var out = ""
        let chars = Array(template)
        var i = 0
        while i < chars.count {
            if chars[i] == "\\", i + 1 < chars.count, let d = chars[i + 1].wholeNumberValue {
                if d < match.numberOfRanges {
                    let r = match.range(at: d)
                    if r.location != NSNotFound { out += text.substring(with: r) }
                }
                i += 2
            } else {
                out.append(chars[i])
                i += 1
            }
        }
        return out
    }
}

/// Lower case with accents removed, letter for letter, so captured positions line up with the original text.
public func foldScalar(_ scalar: Unicode.Scalar) -> Unicode.Scalar {
    if scalar.isASCII {
        if scalar.value >= 65 && scalar.value <= 90 { return Unicode.Scalar(scalar.value + 32)! }
        return scalar
    }
    let lowered = String(scalar).lowercased().unicodeScalars
    let c = lowered.count == 1 ? lowered.first! : scalar
    if c.isASCII { return c }
    if c == "ß" { return "s" }
    if let base = String(c).decomposedStringWithCanonicalMapping.unicodeScalars.first, base.isASCII { return base }
    return c
}

public func fold(_ text: String) -> String {
    var scalars = String.UnicodeScalarView()
    for s in text.unicodeScalars { scalars.append(foldScalar(s)) }
    return String(scalars)
}

public struct PackCommand {
    public let regex: Rx
    public let to: String
    public let source: String       // the pattern as written in the pack (Python spelling)
}

public final class LangPack {
    public let code: String
    public var name = ""
    public var english = ""
    public var locale = ""
    public var ttsLang = ""
    public var ttsVoice = ""
    public var chars = ""
    public var words = Set<String>()
    public var verbs: [String: String] = [:]
    public var apps: [String: String] = [:]
    public var keys: [String: String] = [:]
    public var keyNames: [String: String] = [:]      // english noun -> the noun as written in this language
    public var units: [String: String] = [:]
    public var numbers: [String: Int] = [:]
    public var clock: [String: String] = [:]
    public var timeRules: [(Rx, String)] = []
    public var commands: [PackCommand] = []
    public var phrasebook: [PackCommand] = []
    public var weekdays: [String] = []
    public var months: [String] = []
    public var whenRules: [(Rx, String)] = []
    public var politeTail: Rx?
    public var wakePrefix: Rx?
    public var filler: Rx?

    init(code: String) { self.code = code }

    // MARK: loading

    private static var cache: [String: LangPack] = [:]
    private static var missing = Set<String>()
    private static var englishVocabulary: Set<String>?
    private static let cacheLock = NSLock()

    public static let supported = ["es", "fr", "pt", "de", "it"]

    /// Where the lexicon JSON lives: the package bundle by default; tests or the app can point elsewhere.
    public static var directory: URL? = nil

    private static func fileURL(_ code: String) -> URL? {
        if let dir = directory { return dir.appendingPathComponent("\(code).json") }
        return Bundle.module.url(forResource: code, withExtension: "json", subdirectory: "Lexicon")
    }

    private static func loadJSON(_ code: String) -> [String: Any]? {
        guard let url = fileURL(code), let data = try? Data(contentsOf: url),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return nil }
        return object
    }

    public static func englishWords() -> Set<String> {
        cacheLock.lock(); defer { cacheLock.unlock() }
        if let cached = englishVocabulary { return cached }
        let raw = (loadJSON("en")?["words"] as? [String]) ?? []
        let set = Set(raw.map { fold($0) })
        englishVocabulary = set
        return set
    }

    public static func pack(_ code: String) -> LangPack? {
        cacheLock.lock()
        if let hit = cache[code] { cacheLock.unlock(); return hit }
        if missing.contains(code) { cacheLock.unlock(); return nil }
        cacheLock.unlock()
        guard let raw = loadJSON(code) else {
            cacheLock.lock(); missing.insert(code); cacheLock.unlock()
            return nil
        }
        let english = englishWords()
        let pack = LangPack(code: code)
        pack.build(from: raw, englishVocabulary: english)
        cacheLock.lock(); cache[code] = pack; cacheLock.unlock()
        return pack
    }

    public static func available() -> [String] { supported.filter { pack($0) != nil } }

    public static func reset() {
        cacheLock.lock(); defer { cacheLock.unlock() }
        cache.removeAll(); missing.removeAll(); englishVocabulary = nil
    }

    public static func languageName(_ code: String) -> String {
        if code == "en" { return "English" }
        if let p = pack(code), !p.english.isEmpty { return p.english }
        let scripts = ["ja": "Japanese", "zh": "Chinese", "ko": "Korean", "ru": "Russian", "ar": "Arabic",
                       "hi": "Hindi", "th": "Thai", "el": "Greek", "he": "Hebrew"]
        return scripts[code] ?? code
    }

    // MARK: building

    private func pairs(_ value: Any?) -> [(String, String)] {
        guard let list = value as? [[Any]] else { return [] }
        return list.compactMap { row in
            guard row.count >= 2, let a = row[0] as? String, let b = row[1] as? String else { return nil }
            return (a, b)
        }
    }

    private static let syntax = Rx("\\(\\?P<\\w+>|\\\\[a-zA-Z]|\\(\\?[:=!]", caseInsensitive: false)!
    private static let literalWords = Rx("[a-z]{3,}", caseInsensitive: false)!
    private static let placeholder = Rx("\\{[^}]*\\}", caseInsensitive: false)!
    private static let letters = Rx("[^\\W\\d_]+", caseInsensitive: false)!

    private static func allMatches(_ rx: Rx, in text: String) -> [String] {
        let ns = text as NSString
        return rx.regex.matches(in: text, options: [], range: NSRange(location: 0, length: ns.length))
            .map { ns.substring(with: $0.range) }
    }

    func build(from raw: [String: Any], englishVocabulary: Set<String>) {
        name = raw["name"] as? String ?? code
        english = raw["english"] as? String ?? name
        locale = raw["locale"] as? String ?? ""
        ttsLang = raw["tts_lang"] as? String ?? ""
        ttsVoice = raw["tts_voice"] as? String ?? ""
        chars = raw["chars"] as? String ?? ""
        words = Set((raw["words"] as? [String] ?? []).map { fold($0) })
        for (k, v) in (raw["verbs"] as? [String: String]) ?? [:] { verbs[fold(k)] = v }
        for (k, v) in (raw["apps"] as? [String: String]) ?? [:] { apps[fold(k)] = v }
        for (k, v) in (raw["keys"] as? [String: String]) ?? [:] {
            keys[fold(k)] = v
            if keyNames[v] == nil { keyNames[v] = k }
        }
        for (k, v) in (raw["units"] as? [String: String]) ?? [:] { units[fold(k)] = v }
        for (k, v) in (raw["numbers"] as? [String: Any]) ?? [:] {
            if let n = (v as? NSNumber)?.intValue { numbers[fold(k)] = n }
        }
        clock = (raw["clock"] as? [String: String]) ?? [:]
        weekdays = raw["weekdays"] as? [String] ?? []
        months = raw["months"] as? [String] ?? []
        for (a, b) in pairs(raw["commands"]) { if let rx = Rx(a) { commands.append(PackCommand(regex: rx, to: b, source: a)) } }
        for (a, b) in pairs(raw["phrasebook"]) { if let rx = Rx("^" + a + "$") { phrasebook.append(PackCommand(regex: rx, to: b, source: a)) } }
        for (a, b) in pairs(raw["time_rules"]) { if let rx = Rx(a) { timeRules.append((rx, b)) } }
        for (a, b) in pairs(raw["when_rules"]) { if let rx = Rx(a) { whenRules.append((rx, b)) } }
        if let p = raw["polite_tail"] as? String { politeTail = Rx(p) }
        if let p = raw["wake_prefix"] as? String { wakePrefix = Rx(p) }
        if let p = raw["filler"] as? String { filler = Rx(p) }

        // Vocabulary from the pack's own command words and replies, so a bare "pausa" or a spoken-back
        // "Reproduciendo" still says which language this is.
        var extra = Set(verbs.keys)
        for c in commands {
            let stripped = LangPack.syntax.sub(" ", in: c.source)
            for w in LangPack.allMatches(LangPack.literalWords, in: stripped) { extra.insert(w) }
        }
        for c in phrasebook {
            let plain = LangPack.placeholder.sub(" ", in: c.to)
            for w in LangPack.allMatches(LangPack.letters, in: plain) { extra.insert(fold(w)) }
        }
        for w in extra where !englishVocabulary.contains(w) { words.insert(w) }
        if (raw["translit"] as? Bool) == true {
            for w in Array(words) {
                words.insert(w.replacingOccurrences(of: "ae", with: "a").replacingOccurrences(of: "oe", with: "o")
                                .replacingOccurrences(of: "ue", with: "u"))
            }
        }
    }
}

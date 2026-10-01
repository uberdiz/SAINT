import Foundation

/// What the user said, as the English command the intent router understands (modules/lang/normalize.py).
///
///     "pon música de Bad Bunny"              ->  "play Bad Bunny"
///     "recuérdame llamar a mamá a las 5"     ->  "remind me at 5 to llamar a mamá"
///     "pon some jazz"                        ->  "play some jazz"
///
/// Only a command's *shape* is translated; what the user named is copied from what they said, accents and all.
public enum LangNormalize {
    private static let edge = CharacterSet(charactersIn: " \t\r\n¿¡?!.,;:")
    private static let wakeRx = Rx("^\\s*(?:hey\\s+|ok(?:ay)?\\s+)?saint\\b[\\s,.!:;-]*")!
    private static let placeholderRx = Rx("\\{(\\w+)(?::(\\w+))?\\}", caseInsensitive: false)!
    private static let clockRx = Rx("\\b(\\d{1,2})\\s+(y|menos|et|moins|e|meno|und|vor|nach)\\s+(\\w+)\\b", caseInsensitive: false)!
    private static let spaces = Rx("\\s+", caseInsensitive: false)!

    static func trimEdge(_ s: String) -> String { s.trimmingCharacters(in: edge) }

    public static func stripWake(_ text: String, pack: LangPack? = nil) -> String {
        var t = wakeRx.sub("", in: text)
        if let prefix = pack?.wakePrefix { t = prefix.sub("", in: t) }
        return t.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public static func clean(_ text: String, pack: LangPack? = nil) -> String {
        var t = trimEdge(stripWake(text, pack: pack))
        if let filler = pack?.filler { t = trimEdge(filler.sub("", in: t)) }
        if let tail = pack?.politeTail { t = trimEdge(tail.sub("", in: t)) }
        return t
    }

    // MARK: times

    static func numbers(_ t: String, pack: LangPack) -> String {
        guard !pack.numbers.isEmpty else { return t }
        let words = pack.numbers.keys.sorted { $0.count > $1.count }
        let alternation = words.map { NSRegularExpression.escapedPattern(for: $0) }.joined(separator: "|")
        guard let rx = Rx("\\b(" + alternation + ")\\b", caseInsensitive: false) else { return t }
        let ns = t as NSString
        let found = rx.regex.matches(in: t, options: [], range: NSRange(location: 0, length: ns.length))
        if found.isEmpty { return t }
        var out = ""
        var last = 0
        for m in found {
            out += ns.substring(with: NSRange(location: last, length: m.range.location - last))
            let word = ns.substring(with: m.range(at: 1))
            out += String(pack.numbers[word] ?? 0)
            last = m.range.location + m.range.length
        }
        return out + ns.substring(from: last)
    }

    /// "5 y media" -> "5:30", "5 menos cuarto" -> "4:45" (per the pack's clock words).
    static func clock(_ t: String, pack: LangPack) -> String {
        let c = pack.clock
        if c.isEmpty { return t }
        let ns = t as NSString
        let found = clockRx.regex.matches(in: t, options: [], range: NSRange(location: 0, length: ns.length))
        if found.isEmpty { return t }
        var out = ""
        var last = 0
        for m in found {
            out += ns.substring(with: NSRange(location: last, length: m.range.location - last))
            var hour = Int(ns.substring(with: m.range(at: 1))) ?? 0
            let joiner = ns.substring(with: m.range(at: 2))
            let what = ns.substring(with: m.range(at: 3))
            var replacement = ns.substring(with: m.range)
            if joiner == c["and"] || joiner == c["minus"] {
                var minutes: Int?
                if what == c["half"] { minutes = 30 }
                else if what == c["quarter"] { minutes = 15 }
                else if let n = Int(what) { minutes = n }
                if var mins = minutes {
                    if joiner == c["minus"] {
                        hour = (hour + 23) % 24
                        mins = 60 - mins
                    }
                    replacement = "\(hour):" + (mins < 10 ? "0\(mins)" : "\(mins)")
                }
            }
            out += replacement
            last = m.range.location + m.range.length
        }
        return out + ns.substring(from: last)
    }

    /// A spoken time or duration as the English phrase the reminder parser reads.
    public static func translateTime(_ phrase: String, pack: LangPack) -> String {
        var t = fold(phrase).trimmingCharacters(in: .whitespacesAndNewlines)
        t = numbers(t, pack: pack)
        t = clock(t, pack: pack)
        for (rx, replacement) in pack.timeRules { t = rx.sub(replacement, in: t) }
        return spaces.sub(" ", in: t).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: templates

    static func modify(_ value: String, how: String?, pack: LangPack) -> String {
        guard let how = how else { return value }
        let f = fold(value).trimmingCharacters(in: .whitespacesAndNewlines)
        switch how {
        case "time": return translateTime(value, pack: pack)
        case "app": return pack.apps[f] ?? value
        case "key": return pack.keys[f] ?? value
        case "unit": return pack.units[f] ?? value
        case "num": return pack.numbers[f].map { String($0) } ?? value
        default: return value
        }
    }

    static func groupText(_ match: NSTextCheckingResult, _ name: String, in text: NSString) -> String? {
        var range = NSRange(location: NSNotFound, length: 0)
        if let n = Int(name) {
            if n < match.numberOfRanges { range = match.range(at: n) }
        } else if (match.regularExpression?.pattern ?? "").contains("(?<\(name)>") {
            range = match.range(withName: name)        // asking for a name the pattern doesn't have would crash
        }
        if range.location == NSNotFound { return nil }
        return text.substring(with: range)
    }

    static func render(_ template: String, match: NSTextCheckingResult, original: String, pack: LangPack) -> String {
        let ns = original as NSString
        let tns = template as NSString
        let found = placeholderRx.regex.matches(in: template, options: [], range: NSRange(location: 0, length: tns.length))
        var out = ""
        var last = 0
        for m in found {
            out += tns.substring(with: NSRange(location: last, length: m.range.location - last))
            let name = tns.substring(with: m.range(at: 1))
            let how: String? = m.range(at: 2).location == NSNotFound ? nil : tns.substring(with: m.range(at: 2))
            if let raw = groupText(match, name, in: ns) {
                out += modify(trimEdge(raw), how: how, pack: pack)
            }
            last = m.range.location + m.range.length
        }
        out += tns.substring(from: last)
        return spaces.sub(" ", in: out).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: commands

    public static func toEnglish(_ text: String, languages: [String]) -> (english: String, language: String)? {
        for code in languages {
            guard let pack = LangPack.pack(code) else { continue }
            let original = clean(text, pack: pack)
            if original.isEmpty { continue }
            let folded = fold(original)
            for cmd in pack.commands {
                if let m = cmd.regex.match(folded) {
                    let english = render(cmd.to, match: m, original: original, pack: pack)
                    return english.isEmpty ? nil : (english, code)
                }
            }
            if let viaVerb = leadingVerb(original: original, folded: folded, pack: pack) {
                return (viaVerb, code)
            }
        }
        return nil
    }

    /// "pon some jazz" -> "play some jazz": only the first word is this language's command word.
    static func leadingVerb(original: String, folded: String, pack: LangPack) -> String? {
        let parts = folded.split(separator: " ", maxSplits: 1, omittingEmptySubsequences: true)
        guard let first = parts.first else { return nil }
        guard let verb = pack.verbs[trimEdge(String(first))] else { return nil }
        let originalParts = original.split(separator: " ", maxSplits: 1, omittingEmptySubsequences: true)
        let rest = originalParts.count > 1 ? String(originalParts[1]).trimmingCharacters(in: .whitespacesAndNewlines) : ""
        return "\(verb) \(rest)".trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

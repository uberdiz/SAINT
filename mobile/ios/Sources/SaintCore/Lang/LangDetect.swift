import Foundation

/// Which language is this, and is it one language or two? (modules/lang/detect.py)
///
/// Detection is per word, because people mix: "play la canción de Bad Bunny", "recuérdame to call mom at 5".
///   - a word in exactly one language's list is a vote for it
///   - a word several share ("no", "la", "a") is given to whichever candidate the clearer words point at
///   - a word in none is a name, a title, a number: no vote; a capital in mid-sentence is a name too
/// Letters only one language uses (ñ ¿ ¡, ã õ, ß) count extra, the speech recogniser's own guess is one more
/// vote, and text in another script is recognised by script. A sentence is *mixed* when a second language has
/// a real share of the votes.
public struct Detection {
    public var primary = "und"
    public var secondary = ""
    public var mixed = false
    public var confidence = 0.0
    public var counts: [String: Double] = [:]
    public var tags: [(word: String, lang: String)] = []
    public var candidates: [String] = []
}

public enum LangDetect {
    static let neutral: Set<String> = ["ok", "okay", "hmm", "hm", "uh", "um", "eh", "ah", "oh", "hey", "hi", "yeah", "wow"]
    static let mixedShare = 0.25
    static let wordRx = Rx("[^\\W\\d_]+(?:['’][^\\W\\d_]+)?", caseInsensitive: false)!

    private static let scripts: [(String, ClosedRange<UInt32>)] = [
        ("ja", 0x3040...0x30FF), ("ko", 0xAC00...0xD7AF), ("ko", 0x1100...0x11FF), ("zh", 0x4E00...0x9FFF),
        ("ru", 0x0400...0x04FF), ("ar", 0x0600...0x06FF), ("hi", 0x0900...0x097F), ("th", 0x0E00...0x0E7F),
        ("el", 0x0370...0x03FF), ("he", 0x0590...0x05FF),
    ]

    /// Every word with its range in ``text``.
    static func words(in text: String) -> [(String, NSRange)] {
        let ns = text as NSString
        return wordRx.regex.matches(in: text, options: [], range: NSRange(location: 0, length: ns.length))
            .map { (ns.substring(with: $0.range), $0.range) }
    }

    public static func detect(_ text: String, hint: String = "", prefer: [String] = [], sticky: String = "") -> Detection {
        var det = Detection()
        let found = words(in: text)
        let words = found.map { $0.0 }
        let letters = words.reduce(0) { $0 + $1.unicodeScalars.count }

        // ---- other scripts
        var scriptCounts: [String: Int] = [:]
        for s in text.unicodeScalars {
            for (lang, range) in scripts where range.contains(s.value) {
                scriptCounts[lang, default: 0] += 1
                break
            }
        }
        if !scriptCounts.isEmpty && letters > 0 {
            if scriptCounts["ja"] != nil { scriptCounts["zh"] = nil }
            if let (lang, n) = scriptCounts.max(by: { $0.value < $1.value }), Double(n) / Double(max(1, letters)) >= 0.3 {
                det.primary = lang
                det.confidence = 0.95
                det.counts = [lang: Double(n)]
                let latin = words.filter { $0.unicodeScalars.allSatisfy { $0.value < 0x250 } }
                let english = LangPack.englishWords()
                let eng = latin.filter { english.contains(fold($0)) }
                if !eng.isEmpty && Double(eng.count) / Double(max(1, words.count)) >= mixedShare {
                    det.secondary = "en"
                    det.mixed = true
                }
                det.tags = words.map { w in
                    (word: w, lang: w.unicodeScalars.contains { $0.value >= 0x250 } ? lang : "")
                }
                return det
            }
        }

        // ---- Latin-script languages
        var codes = ["en"]
        var vocab: [String: Set<String>] = ["en": LangPack.englishWords()]
        for code in LangPack.supported {
            if let pack = LangPack.pack(code) {
                codes.append(code)
                vocab[code] = pack.words
            }
        }
        var counts: [String: Double] = [:]
        var soft: [String: Double] = [:]
        for c in codes { counts[c] = 0; soft[c] = 0 }
        var tagged: [(word: String, cands: [String])] = []
        for (i, w) in words.enumerated() {
            if i > 0, let first = w.unicodeScalars.first, first.properties.isUppercase, w != w.uppercased() {
                tagged.append((w, []))                       // a capital mid-sentence: a name or a title
                continue
            }
            let f = fold(w.replacingOccurrences(of: "’", with: "'"))
            if neutral.contains(f) {
                tagged.append((w, []))
                continue
            }
            var cands = codes.filter { vocab[$0]?.contains(f) == true }
            if cands.isEmpty, f.contains("'") {
                let tail = String(f[f.index(after: f.firstIndex(of: "'")!)...])
                cands = codes.filter { vocab[$0]?.contains(tail) == true || vocab[$0]?.contains(f) == true }
            }
            tagged.append((w, cands))
            if cands.count == 1 { counts[cands[0], default: 0] += 1 }
            for c in cands { soft[c, default: 0] += 1.0 / Double(cands.count) }
        }

        let lowered = text.lowercased()
        for code in LangPack.supported {
            guard let pack = LangPack.pack(code), !pack.chars.isEmpty else { continue }
            var n = 0
            for c in pack.chars { n += lowered.filter { $0 == c }.count }
            counts[code, default: 0] += min(3.0, 1.5 * Double(n))
        }
        if counts[hint] != nil { counts[hint, default: 0] += 1.0 }

        var order: [String] = []
        for c in [hint, sticky] + prefer + ["en"] where counts[c] != nil { order.append(c) }
        func rank(_ c: String) -> Int { order.firstIndex(of: c) ?? 99 }
        func position(_ c: String) -> Int { codes.firstIndex(of: c) ?? 99 }

        func pick(_ cands: [String]) -> String {
            cands.max { a, b in
                let ka = (counts[a] ?? 0, -(order.contains(a) ? order.firstIndex(of: a)! : -99))
                let kb = (counts[b] ?? 0, -(order.contains(b) ? order.firstIndex(of: b)! : -99))
                return ka < kb
            }!
        }

        var tags: [(word: String, lang: String)] = []
        for (w, cands) in tagged {
            if cands.count == 1 {
                tags.append((w, cands[0]))
            } else if cands.count > 1 {
                let choice = pick(cands)
                if (counts[choice] ?? 0) > 0 { counts[choice, default: 0] += 0.5 }
                tags.append((w, choice))
            } else {
                tags.append((w, ""))
            }
        }
        det.tags = tags
        det.candidates = soft.filter { $0.value > 0 }
            .sorted { a, b in
                if a.value != b.value { return a.value > b.value }
                if rank(a.key) != rank(b.key) { return rank(a.key) < rank(b.key) }
                return position(a.key) < position(b.key)
            }.map { $0.key }

        let total = counts.values.reduce(0, +)
        det.counts = counts.filter { $0.value > 0 }
        if total <= 0 {
            det.primary = sticky.isEmpty ? "und" : sticky
            return det
        }
        let ranked = counts.sorted { a, b in
            if a.value != b.value { return a.value > b.value }
            if rank(a.key) != rank(b.key) { return rank(a.key) < rank(b.key) }
            return position(a.key) < position(b.key)
        }
        det.primary = ranked[0].key
        det.confidence = ranked[0].value / total
        if ranked.count > 1 {
            let (second, n2) = (ranked[1].key, ranked[1].value)
            if n2 >= 1.0 && n2 / total >= mixedShare {
                det.secondary = second
                det.mixed = true
            }
        }
        return det
    }
}

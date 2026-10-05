import Foundation

/// Finding "SAINT" in a live transcript, like "Hey Siri" but with our own word.
///
/// Speech recognisers write what they think they heard, and "SAINT" is an ordinary word in some languages and a
/// near-miss in others (sein, seint, saint). So the match is a list of spellings, per language, plus whatever
/// the user adds. Only the text *after* the wake word is the command.
public enum WakeWord {
    /// "sain" (French for healthy), "sant" (Catalan/Italian place names) and German "sein" used to be here too:
    /// everyday words, so ordinary speech woke SAINT and became a command (2026-10-01).
    public static let base: Set<String> = ["saint", "saints", "saynt", "sainte", "seint", "seynt"]
    /// How Apple's English recogniser often writes a spoken "SAINT" ("St." as in St. Louis, "sane", "cent"…). They
    /// only count where a wake word is expected — the very start of what was said, or right after "hey"/"okay" —
    /// never after a mere pause, so they don't make ordinary speech wake SAINT. Without them "Jarvis" (which the
    /// recogniser always spells right) worked while "SAINT" mostly didn't (2026-10-02).
    public static let atStart: Set<String> = ["st", "sane", "saine", "sayin", "sint", "sayint", "sayent", "saintt",
                                              "cent", "scent"]
    /// Ordinary words that only count straight after "hey" / "okay": "Hey, sent…", "OK same…".
    public static let afterGreeting: Set<String> = ["sent", "same", "saying", "sank", "stay"]
    /// Spellings the recogniser tends to produce for "saint" in other languages' models.
    public static let perLanguage: [String: Set<String>] = [
        "es": ["sein", "seinte", "seint"],
        "pt": ["seint", "sein", "seinti"],
        "de": ["seint"],
        "it": ["seint", "sein"],
    ]
    /// "Saint Louis", "Saint Patrick's Day": a name, not SAINT being called.
    public static let nameFollowers: Set<String> = [
        "louis", "patrick", "patrick's", "patricks", "paul", "pauls", "peter", "petersburg", "john", "johns", "george",
        "lucia", "kitts", "tropez", "laurent", "martin", "nicholas", "valentine", "valentine's", "valentines", "anthony",
        "francis", "jude", "mary", "mary's", "marys", "helena", "moritz", "michael", "andrews", "andrew", "augustine",
        "denis", "etienne", "germain", "barth", "barts", "thomas", "vincent", "joseph", "lawrence", "clair", "cloud",
    ]

    public struct Match: Equatable {
        public var command: String          // what was said after the wake word ("" if nothing yet)
        public var wakeEnd: Int             // UTF-16 offset in the transcript just past the wake word
    }

    private static let tokens = Rx(#"[\p{L}\p{M}]+(?:['’][\p{L}\p{M}]+)?"#, caseInsensitive: false)!
    private static let edge = CharacterSet(charactersIn: " \t\r\n.,;:!?¿¡-—–\"“”'’")

    public static func variants(languages: [String] = [], extra: [String] = []) -> Set<String> {
        var all = base
        for code in languages { all.formUnion(perLanguage[code] ?? []) }
        for word in extra { all.insert(fold(word.trimmed)) }
        return all
    }

    /// Words that may come before the wake word without it being part of a sentence: "hey SAINT", "ok SAINT".
    /// The fillers that are a greeting to SAINT ("hey SAINT"), after which a looser spelling is trusted.
    static let greetings: Set<String> = ["hey", "hi", "hello", "ok", "okay", "yo", "oye", "hola", "ey"]

    public static let fillers: Set<String> = ["hey", "hi", "hello", "ok", "okay", "yo", "oye", "hola", "ey", "eh", "ola", "ei",
                                              "salut", "ciao", "hallo", "um", "uh", "so", "and", "y", "e"]

    /// The first wake word in ``transcript`` and the command after it.
    ///
    /// The word only counts when it starts what was said (possibly after a "hey"), or when ``pauseBefore`` says
    /// there was a pause before it — so "play Saint Louis Blues" or "my friend Saint called" don't wake anything.
    /// ``pauseBefore`` is given the index of the word in the transcript (0 = first).
    public static func find(in transcript: String, variants: Set<String>? = nil,
                            pauseBefore: ((Int) -> Bool)? = nil) -> Match? {
        let accepted = variants ?? WakeWord.base
        let ns = transcript as NSString
        let found = tokens.regex.matches(in: transcript, options: [], range: NSRange(location: 0, length: ns.length))
        var words: [String] = []
        for m in found { words.append(fold(ns.substring(with: m.range).replacingOccurrences(of: "’", with: "'"))) }
        for (i, m) in found.enumerated() {
            let leading = words[0..<i].allSatisfy { fillers.contains($0) }
            let greeted = i > 0 && leading && greetings.contains(words[i - 1])
            if !accepted.contains(words[i]) {
                let near = (leading && atStart.contains(words[i])) || (greeted && afterGreeting.contains(words[i]))
                if !near { continue }
                // "1st" is a number, not "St."
                if words[i] == "st", m.range.location > 0,
                   let prev = Unicode.Scalar(ns.character(at: m.range.location - 1)),
                   CharacterSet.decimalDigits.contains(prev) { continue }
            }
            if !(i == 0 || leading || (pauseBefore?(i) ?? false)) { continue }
            if i + 1 < words.count && nameFollowers.contains(words[i + 1]) && !(pauseBefore?(i + 1) ?? false) { continue }
            let end = m.range.location + m.range.length
            return Match(command: ns.substring(from: end).trimmingCharacters(in: edge), wakeEnd: end)
        }
        return nil
    }

    /// The command that follows the wake word whose end we already know, as the transcript grows.
    public static func command(in transcript: String, after wakeEnd: Int) -> String {
        let ns = transcript as NSString
        if wakeEnd >= ns.length { return "" }
        return ns.substring(from: wakeEnd).trimmingCharacters(in: edge)
    }

    /// Of several recognisers' versions of one utterance (say, an English one and a Spanish one), the one most likely
    /// to be right: the recogniser's own confidence counts most, then whether the text is in the language the
    /// recogniser was listening for, then length.
    public static func best(_ candidates: [(language: String, text: String, confidence: Double)])
        -> (language: String, text: String)? {
        var winner: (language: String, text: String)?
        var winnerScore = -1.0
        for c in candidates {
            let text = c.text.trimmed
            if text.isEmpty { continue }
            let det = LangDetect.detect(text, hint: c.language)
            var score = c.confidence * 3 + Double(text.split(separator: " ").count) * 0.05
            if det.primary == c.language { score += 1.0 }
            if score > winnerScore {
                winnerScore = score
                winner = (c.language, text)
            }
        }
        return winner
    }
}

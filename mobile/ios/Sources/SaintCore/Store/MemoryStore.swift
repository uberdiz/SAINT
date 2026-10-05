import Foundation

/// One thing SAINT knows about you ("my favorite color is green", "Gian is my brother").
/// Mirrors the desktop's memory entries so the two devices can swap them.
public struct MemoryEntry: Codable, Equatable, Identifiable {
    public var id: String
    public var content: String
    public var key: String
    public var value: String
    public var category: String
    public var how: String                 // "told" or "learned"
    public var tags: [String]
    public var confidence: Double
    public var created: Date

    public init(id: String = Ids.make(16), content: String, key: String = "", value: String = "",
                category: String = "fact", how: String = "told", tags: [String]? = nil,
                confidence: Double = 1.0, created: Date = Date()) {
        self.id = id
        self.content = content
        self.key = key
        self.value = value
        self.category = category
        self.how = how
        self.tags = tags ?? [category]
        self.confidence = confidence
        self.created = created
    }
}

public final class MemoryStore: SyncAdapter {
    public let kind = "memory"
    private let file: JSONFile<[MemoryEntry]>

    public init(directory: URL?) {
        file = JSONFile(url: directory?.appendingPathComponent("memory.json"), empty: [])
    }

    public func all() -> [MemoryEntry] {
        file.read { (list: [MemoryEntry]) -> [MemoryEntry] in list.sorted { $0.created > $1.created } }
    }

    public var count: Int { file.read { (list: [MemoryEntry]) -> Int in list.count } }

    /// Remember something. When ``key`` is given and already known, that entry is updated instead of duplicated.
    @discardableResult
    public func remember(content: String, key: String = "", value: String = "", category: String = "fact",
                         how: String = "told") -> MemoryEntry {
        let k = key.trimmed
        return file.write { (list: inout [MemoryEntry]) -> MemoryEntry in
            if !k.isEmpty, let i = list.firstIndex(where: { fold($0.key) == fold(k) }) {
                list[i].content = content
                list[i].value = value
                list[i].category = category
                list[i].how = how
                return list[i]
            }
            let entry = MemoryEntry(content: content, key: k, value: value, category: category, how: how)
            list.append(entry)
            return entry
        }
    }

    @discardableResult
    public func forget(id: String) -> Bool {
        file.write { (list: inout [MemoryEntry]) -> Bool in
            let before = list.count
            list.removeAll { $0.id == id }
            return list.count != before
        }
    }

    /// Forget everything that matches ``query`` well. Returns what was forgotten.
    @discardableResult
    public func forget(matching query: String) -> [MemoryEntry] {
        let hits = search(query, limit: 3).filter { MemoryStore.score($0, words: MemoryStore.keywords(query)) >= 2 }
        for h in hits { forget(id: h.id) }
        return hits
    }

    // MARK: searching

    static let stop: Set<String> = [
        "the", "a", "an", "my", "your", "is", "are", "was", "what", "whats", "do", "does", "i", "me", "id", "im",
        "can", "could", "would", "should", "please", "some", "any", "of", "to", "that", "this", "and", "or", "for",
        "you", "know", "remember", "about", "tell", "which", "who", "where", "when", "how", "did", "say", "said",
        "it", "in", "on", "at", "be", "s",
    ]

    static func keywords(_ text: String) -> [String] {
        let folded = fold(text)
        let parts = folded.split(whereSeparator: { !$0.isLetter && !$0.isNumber })
        return parts.map { String($0) }.filter { $0.count > 1 && !stop.contains($0) }
    }

    static func score(_ entry: MemoryEntry, words: [String]) -> Double {
        let all = Set(keywords(entry.key + " " + entry.content + " " + entry.value))
        let keyWords = Set(keywords(entry.key))
        var score = 0.0
        for w in words {
            if all.contains(w) { score += 1 }
            if keyWords.contains(w) { score += 1 }
        }
        return score
    }

    public func search(_ query: String, limit: Int = 5) -> [MemoryEntry] {
        let words = MemoryStore.keywords(query)
        if words.isEmpty { return [] }
        var scored: [(score: Double, entry: MemoryEntry)] = []
        for e in all() {
            let s = MemoryStore.score(e, words: words)
            if s > 0 { scored.append((score: s, entry: e)) }
        }
        scored.sort { $0.score > $1.score }
        return scored.prefix(limit).map { $0.entry }
    }

    // MARK: SyncAdapter

    public func snapshot() -> [String: JSONObject] {
        var out: [String: JSONObject] = [:]
        for e in all() {
            out[e.id] = ["content": e.content, "key": e.key, "value": e.value, "category": e.category, "how": e.how,
                         "tags": e.tags, "confidence": (e.confidence * 100).rounded() / 100]
        }
        return out
    }

    public func fingerprint(_ data: JSONObject) -> String {
        var d = data
        d["confidence"] = nil                   // it drifts as SAINT hears things again; not an edit
        return SyncEngine.fingerprint(d)
    }

    public func apply(changes: [(uid: String, data: JSONObject?)]) -> [String] {
        file.write { (list: inout [MemoryEntry]) -> [String] in
            for change in changes {
                let index = list.firstIndex(where: { $0.id == change.uid })
                guard let data = change.data else {
                    if let i = index { list.remove(at: i) }
                    continue
                }
                let category = (data["category"] as? String) ?? "fact"
                var entry = index.map { list[$0] } ?? MemoryEntry(id: change.uid, content: "")
                entry.content = (data["content"] as? String) ?? entry.content
                entry.key = (data["key"] as? String) ?? ""
                entry.value = (data["value"] as? String) ?? ""
                entry.category = category
                entry.how = (data["how"] as? String) ?? "told"
                entry.tags = (data["tags"] as? [String]) ?? [category]
                entry.confidence = (data["confidence"] as? NSNumber)?.doubleValue ?? 1.0
                if let i = index { list[i] = entry } else { list.append(entry) }
            }
            return []
        }
    }
}

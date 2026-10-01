import Foundation

// What SAINT has been taught: skills ("when I say X, do these steps"), aliases ("the lab means ...")
// and scenes (named routines). Each syncs with the desktop's matching store.

// MARK: skills

public struct Skill: Codable, Equatable, Identifiable {
    public var id: String
    public var phrase: String               // normalised: what you say
    public var steps: [String]              // commands, in order
    public var how: String                  // "shown", "planned", "edited", "told"
    public var said: String                 // the request as first said
    public var created: Date
    public var uses: Int

    public init(id: String = Ids.make(8), phrase: String, steps: [String], how: String = "told", said: String = "",
                created: Date = Date(), uses: Int = 0) {
        self.id = id
        self.phrase = phrase
        self.steps = steps
        self.how = how
        self.said = said
        self.created = created
        self.uses = uses
    }
}

public final class SkillStore: SyncAdapter {
    public let kind = "skill"
    private let file: JSONFile<[Skill]>

    public init(directory: URL?) {
        file = JSONFile(url: directory?.appendingPathComponent("skills.json"), empty: [])
    }

    static func normalize(_ text: String) -> String {
        let words = fold(text).split(whereSeparator: { !$0.isLetter && !$0.isNumber && $0 != "'" }).map { String($0) }
        return words.joined(separator: " ")
    }

    public func all() -> [Skill] {
        file.read { (list: [Skill]) -> [Skill] in list.sorted { $0.created > $1.created } }
    }

    /// The skill whose phrase is exactly what was said.
    public func match(_ spoken: String) -> Skill? {
        let key = SkillStore.normalize(spoken)
        if key.isEmpty { return nil }
        return file.read { (list: [Skill]) -> Skill? in list.first { $0.phrase == key } }
    }

    @discardableResult
    public func learn(phrase: String, steps: [String], how: String = "told") -> Skill? {
        let key = SkillStore.normalize(phrase)
        let cleaned = steps.map { $0.trimmed }.filter { !$0.isEmpty }
        if key.count < 3 || cleaned.isEmpty { return nil }
        if cleaned.map({ SkillStore.normalize($0) }) == [key] { return nil }   // "play lofi" -> "play lofi" teaches nothing
        return file.write { (list: inout [Skill]) -> Skill in
            if let i = list.firstIndex(where: { $0.phrase == key }) {
                list[i].steps = cleaned
                list[i].how = how
                return list[i]
            }
            let skill = Skill(phrase: key, steps: cleaned, how: how, said: String(phrase.trimmed.prefix(200)))
            list.append(skill)
            return skill
        }
    }

    public func noteUse(id: String) {
        file.write(announce: false) { (list: inout [Skill]) -> Void in
            if let i = list.firstIndex(where: { $0.id == id }) { list[i].uses += 1 }
        }
    }

    @discardableResult
    public func forget(id: String) -> Bool {
        file.write { (list: inout [Skill]) -> Bool in
            let before = list.count
            list.removeAll { $0.id == id }
            return list.count != before
        }
    }

    public func snapshot() -> [String: JSONObject] {
        var out: [String: JSONObject] = [:]
        for s in all() { out[s.id] = ["phrase": s.phrase, "steps": s.steps, "how": s.how, "said": s.said] }
        return out
    }

    public func apply(changes: [(uid: String, data: JSONObject?)]) -> [String] {
        file.write { (list: inout [Skill]) -> [String] in
            var refused: [String] = []
            for change in changes {
                guard let data = change.data else {
                    list.removeAll { $0.id == change.uid }
                    continue
                }
                let phrase = (data["phrase"] as? String) ?? ""
                let steps = (data["steps"] as? [String]) ?? []
                if phrase.isEmpty || steps.isEmpty { refused.append(change.uid); continue }
                if let clash = list.firstIndex(where: { $0.phrase == phrase && $0.id != change.uid }) {
                    if list[clash].id < change.uid { refused.append(change.uid); continue }   // same phrase learned twice: the smaller id wins everywhere
                    list.remove(at: clash)
                }
                if let i = list.firstIndex(where: { $0.id == change.uid }) {
                    list[i].phrase = phrase
                    list[i].steps = steps
                    list[i].how = (data["how"] as? String) ?? list[i].how
                    list[i].said = (data["said"] as? String) ?? list[i].said
                } else {
                    list.append(Skill(id: change.uid, phrase: phrase, steps: steps, how: (data["how"] as? String) ?? "shown",
                                      said: (data["said"] as? String) ?? ""))
                }
            }
            return refused
        }
    }
}

// MARK: aliases

public final class AliasStore: SyncAdapter {
    public let kind = "alias"
    private let file: JSONFile<[String: String]>

    public init(directory: URL?) {
        file = JSONFile(url: directory?.appendingPathComponent("aliases.json"), empty: [:])
    }

    static func key(_ text: String) -> String { SkillStore.normalize(text) }

    public func all() -> [String: String] { file.read { (d: [String: String]) -> [String: String] in d } }

    public func set(_ name: String, to target: String) {
        let k = AliasStore.key(name)
        if k.isEmpty || target.trimmed.isEmpty { return }
        file.write { (d: inout [String: String]) -> Void in d[k] = target.trimmed }
    }

    @discardableResult
    public func forget(_ name: String) -> Bool {
        let k = AliasStore.key(name)
        return file.write { (d: inout [String: String]) -> Bool in d.removeValue(forKey: k) != nil }
    }

    public func resolve(_ name: String) -> String? {
        let k = AliasStore.key(name)
        return file.read { (d: [String: String]) -> String? in d[k] }
    }

    public func snapshot() -> [String: JSONObject] {
        var out: [String: JSONObject] = [:]
        for (k, v) in all() { out[k] = ["target": v] }
        return out
    }

    public func apply(changes: [(uid: String, data: JSONObject?)]) -> [String] {
        file.write { (d: inout [String: String]) -> [String] in
            for change in changes {
                if let data = change.data, let target = data["target"] as? String, !target.isEmpty {
                    d[change.uid] = target
                } else {
                    d.removeValue(forKey: change.uid)
                }
            }
            return []
        }
    }
}

// MARK: scenes

public struct Routine: Codable, Equatable, Identifiable {
    public var id: String
    public var name: String
    public var steps: [String]
    public var phrase: String               // an extra voice trigger, e.g. "movie time"

    public init(id: String = Ids.make(8), name: String, steps: [String], phrase: String = "") {
        self.id = id
        self.name = name
        self.steps = steps
        self.phrase = phrase
    }
}

public final class SceneStore: SyncAdapter {
    public let kind = "scene"
    private let file: JSONFile<[Routine]>

    public init(directory: URL?) {
        file = JSONFile(url: directory?.appendingPathComponent("scenes.json"), empty: [])
    }

    static func norm(_ text: String) -> String { SkillStore.normalize(text) }

    public func all() -> [Routine] {
        file.read { (list: [Routine]) -> [Routine] in list.sorted { $0.name.lowercased() < $1.name.lowercased() } }
    }

    /// "movie time", "the gaming scene", "gaming".
    public func find(_ spoken: String) -> Routine? {
        var key = SceneStore.norm(spoken)
        for prefix in ["the ", "my "] where key.hasPrefix(prefix) { key = String(key.dropFirst(prefix.count)) }
        for suffix in [" scene", " mode", " routine"] where key.hasSuffix(suffix) { key = String(key.dropLast(suffix.count)) }
        if key.isEmpty { return nil }
        return file.read { (list: [Routine]) -> Routine? in
            list.first { scene in
                let name = SceneStore.norm(scene.name)
                let stripped = name.replacingOccurrences(of: " mode", with: "").replacingOccurrences(of: " scene", with: "")
                return name == key || stripped == key || (!scene.phrase.isEmpty && SceneStore.norm(scene.phrase) == key)
            }
        }
    }

    public func snapshot() -> [String: JSONObject] {
        var out: [String: JSONObject] = [:]
        for s in all() { out[s.id] = ["name": s.name, "steps": s.steps, "phrase": s.phrase] }
        return out
    }

    public func apply(changes: [(uid: String, data: JSONObject?)]) -> [String] {
        file.write { (list: inout [Routine]) -> [String] in
            var refused: [String] = []
            for change in changes {
                guard let data = change.data else {
                    list.removeAll { $0.id == change.uid }
                    continue
                }
                var scene = Routine(id: change.uid, name: (data["name"] as? String) ?? "", steps: (data["steps"] as? [String]) ?? [],
                                  phrase: (data["phrase"] as? String) ?? "")
                if scene.name.trimmed.isEmpty || scene.steps.isEmpty { refused.append(change.uid); continue }
                if let twin = list.firstIndex(where: { SceneStore.norm($0.name) == SceneStore.norm(scene.name) && $0.id != change.uid }) {
                    let same = list[twin].steps == scene.steps && list[twin].phrase == scene.phrase
                    if same {
                        if list[twin].id < change.uid { refused.append(change.uid); continue }
                        list.remove(at: twin)
                    } else {
                        scene.name += " (synced)"             // two different scenes with one name: keep both
                    }
                }
                if let i = list.firstIndex(where: { $0.id == change.uid }) { list[i] = scene } else { list.append(scene) }
            }
            return refused
        }
    }
}

// MARK: language settings

/// The language preferences that follow you from device to device.
public final class SettingsStore: SyncAdapter {
    public let kind = "settings"
    private let file: JSONFile<StoredLanguage>

    struct StoredLanguage: Codable {
        var preferred: [String]
        var mixedMode: String
        var replyInUserLanguage: Bool
    }

    public init(directory: URL?) {
        file = JSONFile(url: directory?.appendingPathComponent("settings.json"),
                        empty: StoredLanguage(preferred: [], mixedMode: "mirror", replyInUserLanguage: true))
    }

    public var preferred: [String] { file.read { (s: StoredLanguage) -> [String] in s.preferred } }
    public var mixedMode: String { file.read { (s: StoredLanguage) -> String in s.mixedMode } }
    public var replyInUserLanguage: Bool { file.read { (s: StoredLanguage) -> Bool in s.replyInUserLanguage } }

    public func update(preferred: [String]? = nil, mixedMode: String? = nil, replyInUserLanguage: Bool? = nil) {
        file.write { (s: inout StoredLanguage) -> Void in
            if let p = preferred { s.preferred = p }
            if let m = mixedMode { s.mixedMode = m }
            if let r = replyInUserLanguage { s.replyInUserLanguage = r }
        }
    }

    public func apply(to settings: inout LangSettings) {
        settings.preferred = preferred
        settings.mixedMode = mixedMode
        settings.replyInUserLanguage = replyInUserLanguage
    }

    public func snapshot() -> [String: JSONObject] {
        let s = file.read { (s: StoredLanguage) -> StoredLanguage in s }
        return ["language": ["preferred": s.preferred, "mixed_mode": s.mixedMode, "reply_in_user_language": s.replyInUserLanguage]]
    }

    public func apply(changes: [(uid: String, data: JSONObject?)]) -> [String] {
        for change in changes where change.uid == "language" {
            guard let data = change.data else { continue }
            update(preferred: data["preferred"] as? [String], mixedMode: data["mixed_mode"] as? String,
                   replyInUserLanguage: data["reply_in_user_language"] as? Bool)
        }
        return []
    }
}

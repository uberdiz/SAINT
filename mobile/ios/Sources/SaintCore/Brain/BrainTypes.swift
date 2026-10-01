import Foundation

// What the on-device brain needs from the outside world. The app supplies the real things (Spotify, the link to
// your PC, the language model); tests supply fakes. SaintCore never touches the network or the audio system.

public enum MusicIntent: Equatable {
    case play(String)                  // a song, artist or anything Spotify can search
    case playAlbum(String)
    case playPlaylist(String)
    case playLike(String)              // "play something like ..."
    case playGenre(String)             // "play some jazz"
    case playSomethingILike
    case pause, resume, skip, previous, restart
    case volumeUp, volumeDown
    case volume(Int)
    case nowPlaying
    case like
    case shuffle(Bool)
}

public protocol MusicService: AnyObject {
    /// Do it and answer in SAINT's English phrases ("Paused.", "Playing X by Y.", "Spotify isn't connected.").
    func perform(_ intent: MusicIntent) async -> String
}

public struct PeerInfo: Equatable, Identifiable {
    public var id: String
    public var name: String
    public var role: String            // "own" or "collaborator"
    public var platform: String
    public var nicknames: [String]
    public var online: Bool

    public init(id: String, name: String, role: String, platform: String = "", nicknames: [String] = [], online: Bool = false) {
        self.id = id
        self.name = name
        self.role = role
        self.platform = platform
        self.nicknames = nicknames
        self.online = online
    }

    public var isOwn: Bool { role == "own" }
}

public struct AskAnswer {
    public var text: String
    public var expectsReply: Bool
    public var language: String
    public init(text: String, expectsReply: Bool = false, language: String = "") {
        self.text = text
        self.expectsReply = expectsReply
        self.language = language
    }
}

/// The link to your PC and your friends' SAINTs.
public protocol PCBridge: AnyObject {
    var peers: [PeerInfo] { get }
    /// Say something to that SAINT as if typed there; its answer comes back already in the user's language.
    func ask(peerID: String, text: String, language: String) async throws -> AskAnswer
    /// One of the closed list of remote automations (send_prompt, message, open_url, run_scene, play_music, ask).
    func runAutomation(peerID: String, name: String, args: JSONObject) async throws -> String
    func syncNow() async -> Int
}

public protocol LanguageModel: AnyObject {
    func respond(system: String, prompt: String) async throws -> String
}

public struct BrainReply {
    public var text: String            // what to show and say, in the user's language
    public var english: String         // the English it was written in (empty when it came from the PC)
    public var language: String        // the language it is in
    public var secondary: String = ""
    public var mixed = false
    public var expectsReply = false    // keep listening for the answer without the wake word
    public var ok = true
    public var stopSpeaking = false
    public var source = "phone"        // "phone", "pc", "model"

    public init(text: String, english: String = "", language: String = "en") {
        self.text = text
        self.english = english
        self.language = language
    }
}

/// What you were just doing on your other devices (modules/link/context_feed.py); kept in memory only.
public final class ContextFeed {
    public struct Item: Equatable {
        public var source: String
        public var user: String
        public var reply: String
        public var ts: Date
    }

    public static let maxAge: TimeInterval = 30 * 60
    public static let keep = 20
    private var items: [Item] = []
    private let lock = NSLock()

    public init() {}

    public func add(source: String, user: String, reply: String = "", ts: Date = Date()) {
        lock.lock()
        defer { lock.unlock() }
        items.append(Item(source: String((source.isEmpty ? "another device" : source).prefix(40)),
                          user: String(user.prefix(240)), reply: String(reply.prefix(240)), ts: ts))
        if items.count > ContextFeed.keep { items.removeFirst(items.count - ContextFeed.keep) }
    }

    public func recent(now: Date = Date()) -> [Item] {
        lock.lock()
        defer { lock.unlock() }
        return items.filter { now.timeIntervalSince($0.ts) <= ContextFeed.maxAge }
    }

    public func describe(now: Date = Date()) -> String {
        var lines: [String] = []
        for i in recent(now: now).reversed() {
            let mins = max(0, Int(now.timeIntervalSince(i.ts) / 60))
            let when = mins < 1 ? "just now" : "\(mins) min ago"
            let said = i.reply.isEmpty ? "" : " and SAINT answered “\(i.reply)”"
            lines.append("- \(when), on \(i.source): the user said “\(i.user)”\(said)")
        }
        return lines.joined(separator: "\n")
    }

    public func clear() {
        lock.lock()
        defer { lock.unlock() }
        items = []
    }
}

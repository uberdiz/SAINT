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
    case playLiked                     // "play my liked songs"
    case playRecent                    // "play what I've been listening to today"
    case recentSummary                 // "what did I listen to today?"
    case topTrack                      // "what's the song I've played the most lately?"
    case queue(String)                 // "add Levitating to the queue"
    case addToPlaylist(String)         // "add this to my workout playlist"
    case recommend                     // "what should I listen to?"
    case transfer(String)              // "play it on my speaker" / "switch Spotify to my PC"
    case seek(Int)                     // "skip ahead 30 seconds" (negative: back)
}

public protocol MusicService: AnyObject {
    /// Do it and answer in SAINT's English phrases ("Paused.", "Playing X by Y.", "Spotify isn't connected.").
    func perform(_ intent: MusicIntent) async -> String
}

/// The phrases a MusicService answers with when it couldn't do it (they used to be logged as done).
public func musicFailed(_ text: String) -> Bool {
    let t = text.lowercased()
    return ["isn't connected", "open spotify on a device", "spotify said no", "spotify is busy",
            "couldn't reach spotify", "didn't find anything", "isn't playing anything", "nothing found"]
        .contains { t.contains($0) }
}

/// Things the phone itself can do. Anything iOS doesn't let an app do directly goes through a Shortcut the user
/// made (``shortcut``/``system``), and sending a text or placing a call is always confirmed by the user in
/// Apple's own sheet.
public enum PhoneIntent: Equatable {
    case takePhoto(selfie: Bool)
    case openCamera
    case recordVideo
    case flashlight(Bool?)             // nil: toggle
    case brightness(Int)               // percent
    case brightnessStep(up: Bool)
    case call(String)                  // who (a contact name or a number)
    case facetime(String)
    case text(String)                  // "mom I'm on my way" — the app splits recipient and message by contact names
    case openApp(String)
    case navigate(String)
    case battery
    case system(String, Bool?)         // "low power mode", "wi-fi", "bluetooth", "do not disturb", "airplane mode", "dark mode"
    case shortcut(String)
    case openSettings
    case webSearch(String)
}

public struct PhoneResult: Equatable {
    public var text: String
    public var ok: Bool
    public init(_ text: String, ok: Bool = true) {
        self.text = text
        self.ok = ok
    }
}

public protocol PhoneService: AnyObject {
    /// Do it and answer in SAINT's English phrases, or nil when this phone can't (it's then tried on your PC).
    func perform(_ intent: PhoneIntent) async -> PhoneResult?
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
    /// Can that device be reached right now (dialling it if the link is down)?
    func reachable(peerID: String) async -> Bool
}

public extension PCBridge {
    func reachable(peerID: String) async -> Bool { false }
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

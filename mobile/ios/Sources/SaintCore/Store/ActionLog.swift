import Foundation

/// Everything SAINT did on this phone: what you asked, what it did, whether it worked, and where it ran.
/// Shown on the Activity tab and synced to your PC (kind "actionlog"), which adds it to its History.
public struct ActionEntry: Codable, Equatable, Identifiable {
    public var id: String
    public var ts: Date
    public var request: String           // what you said or typed
    public var action: String            // what SAINT did, in a few words ("Played Blinding Lights + 19 more")
    public var kind: String              // music, phone, reminder, memory, pc, chat, voice, system
    public var status: String            // done, failed, queued, sent (ran on the PC), info
    public var source: String            // phone, pc, model
    public var device: String            // this phone's name

    public init(id: String = Ids.make(12), ts: Date = Date(), request: String, action: String, kind: String,
                status: String, source: String = "phone", device: String = "") {
        self.id = id
        self.ts = ts
        self.request = String(request.prefix(300))
        self.action = String(action.prefix(300))
        self.kind = kind
        self.status = status
        self.source = source
        self.device = device
    }
}

public final class ActionLog: SyncAdapter {
    public let kind = "actionlog"
    /// Kept for 30 days, at most this many entries.
    public static let keep = 2000
    public static let maxAge: TimeInterval = 30 * 86400
    private let file: JSONFile<[ActionEntry]>

    public init(directory: URL?) {
        file = JSONFile(url: directory?.appendingPathComponent("actionlog.json"), empty: [])
    }

    public func all() -> [ActionEntry] {
        file.read { (d: [ActionEntry]) -> [ActionEntry] in d.sorted { $0.ts > $1.ts } }
    }

    @discardableResult
    public func add(_ entry: ActionEntry) -> ActionEntry {
        file.write { (d: inout [ActionEntry]) -> Void in
            d.append(entry)
            ActionLog.prune(&d)
        }
        return entry
    }

    public func clear() {
        file.write { (d: inout [ActionEntry]) -> Void in d.removeAll() }
    }

    static func prune(_ d: inout [ActionEntry], now: Date = Date()) {
        d.removeAll { now.timeIntervalSince($0.ts) > maxAge }
        if d.count > keep {
            d.sort { $0.ts < $1.ts }
            d.removeFirst(d.count - keep)
        }
    }

    // MARK: sync

    public func snapshot() -> [String: JSONObject] {
        var out: [String: JSONObject] = [:]
        for e in all() {
            out[e.id] = ["ts": e.ts.timeIntervalSince1970, "request": e.request, "action": e.action, "kind": e.kind,
                         "status": e.status, "source": e.source, "device": e.device]
        }
        return out
    }

    public func apply(changes: [(uid: String, data: JSONObject?)]) -> [String] {
        file.write { (d: inout [ActionEntry]) -> [String] in
            for change in changes {
                d.removeAll { $0.id == change.uid }
                guard let data = change.data else { continue }
                let ts = (data["ts"] as? Double).map { Date(timeIntervalSince1970: $0) } ?? Date()
                d.append(ActionEntry(id: change.uid, ts: ts, request: data["request"] as? String ?? "",
                                     action: data["action"] as? String ?? "", kind: data["kind"] as? String ?? "",
                                     status: data["status"] as? String ?? "info", source: data["source"] as? String ?? "phone",
                                     device: data["device"] as? String ?? ""))
            }
            ActionLog.prune(&d)
            return []
        }
    }
}

import Foundation

/// What one device learns, the others know (see modules/link/sync.py).
///
/// Every synced item has a stable uid; the mirror remembers the content hash, timestamp and origin device we
/// last knew, so a changed hash is a local edit and a missing item is a deletion (kept as a tombstone) —
/// no store has to announce its changes. Timestamps are a hybrid logical clock, so a wrong phone clock can't
/// overwrite everything; merging is last-writer-wins per item, ties broken on the device id. Applying a remote
/// item records its hash *as stored locally*, so it isn't echoed back as a local edit.
public protocol SyncAdapter: AnyObject {
    var kind: String { get }
    /// uid -> data for everything that exists locally now.
    func snapshot() -> [String: JSONObject]
    /// Write remote changes ((uid, data) or (uid, nil) to delete). Returns the uids that were refused.
    func apply(changes: [(uid: String, data: JSONObject?)]) -> [String]
    /// What counts as a change (default: any field).
    func fingerprint(_ data: JSONObject) -> String
}

public extension SyncAdapter {
    func fingerprint(_ data: JSONObject) -> String { SyncEngine.fingerprint(data) }
}

public struct MirrorItem: Codable, Equatable {
    public var kind: String
    public var uid: String
    public var ts: Int64
    public var origin: String
    public var hash: String
    public var deleted: Bool
    public var data: String?          // canonical JSON of what we'd serve
}

/// The wire form of an item: {"k": kind, "u": uid, "ts": ms, "o": origin, "x": deleted, "d": data}
public struct SyncItem {
    public var kind: String
    public var uid: String
    public var ts: Int64
    public var origin: String
    public var deleted: Bool
    public var data: JSONObject?

    public init(kind: String, uid: String, ts: Int64, origin: String, deleted: Bool, data: JSONObject?) {
        self.kind = kind; self.uid = uid; self.ts = ts; self.origin = origin; self.deleted = deleted; self.data = data
    }

    public func toJSON() -> JSONObject {
        var o: JSONObject = ["k": kind, "u": uid, "ts": ts, "o": origin, "x": deleted]
        if let data = data { o["d"] = data } else { o["d"] = NSNull() }
        return o
    }

    public static func from(json: Any) -> SyncItem? {
        guard let o = json as? JSONObject, let kind = o["k"] as? String, let uid = (o["u"] as? String) ?? (o["u"] as? NSNumber)?.stringValue,
              let tsNumber = o["ts"] as? NSNumber, let origin = o["o"] as? String else { return nil }
        let deleted = (o["x"] as? Bool) ?? ((o["x"] as? NSNumber)?.boolValue ?? false)
        return SyncItem(kind: kind, uid: uid, ts: tsNumber.int64Value, origin: origin, deleted: deleted,
                        data: o["d"] as? JSONObject)
    }
}

public final class SyncEngine {
    public let deviceID: String
    public private(set) var adapters: [String: SyncAdapter] = [:]
    private var items: [String: MirrorItem] = [:]        // "kind\u{1}uid" -> item
    private var clock: Int64 = 0
    private let fileURL: URL?
    private let lock = NSRecursiveLock()
    public static let tombstoneDays = 180.0

    public init(deviceID: String, adapters: [SyncAdapter], storage: URL? = nil) {
        self.deviceID = deviceID
        self.fileURL = storage
        for a in adapters { self.adapters[a.kind] = a }
        load()
    }

    // MARK: fingerprints

    public static func canonical(_ object: Any) -> String {
        guard JSONSerialization.isValidJSONObject(object),
              let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
              let text = String(data: data, encoding: .utf8) else { return "" }
        return text
    }

    public static func fingerprint(_ data: JSONObject) -> String {
        SaintCrypto.sha256(Data(canonical(data).utf8)).hex.prefix(24).description
    }

    private func key(_ kind: String, _ uid: String) -> String { "\(kind)\u{1}\(uid)" }

    // MARK: clock

    public func tick() -> Int64 {
        lock.lock(); defer { lock.unlock() }
        let now = Int64(Date().timeIntervalSince1970 * 1000)
        clock = max(now, clock + 1)
        return clock
    }

    public func observe(_ ts: Int64) {
        lock.lock(); defer { lock.unlock() }
        if ts > clock { clock = ts }
    }

    public var currentClock: Int64 { lock.lock(); defer { lock.unlock() }; return clock }

    // MARK: local changes

    /// Record local edits and deletions in the mirror. Returns how many.
    @discardableResult
    public func scan() -> Int {
        lock.lock(); defer { lock.unlock() }
        var changed = 0
        for (kind, adapter) in adapters {
            let snap = adapter.snapshot()
            for (uid, data) in snap {
                let h = adapter.fingerprint(data)
                let k = key(kind, uid)
                let canonicalData = SyncEngine.canonical(data)
                if let m = items[k], !m.deleted, m.hash == h {
                    if m.data != canonicalData {      // volatile fields moved: refresh what we'd serve, keep the timestamp
                        var updated = m
                        updated.data = canonicalData
                        items[k] = updated
                    }
                    continue
                }
                items[k] = MirrorItem(kind: kind, uid: uid, ts: tick(), origin: deviceID, hash: h, deleted: false,
                                      data: canonicalData)
                changed += 1
            }
            for (k, m) in items where m.kind == kind && !m.deleted && snap[m.uid] == nil {
                items[k] = MirrorItem(kind: kind, uid: m.uid, ts: tick(), origin: deviceID, hash: "", deleted: true, data: nil)
                changed += 1
            }
        }
        let cutoff = Int64((Date().timeIntervalSince1970 - SyncEngine.tombstoneDays * 86400) * 1000)
        items = items.filter { !($0.value.deleted && $0.value.ts < cutoff) }
        save()
        return changed
    }

    // MARK: exchanging

    /// {"clock": ms, "items": {kind: {uid: [ts, origin, deleted]}}}
    public func manifest() -> JSONObject {
        lock.lock(); defer { lock.unlock() }
        var byKind: [String: [String: Any]] = [:]
        for m in items.values {
            byKind[m.kind, default: [:]][m.uid] = [m.ts, m.origin, m.deleted] as [Any]
        }
        // "kinds": what this device can store, so the other side doesn't send kinds it would only drop.
        return ["clock": clock, "items": byKind, "kinds": Array(adapters.keys).sorted()]
    }

    /// (what I want from them as [kind, uid], items I have that are newer than theirs)
    public func diff(remote: JSONObject) -> (want: [[String]], offer: [SyncItem]) {
        if let c = (remote["clock"] as? NSNumber)?.int64Value { observe(c) }
        let theirs = (remote["items"] as? [String: Any]) ?? [:]
        lock.lock(); defer { lock.unlock() }
        var want: [[String]] = []
        var offer: [SyncItem] = []
        for (kind, entriesAny) in theirs {
            guard adapters[kind] != nil, let entries = entriesAny as? [String: Any] else { continue }
            for (uid, metaAny) in entries {
                guard let meta = metaAny as? [Any], meta.count >= 2, let ts = (meta[0] as? NSNumber)?.int64Value,
                      let origin = meta[1] as? String else { continue }
                if let m = items[key(kind, uid)] {
                    if (ts, origin) > (m.ts, m.origin) { want.append([kind, uid]) }
                } else {
                    want.append([kind, uid])
                }
            }
        }
        let theirKinds = (remote["kinds"] as? [String]).map { Set($0) }
        for m in items.values {
            if let kinds = theirKinds, !kinds.contains(m.kind) { continue }
            let meta = ((theirs[m.kind] as? [String: Any])?[m.uid]) as? [Any]
            if let meta = meta, meta.count >= 2, let ts = (meta[0] as? NSNumber)?.int64Value, let origin = meta[1] as? String {
                if (m.ts, m.origin) > (ts, origin) { offer.append(item(from: m)) }
            } else {
                offer.append(item(from: m))
            }
        }
        return (want, offer)
    }

    private func item(from m: MirrorItem) -> SyncItem {
        var data: JSONObject?
        if !m.deleted, let text = m.data, let raw = text.data(using: .utf8),
           let object = (try? JSONSerialization.jsonObject(with: raw)) as? JSONObject {
            data = object
        }
        return SyncItem(kind: m.kind, uid: m.uid, ts: m.ts, origin: m.origin, deleted: m.deleted, data: data)
    }

    public func items(for want: [[String]]) -> [SyncItem] {
        lock.lock(); defer { lock.unlock() }
        return want.compactMap { pair in
            guard pair.count == 2, let m = items[key(pair[0], pair[1])] else { return nil }
            return item(from: m)
        }
    }

    /// Apply remote items that are newer than ours. Returns how many changed anything.
    @discardableResult
    public func apply(_ incoming: [SyncItem]) -> Int {
        lock.lock(); defer { lock.unlock() }
        var byKind: [String: [SyncItem]] = [:]
        for it in incoming {
            guard adapters[it.kind] != nil else { continue }
            observe(it.ts)
            if let m = items[key(it.kind, it.uid)], (m.ts, m.origin) >= (it.ts, it.origin) { continue }
            if !it.deleted && it.data == nil { continue }
            byKind[it.kind, default: []].append(it)
        }
        var applied = 0
        for (kind, batch) in byKind {
            guard let adapter = adapters[kind] else { continue }
            let changes: [(uid: String, data: JSONObject?)] = batch.map { item in
                let payload: JSONObject? = item.deleted ? nil : item.data
                return (uid: item.uid, data: payload)
            }
            let refused = Set(adapter.apply(changes: changes))
            let snap = adapter.snapshot()
            for it in batch {
                let k = key(kind, it.uid)
                if refused.contains(it.uid) {
                    items[k] = MirrorItem(kind: kind, uid: it.uid, ts: tick(), origin: deviceID, hash: "", deleted: true, data: nil)
                    continue
                }
                if it.deleted {
                    items[k] = MirrorItem(kind: kind, uid: it.uid, ts: it.ts, origin: it.origin, hash: "", deleted: true, data: nil)
                } else {
                    let stored = snap[it.uid] ?? it.data ?? [:]
                    items[k] = MirrorItem(kind: kind, uid: it.uid, ts: it.ts, origin: it.origin,
                                          hash: adapter.fingerprint(stored), deleted: false,
                                          data: SyncEngine.canonical(stored))
                }
                applied += 1
            }
        }
        save()
        return applied
    }

    // MARK: persistence

    private struct Stored: Codable {
        var clock: Int64
        var items: [MirrorItem]
    }

    private func load() {
        guard let url = fileURL, let data = try? Data(contentsOf: url),
              let stored = try? JSONDecoder().decode(Stored.self, from: data) else { return }
        clock = stored.clock
        for m in stored.items { items[key(m.kind, m.uid)] = m }
    }

    private func save() {
        guard let url = fileURL else { return }
        let stored = Stored(clock: clock, items: Array(items.values))
        if let data = try? JSONEncoder().encode(stored) {
            try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? data.write(to: url, options: .atomic)
        }
    }
}

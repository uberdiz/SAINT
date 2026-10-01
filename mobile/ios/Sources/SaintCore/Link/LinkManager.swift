import Foundation

public enum LinkEvent {
    case connected(PeerInfo)
    case disconnected(peerID: String)
    case paired(LinkPeer)
    case synced(peer: String, received: Int, sent: Int)
    case fileReceived(peer: String, name: String, url: URL)
    case fileProgress(peer: String, name: String, done: Int64, total: Int64)
    case problem(String)
}

/// Everything about talking to your other SAINTs: pairing, staying connected, sync, files, questions and
/// automations. The phone always dials out — it never listens — so a paired PC reaches it only through the
/// connection the phone opened, and anything a *collaborator* sends over it is refused.
public final class LinkManager: PCBridge {
    public private(set) var identity: LinkIdentity
    public let peerStore: PeerStore
    public let engine: SyncEngine
    public let feed: ContextFeed
    public var inboxDirectory: URL
    public var makeTransport: (_ host: String, _ port: Int) -> ByteTransport
    public var onEvent: ((LinkEvent) -> Void)?
    public var shareContext = true
    public var languages: () -> [String] = { [] }
    public static let maxFileBytes: Int64 = 1 << 30

    private var connections: [String: LinkConnection] = [:]
    private var connectTasks: [String: Task<LinkConnection, Error>] = [:]
    private var hints: [String: (host: String, port: Int)] = [:]
    private var backoff: [String: (next: Date, delay: TimeInterval)] = [:]
    private var incoming: [Data: IncomingFile] = [:]
    private var syncing = Set<String>()
    private var syncDirty = false
    private var loop: Task<Void, Never>?
    private var debounce: Task<Void, Never>?
    private var observer: NSObjectProtocol?
    private let lock = NSLock()

    public init(identity: LinkIdentity, peerStore: PeerStore, engine: SyncEngine, feed: ContextFeed, inboxDirectory: URL,
                makeTransport: @escaping (_ host: String, _ port: Int) -> ByteTransport) {
        self.identity = identity
        self.peerStore = peerStore
        self.engine = engine
        self.feed = feed
        self.inboxDirectory = inboxDirectory
        self.makeTransport = makeTransport
    }

    // MARK: who is there

    public var peers: [PeerInfo] {
        let live = Set(connectedIDs())
        return peerStore.all().map {
            PeerInfo(id: $0.id, name: $0.name, role: $0.role, platform: $0.platform, nicknames: $0.nicknames,
                     online: live.contains($0.id))
        }
    }

    public func connectedIDs() -> [String] {
        lock.lock()
        defer { lock.unlock() }
        return connections.filter { !$0.value.isClosed }.map { $0.key }
    }

    private func liveConnection(_ id: String) -> LinkConnection? {
        lock.lock()
        defer { lock.unlock() }
        if let c = connections[id], !c.isClosed { return c }
        return nil
    }

    private func emit(_ event: LinkEvent) { onEvent?(event) }

    public func rename(to name: String) { identity.name = String(name.prefix(40)) }

    // MARK: starting and stopping

    /// Keep every paired device connected, and sync whenever something changes.
    public func start() {
        if loop != nil { return }
        observer = NotificationCenter.default.addObserver(forName: .saintDataChanged, object: nil, queue: nil) { [weak self] _ in
            self?.syncSoon()
        }
        loop = Task { [weak self] in
            var round = 0
            while !Task.isCancelled {
                guard let self = self else { return }
                self.reconnectDue()
                if round % 3 == 0 { self.keepAlive() }
                if round % 12 == 0 { self.syncSoon(delay: 0.2) }
                round += 1
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
    }

    public func stop() {
        loop?.cancel()
        loop = nil
        debounce?.cancel()
        if let o = observer { NotificationCenter.default.removeObserver(o) }
        observer = nil
        lock.lock()
        let all = Array(connections.values)
        connections = [:]
        lock.unlock()
        for c in all { c.close() }
    }

    /// The network changed or the app came back: try everything again now.
    public func reconnectNow() {
        lock.lock()
        backoff = [:]
        lock.unlock()
        reconnectDue()
    }

    public func setAddressHint(peerID: String, host: String, port: Int) {
        lock.lock()
        hints[peerID] = (host, port)
        lock.unlock()
        if peerStore.get(peerID) != nil { reconnectNow() }
    }

    private func reconnectDue() {
        let now = Date()
        for peer in peerStore.all() where peer.autoConnect {
            lock.lock()
            let connected = connections[peer.id].map { !$0.isClosed } ?? false
            let dialling = connectTasks[peer.id] != nil
            var waiting = false
            if let b = backoff[peer.id] { waiting = b.next > now }
            let known = hints[peer.id] != nil || (!peer.host.isEmpty && peer.port > 0)
            lock.unlock()
            if connected || dialling || waiting || !known { continue }
            Task { [weak self] in
                guard let self = self else { return }
                do {
                    _ = try await self.connect(peerID: peer.id)
                    self.noteConnectResult(peer.id, success: true)
                } catch {
                    self.noteConnectResult(peer.id, success: false)
                }
            }
        }
    }

    private func noteConnectResult(_ id: String, success: Bool) {
        lock.lock()
        defer { lock.unlock() }
        if success {
            backoff[id] = nil
        } else {
            let delay = min((backoff[id]?.delay ?? 2) * 2, 120)
            backoff[id] = (Date().addingTimeInterval(delay), delay)
        }
    }

    private func keepAlive() {
        lock.lock()
        let live = Array(connections.values)
        lock.unlock()
        for c in live where !c.isClosed {
            if Date().timeIntervalSince(c.lastReceived) > 50 {
                c.close()
            } else {
                try? c.notify("ping")
            }
        }
    }

    // MARK: connecting and pairing

    public func connect(peerID: String) async throws -> LinkConnection {
        if let c = liveConnection(peerID) { return c }
        let task = connectTask(for: peerID)
        defer { clearConnectTask(peerID) }
        return try await task.value
    }

    private func connectTask(for peerID: String) -> Task<LinkConnection, Error> {
        lock.lock()
        defer { lock.unlock() }
        if let existing = connectTasks[peerID] { return existing }
        let task = Task<LinkConnection, Error> { [weak self] in
            guard let self = self else { throw LinkError.closed }
            return try await self.dial(peerID: peerID)
        }
        connectTasks[peerID] = task
        return task
    }

    private func clearConnectTask(_ peerID: String) {
        lock.lock()
        connectTasks[peerID] = nil
        lock.unlock()
    }

    private func address(of peer: LinkPeer) -> (host: String, port: Int)? {
        lock.lock()
        let hint = hints[peer.id]
        lock.unlock()
        if let h = hint { return h }
        if !peer.host.isEmpty && peer.port > 0 { return (peer.host, peer.port) }
        return nil
    }

    private func dial(peerID: String) async throws -> LinkConnection {
        guard let peer = peerStore.get(peerID) else { throw LinkError.notConnected }
        guard let (host, port) = address(of: peer) else {
            throw LinkError.unreachable("I don't know where \(peer.name) is yet.")
        }
        let transport = makeTransport(host, port)
        do {
            let connection = try await LinkConnection.connect(transport: transport, identity: identity, peer: peer)
            peerStore.update(peer.id) { $0.host = host; $0.port = port; $0.lastSeen = Date() }
            attach(connection, peer: peer)
            return connection
        } catch {
            transport.close()
            if let link = error as? LinkError { throw link }
            throw LinkError.unreachable("Couldn't reach \(peer.name) at \(host):\(port).")
        }
    }

    @discardableResult
    public func pair(link: Pairing.PairLink) async throws -> LinkPeer {
        try await pair(host: link.host, port: link.port, token: link.token, role: link.role)
    }

    @discardableResult
    public func pair(host: String, port: Int, code: String, role: String) async throws -> LinkPeer {
        try await pair(host: host, port: port, token: try Pairing.decodeCode(code), role: role)
    }

    @discardableResult
    public func pair(host: String, port: Int, token: Data, role: String) async throws -> LinkPeer {
        let transport = makeTransport(host, port)
        do {
            let (peer, connection) = try await LinkConnection.pair(transport: transport, identity: identity, host: host,
                                                                   port: port, token: token, role: role)
            var saved = peer
            saved.lastSeen = Date()
            peerStore.save(saved)
            attach(connection, peer: saved)
            emit(.paired(saved))
            return saved
        } catch {
            transport.close()
            throw error
        }
    }

    public func unpair(_ peerID: String) {
        lock.lock()
        let c = connections[peerID]
        connections[peerID] = nil
        lock.unlock()
        c?.close()
        peerStore.remove(peerID)
    }

    private func attach(_ connection: LinkConnection, peer: LinkPeer) {
        let id = peer.id
        connection.onNotification = { [weak self] type, data in self?.handleNotification(peerID: id, type: type, data: data) }
        connection.onRequest = { [weak self] type, data in
            guard let self = self else { throw LinkError.closed }
            return try await self.handleRequest(peerID: id, type: type, data: data)
        }
        connection.onChunk = { [weak self] tid, offset, data in self?.handleChunk(peerID: id, transferID: tid, offset: offset, data: data) }
        connection.onClose = { [weak self, weak connection] in
            guard let self = self else { return }
            self.lock.lock()
            if let current = self.connections[id], current === connection { self.connections[id] = nil }
            self.lock.unlock()
            self.emit(.disconnected(peerID: id))
        }
        lock.lock()
        let old = connections[id]
        connections[id] = connection
        lock.unlock()
        old?.close()
        connection.start()
        emit(.connected(PeerInfo(id: id, name: peer.name, role: peer.role, platform: peer.platform,
                                 nicknames: peer.nicknames, online: true)))
        if peer.isOwn { syncSoon(delay: 0.5) }
    }

    // MARK: asking and doing

    public func ask(peerID: String, text: String, language: String) async throws -> AskAnswer {
        let connection = try await connect(peerID: peerID)
        let reply = try await connection.request("chat.ask", ["text": String(text.prefix(2000)), "lang": language], timeout: 150)
        return AskAnswer(text: (reply["text"] as? String) ?? "", expectsReply: (reply["expects_reply"] as? Bool) ?? false,
                         language: (reply["lang"] as? String) ?? "")
    }

    public func runAutomation(peerID: String, name: String, args: JSONObject) async throws -> String {
        let connection = try await connect(peerID: peerID)
        let reply = try await connection.request("automation.run", ["name": name, "args": args], timeout: 150)
        return (reply["text"] as? String) ?? ""
    }

    public func automations(peerID: String) async throws -> [JSONObject] {
        let connection = try await connect(peerID: peerID)
        let reply = try await connection.request("automation.list", [:], timeout: 20)
        return (reply["automations"] as? [JSONObject]) ?? []
    }

    public func status(peerID: String) async throws -> JSONObject {
        let connection = try await connect(peerID: peerID)
        return try await connection.request("status.get", [:], timeout: 20)
    }

    /// Share one learned thing with a collaborator, who decides whether to keep it.
    public func share(peerID: String, kind: String, uid: String, data: JSONObject) async throws {
        let connection = try await connect(peerID: peerID)
        _ = try await connection.request("share.push", ["items": [["k": kind, "u": uid, "d": data]]], timeout: 30)
    }

    /// Tell your other devices what you just did, so they keep up (never written to disk on their side).
    public func shareTurn(user: String, reply: String) {
        if !shareContext || user.isEmpty { return }
        lock.lock()
        let live = connections.values.filter { !$0.isClosed && $0.peerRole == "own" }
        lock.unlock()
        for c in live {
            try? c.notify("context.feed", ["source": identity.name, "user": String(user.prefix(240)),
                                           "reply": String(reply.prefix(240)), "ts": Date().timeIntervalSince1970])
        }
    }

    // MARK: sync

    @discardableResult
    public func syncNow() async -> Int {
        var n = 0
        for id in connectedIDs() {
            guard let peer = peerStore.get(id), peer.isOwn else { continue }
            if (try? await syncWith(peerID: id)) != nil { n += 1 }
        }
        return n
    }

    private func syncSoon(delay: TimeInterval = 3) {
        lock.lock()
        debounce?.cancel()
        debounce = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            if Task.isCancelled { return }
            _ = await self?.syncNow()
        }
        lock.unlock()
    }

    private func beginSync(_ id: String) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if syncing.contains(id) {
            syncDirty = true
            return false
        }
        syncing.insert(id)
        return true
    }

    private func endSync(_ id: String) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        syncing.remove(id)
        let again = syncDirty
        syncDirty = false
        return again
    }

    @discardableResult
    public func syncWith(peerID: String) async throws -> (received: Int, sent: Int) {
        if !beginSync(peerID) { return (0, 0) }
        var received = 0, sent = 0
        do {
            let connection = try await connect(peerID: peerID)
            engine.scan()
            let response = try await connection.request("sync.manifest", ["manifest": engine.manifest()], timeout: 90)
            let theirs = ((response["items"] as? [Any]) ?? []).compactMap { SyncItem.from(json: $0) }
            received = engine.apply(theirs)
            let want: [[String]] = ((response["want"] as? [Any]) ?? []).compactMap { entry in
                (entry as? [Any])?.compactMap { $0 as? String }
            }
            let mine = engine.items(for: want)
            var start = 0
            while start < mine.count {
                let batch = Array(mine[start..<min(start + 150, mine.count)])
                _ = try await connection.request("sync.push", ["items": batch.map { $0.toJSON() }], timeout: 90)
                start += 150
            }
            sent = mine.count
            peerStore.update(peerID) { $0.lastSeen = Date() }
            if received > 0 || sent > 0 {
                emit(.synced(peer: peerStore.get(peerID)?.name ?? "", received: received, sent: sent))
            }
        } catch {
            if endSync(peerID) { syncSoon(delay: 1) }
            throw error
        }
        if endSync(peerID) { syncSoon(delay: 1) }
        return (received, sent)
    }

    // MARK: what the other device sends us

    private func requireOwn(_ peerID: String) throws {
        guard peerStore.get(peerID)?.isOwn == true else {
            throw LinkError.remote(code: "denied", message: "This phone only takes requests from your own devices.")
        }
    }

    private func handleNotification(peerID: String, type: String, data: JSONObject) {
        guard peerStore.get(peerID)?.isOwn == true else { return }
        switch type {
        case "context.feed":
            absorbContext(peerID: peerID, data: data)
        case "file.cancel":
            if let hex = data["id"] as? String, let tid = Data(hex: hex) { dropIncoming(tid) }
        default:
            break
        }
    }

    private func absorbContext(peerID: String, data: JSONObject) {
        let ts = (data["ts"] as? NSNumber)?.doubleValue ?? Date().timeIntervalSince1970
        feed.add(source: (data["source"] as? String) ?? (peerStore.get(peerID)?.name ?? ""), user: (data["user"] as? String) ?? "",
                 reply: (data["reply"] as? String) ?? "", ts: Date(timeIntervalSince1970: ts))
    }

    private func handleRequest(peerID: String, type: String, data: JSONObject) async throws -> JSONObject {
        switch type {
        case "sync.manifest":
            try requireOwn(peerID)
            engine.scan()
            let result = engine.diff(remote: (data["manifest"] as? JSONObject) ?? [:])
            return ["want": result.want, "items": result.offer.map { $0.toJSON() }]
        case "sync.push":
            try requireOwn(peerID)
            let items = ((data["items"] as? [Any]) ?? []).compactMap { SyncItem.from(json: $0) }
            return ["applied": engine.apply(items)]
        case "context.feed":
            try requireOwn(peerID)
            absorbContext(peerID: peerID, data: data)
            return [:]
        case "status.get":
            try requireOwn(peerID)
            return ["device": identity.hello(), "time": Date().timeIntervalSince1970, "now_playing": [String: Any](),
                    "language": languages()]
        case "file.offer":
            try requireOwn(peerID)
            return try fileOffer(peerID: peerID, data: data)
        case "file.done":
            try requireOwn(peerID)
            return try fileDone(peerID: peerID, data: data)
        case "file.cancel":
            try requireOwn(peerID)
            if let hex = data["id"] as? String, let tid = Data(hex: hex) { dropIncoming(tid) }
            return [:]
        default:
            throw LinkError.remote(code: "unknown", message: "This phone doesn't handle '\(type)'.")
        }
    }

    // MARK: files

    private final class IncomingFile {
        let id: Data
        let peerID: String
        let peerName: String
        let name: String
        let size: Int64
        let sha: String
        let temp: URL
        let handle: FileHandle
        let hash = StreamingSHA256()
        var received: Int64 = 0
        var failure: String?

        init(id: Data, peerID: String, peerName: String, name: String, size: Int64, sha: String, temp: URL, handle: FileHandle) {
            self.id = id
            self.peerID = peerID
            self.peerName = peerName
            self.name = name
            self.size = size
            self.sha = sha
            self.temp = temp
            self.handle = handle
        }
    }

    /// The same rules as the desktop's sanitize_filename: the last path component, no control or reserved characters.
    static func sanitize(_ name: String) -> String {
        let last = name.replacingOccurrences(of: "\\", with: "/").components(separatedBy: "/").last ?? ""
        var cleaned = String.UnicodeScalarView()
        let reserved = Set("<>:\"|?*".unicodeScalars)
        for scalar in last.unicodeScalars {
            if scalar.value < 32 || reserved.contains(scalar) { cleaned.append("_") } else { cleaned.append(scalar) }
        }
        let trimmed = String(cleaned).trimmingCharacters(in: CharacterSet(charactersIn: " ."))
        if trimmed.isEmpty { return "file" }
        return String(trimmed.prefix(150))
    }

    private func inbox(for peerName: String) throws -> URL {
        let folder = inboxDirectory.appendingPathComponent(LinkManager.sanitize(peerName), isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        return folder
    }

    private func fileOffer(peerID: String, data: JSONObject) throws -> JSONObject {
        guard let hex = data["id"] as? String, let tid = Data(hex: hex), tid.count == 16,
              let size = (data["size"] as? NSNumber)?.int64Value, size >= 0,
              let sha = (data["sha256"] as? String)?.lowercased(), sha.count == 64 else {
            throw LinkError.remote(code: "bad_request", message: "bad file offer")
        }
        if size > LinkManager.maxFileBytes {
            throw LinkError.remote(code: "too_large", message: "That file is bigger than this phone will take.")
        }
        let peerName = peerStore.get(peerID)?.name ?? "device"
        let name = LinkManager.sanitize((data["name"] as? String) ?? "file")
        do {
            let folder = try inbox(for: peerName)
            let temp = folder.appendingPathComponent(".\(hex).part")
            FileManager.default.createFile(atPath: temp.path, contents: nil)
            let handle = try FileHandle(forWritingTo: temp)
            lock.lock()
            if incoming.count >= 4 {
                lock.unlock()
                try? handle.close()
                try? FileManager.default.removeItem(at: temp)
                throw LinkError.remote(code: "busy", message: "Too many transfers at once; try again in a moment.")
            }
            incoming[tid] = IncomingFile(id: tid, peerID: peerID, peerName: peerName, name: name, size: size, sha: sha,
                                         temp: temp, handle: handle)
            lock.unlock()
        } catch let error as LinkError {
            throw error
        } catch {
            throw LinkError.remote(code: "io", message: "There isn't room for that file here.")
        }
        return ["accept": true]
    }

    private func handleChunk(peerID: String, transferID: Data, offset: UInt64, data: Data) {
        lock.lock()
        let file = incoming[transferID]
        lock.unlock()
        guard let f = file, f.peerID == peerID, f.failure == nil else { return }
        if Int64(offset) != f.received || f.received + Int64(data.count) > f.size {
            f.failure = "chunk out of order or too long"
            return
        }
        do {
            try f.handle.write(contentsOf: data)
        } catch {
            f.failure = "couldn't write the file"
            return
        }
        f.hash.update(data)
        f.received += Int64(data.count)
        emit(.fileProgress(peer: f.peerName, name: f.name, done: f.received, total: f.size))
    }

    private func dropIncoming(_ tid: Data) {
        lock.lock()
        let f = incoming.removeValue(forKey: tid)
        lock.unlock()
        if let f = f {
            try? f.handle.close()
            try? FileManager.default.removeItem(at: f.temp)
        }
    }

    private func fileDone(peerID: String, data: JSONObject) throws -> JSONObject {
        guard let hex = data["id"] as? String, let tid = Data(hex: hex) else {
            throw LinkError.remote(code: "bad_request", message: "bad request")
        }
        lock.lock()
        let file = incoming.removeValue(forKey: tid)
        lock.unlock()
        guard let f = file, f.peerID == peerID else {
            throw LinkError.remote(code: "unknown_transfer", message: "I'm not receiving that file.")
        }
        try? f.handle.close()
        if f.failure != nil || f.received != f.size || f.hash.finish() != f.sha {
            try? FileManager.default.removeItem(at: f.temp)
            throw LinkError.remote(code: "corrupt", message: "The file arrived damaged, so I threw it away.")
        }
        let folder = f.temp.deletingLastPathComponent()
        let final = LinkManager.uniqueURL(in: folder, name: f.name)
        do {
            try FileManager.default.moveItem(at: f.temp, to: final)
        } catch {
            try? FileManager.default.removeItem(at: f.temp)
            throw LinkError.remote(code: "io", message: "I couldn't save it.")
        }
        emit(.fileReceived(peer: f.peerName, name: final.lastPathComponent, url: final))
        return ["ok": true, "name": final.lastPathComponent]
    }

    static func uniqueURL(in folder: URL, name: String) -> URL {
        var candidate = folder.appendingPathComponent(name)
        if !FileManager.default.fileExists(atPath: candidate.path) { return candidate }
        let base = (name as NSString).deletingPathExtension
        let ext = (name as NSString).pathExtension
        var n = 2
        while FileManager.default.fileExists(atPath: candidate.path) {
            candidate = folder.appendingPathComponent(ext.isEmpty ? "\(base) (\(n))" : "\(base) (\(n)).\(ext)")
            n += 1
        }
        return candidate
    }

    /// Send a file to a device. Returns the name it was saved under there.
    public func sendFile(peerID: String, url: URL, progress: ((Int64, Int64) -> Void)? = nil) async throws -> String {
        let connection = try await connect(peerID: peerID)
        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        let size = (attributes[.size] as? NSNumber)?.int64Value ?? 0
        let sha = try SaintCrypto.sha256Hex(ofFile: url)
        let tid = SaintCrypto.randomBytes(16)
        _ = try await connection.request("file.offer", ["id": tid.hex, "name": url.lastPathComponent, "size": size, "sha256": sha],
                                         timeout: 90)
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var sent: UInt64 = 0
        do {
            while true {
                let block = try handle.read(upToCount: LinkWire.chunkSize) ?? Data()
                if block.isEmpty { break }
                try await connection.sendChunk(transferID: tid, offset: sent, data: block)
                sent += UInt64(block.count)
                progress?(Int64(sent), size)
            }
            let done = try await connection.request("file.done", ["id": tid.hex], timeout: 60)
            return (done["name"] as? String) ?? url.lastPathComponent
        } catch {
            try? connection.notify("file.cancel", ["id": tid.hex])
            throw error
        }
    }
}

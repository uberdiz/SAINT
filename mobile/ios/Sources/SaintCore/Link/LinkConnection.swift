import Foundation

/// A TCP connection, as bytes. The app implements it with Network.framework; SaintCore only needs this much.
public protocol ByteTransport: AnyObject {
    func connect() async throws
    func send(_ data: Data) async throws
    /// The next bytes to arrive. Throws `LinkError.closed` at the end of the stream.
    func receive() async throws -> Data
    func close()
}

/// One live, encrypted connection to a paired device: requests with answers, notifications, file chunks.
/// See modules/link/node.py for the other end. Messages are `{"t": type, "id": n, "d": {…}}`; an answer is
/// `{"re": n, "ok": true, "d": {…}}` or `{"re": n, "ok": false, "e": {"code": …, "msg": …}}`.
public final class LinkConnection {
    public let peerID: String
    public let peerName: String
    public let peerRole: String
    public private(set) var lastReceived = Date()

    public var onNotification: ((String, JSONObject) -> Void)?
    public var onRequest: ((String, JSONObject) async throws -> JSONObject)?
    public var onChunk: ((Data, UInt64, Data) -> Void)?
    public var onClose: (() -> Void)?

    private let transport: ByteTransport
    private let channel: SecureChannel
    private var decoder: FrameDecoder
    private let lock = NSLock()
    private var waiting: [Int: CheckedContinuation<JSONObject, Error>] = [:]
    private var nextID = 1
    private var closed = false
    private var outbox: AsyncStream<Data>.Continuation?
    private var queuedBytes = 0
    private var started = false
    private var early: [Data]

    init(peerID: String, peerName: String, peerRole: String, transport: ByteTransport, channel: SecureChannel,
         decoder: FrameDecoder, earlyFrames: [Data]) {
        self.peerID = peerID
        self.peerName = peerName
        self.peerRole = peerRole
        self.transport = transport
        self.channel = channel
        self.decoder = decoder
        self.early = earlyFrames
    }

    public var isClosed: Bool {
        lock.lock()
        defer { lock.unlock() }
        return closed
    }

    /// Start reading and writing. Set the callbacks first.
    public func start() {
        lock.lock()
        if started || closed {
            lock.unlock()
            return
        }
        started = true
        var continuation: AsyncStream<Data>.Continuation?
        let stream = AsyncStream<Data> { continuation = $0 }
        outbox = continuation
        lock.unlock()
        Task { [weak self] in
            for await bytes in stream {
                guard let self = self else { return }
                do {
                    try await self.transport.send(bytes)
                    self.noteSent(bytes.count)
                } catch {
                    self.close()
                    return
                }
            }
        }
        Task { [weak self] in await self?.readLoop() }
    }

    private func noteSent(_ count: Int) {
        lock.lock()
        queuedBytes = max(0, queuedBytes - count)
        lock.unlock()
    }

    // MARK: sending

    private func enqueue(_ frames: [Data]) throws {
        lock.lock()
        defer { lock.unlock() }
        if closed { throw LinkError.closed }
        for f in frames {
            queuedBytes += f.count
            outbox?.yield(f)
        }
    }

    public func send(json: JSONObject) throws {
        lock.lock()
        defer { lock.unlock() }
        if closed { throw LinkError.closed }
        let frames = try channel.encode(json: json)        // encoding and queueing under one lock keeps the nonces in order
        for f in frames {
            queuedBytes += f.count
            outbox?.yield(f)
        }
    }

    public func notify(_ type: String, _ data: JSONObject = [:]) throws {
        try send(json: ["t": type, "d": data])
    }

    public func request(_ type: String, _ data: JSONObject = [:], timeout: TimeInterval = 20) async throws -> JSONObject {
        let id = takeRequestID()
        return try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<JSONObject, Error>) in
            lock.lock()
            if closed {
                lock.unlock()
                continuation.resume(throwing: LinkError.closed)
                return
            }
            waiting[id] = continuation
            lock.unlock()
            do {
                try send(json: ["t": type, "id": id, "d": data])
            } catch {
                fail(id, error)
                return
            }
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                self?.fail(id, LinkError.timeout)
            }
        }
    }

    private func takeRequestID() -> Int {
        lock.lock()
        defer { lock.unlock() }
        let id = nextID
        nextID += 1
        return id
    }

    private func sendState() -> (busy: Bool, closed: Bool) {
        lock.lock()
        defer { lock.unlock() }
        return (queuedBytes > 1_000_000, closed)
    }

    private func fail(_ id: Int, _ error: Error) {
        lock.lock()
        let c = waiting.removeValue(forKey: id)
        lock.unlock()
        c?.resume(throwing: error)
    }

    /// Send one file chunk, waiting while too much is already queued so a big file doesn't sit in memory.
    public func sendChunk(transferID: Data, offset: UInt64, data: Data) async throws {
        while true {
            let state = sendState()
            if state.closed { throw LinkError.closed }
            if !state.busy { break }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        let frame = try channel.encodeChunk(transferID: transferID, offset: offset, data: data)
        try enqueue([frame])
    }

    // MARK: receiving

    private func readLoop() async {
        do {
            for frame in early { try process(frame) }
            early = []
            while true {
                let bytes = try await transport.receive()
                lastReceived = Date()
                for frame in decoder.feed(bytes) { try process(frame) }
            }
        } catch {
            // the stream ended or a message failed authentication: either way this connection is over
        }
        close()
    }

    private func process(_ frame: Data) throws {
        guard let message = try channel.decode(frame: frame) else { return }
        switch message {
        case .json(let object): handle(object)
        case .chunk(let id, let offset, let data): onChunk?(id, offset, data)
        }
    }

    private func handle(_ object: JSONObject) {
        if let re = (object["re"] as? NSNumber)?.intValue {
            lock.lock()
            let c = waiting.removeValue(forKey: re)
            lock.unlock()
            guard let continuation = c else { return }
            if (object["ok"] as? Bool) ?? ((object["ok"] as? NSNumber)?.boolValue ?? false) {
                continuation.resume(returning: (object["d"] as? JSONObject) ?? [:])
            } else {
                let e = (object["e"] as? JSONObject) ?? [:]
                continuation.resume(throwing: LinkError.remote(code: (e["code"] as? String) ?? "error",
                                                               message: (e["msg"] as? String) ?? "The request failed."))
            }
            return
        }
        guard let type = object["t"] as? String, type != "ping", type != "pong" else { return }
        let data = (object["d"] as? JSONObject) ?? [:]
        guard let rid = (object["id"] as? NSNumber)?.intValue else {
            onNotification?(type, data)
            return
        }
        Task { [weak self] in
            guard let self = self else { return }
            do {
                guard let handler = self.onRequest else { throw LinkError.remote(code: "unknown", message: "unknown request '\(type)'") }
                let result = try await handler(type, data)
                try? self.send(json: ["re": rid, "ok": true, "d": result])
            } catch let error as LinkError {
                try? self.send(json: ["re": rid, "ok": false, "e": ["code": error.code, "msg": error.errorDescription ?? "The request failed."]])
            } catch {
                try? self.send(json: ["re": rid, "ok": false, "e": ["code": "internal", "msg": "Something went wrong on the other device."]])
            }
        }
    }

    // MARK: closing

    public func close() {
        lock.lock()
        if closed {
            lock.unlock()
            return
        }
        closed = true
        let stragglers = waiting
        waiting = [:]
        outbox?.finish()
        outbox = nil
        lock.unlock()
        transport.close()
        for (_, c) in stragglers { c.resume(throwing: LinkError.closed) }
        onClose?()
    }

    // MARK: handshakes

    /// Open a connection to a device we've paired with (Noise IK).
    public static func connect(transport: ByteTransport, identity: LinkIdentity, peer: LinkPeer,
                               timeout: TimeInterval = 10) async throws -> LinkConnection {
        try await withTimeout(timeout) {
            try await transport.connect()
            let handshake = try ClientHandshake(staticPrivate: identity.privateKey, hello: identity.hello(),
                                                remoteStatic: peer.key, psk: nil)
            try await transport.send(try handshake.begin())
            var decoder = FrameDecoder()
            let (frames, _) = try await readFrames(transport, decoder: &decoder, count: 1)
            let step = try handshake.receive(frame: frames[0])
            guard case .ready(let channel, let hello) = step else { throw LinkError.handshake("unexpected handshake step") }
            if let id = hello?["id"] as? String, id != peer.id { throw LinkError.handshake("That isn't the device I paired with.") }
            return LinkConnection(peerID: peer.id, peerName: peer.name, peerRole: peer.role, transport: transport,
                                  channel: channel, decoder: decoder, earlyFrames: Array(frames.dropFirst()))
        }
    }

    /// Pair with a device that is showing a code (Noise XXpsk3). Returns the new peer and the live connection.
    public static func pair(transport: ByteTransport, identity: LinkIdentity, host: String, port: Int, token: Data,
                            role: String, timeout: TimeInterval = 12) async throws -> (LinkPeer, LinkConnection) {
        try await withTimeout(timeout) {
            try await transport.connect()
            let handshake = try ClientHandshake(staticPrivate: identity.privateKey, hello: identity.hello(role: role),
                                                remoteStatic: nil, psk: Pairing.psk(forToken: token))
            try await transport.send(try handshake.begin())
            var decoder = FrameDecoder()
            let (frames, _) = try await readFrames(transport, decoder: &decoder, count: 1)
            let step: ClientHandshake.Step
            do {
                step = try handshake.receive(frame: frames[0])
            } catch {
                throw LinkError.pairingFailed("Pairing failed: wrong code, or the other device closed the pairing window.")
            }
            guard case .sendAndReady(let third, let channel) = step else { throw LinkError.handshake("unexpected handshake step") }
            try await transport.send(third)
            var extra = Array(frames.dropFirst())
            var message: JSONObject?
            do {
                while message == nil {
                    if extra.isEmpty {
                        let (more, _) = try await readFrames(transport, decoder: &decoder, count: 1)
                        extra = more
                    }
                    let frame = extra.removeFirst()
                    guard let incoming = try channel.decode(frame: frame) else { continue }
                    if case .json(let object) = incoming { message = object }
                }
            } catch {
                // the other side hangs up after a wrong code instead of answering
                throw LinkError.pairingFailed("Pairing failed: wrong code, or the other device closed the pairing window.")
            }
            guard let reply = message, reply["t"] as? String == "pair.ok", let body = reply["d"] as? JSONObject,
                  let theirHello = body["hello"] as? JSONObject, let theirID = theirHello["id"] as? String else {
                throw LinkError.pairingFailed("Pairing failed: unexpected answer.")
            }
            guard theirID == Pairing.deviceID(forPublicKey: channel.remoteStatic) else {
                throw LinkError.pairingFailed("Pairing failed: the device's identity didn't check out.")
            }
            let name = String(((theirHello["name"] as? String) ?? "Device").prefix(40))
            let platform = String(((theirHello["platform"] as? String) ?? "").prefix(16))
            let thePort = (theirHello["port"] as? NSNumber)?.intValue ?? port
            let peer = LinkPeer(id: theirID, name: name, publicKey: channel.remoteStatic.hex, role: role, platform: platform,
                                host: host, port: thePort > 0 && thePort < 65536 ? thePort : port)
            let connection = LinkConnection(peerID: peer.id, peerName: peer.name, peerRole: role, transport: transport,
                                            channel: channel, decoder: decoder, earlyFrames: extra)
            return (peer, connection)
        }
    }

    /// Read from the transport until at least ``count`` whole frames have arrived.
    private static func readFrames(_ transport: ByteTransport, decoder: inout FrameDecoder, count: Int) async throws -> ([Data], Int) {
        var frames: [Data] = []
        while frames.count < count {
            let bytes = try await transport.receive()
            frames += decoder.feed(bytes)
        }
        return (frames, frames.count)
    }
}

/// Run ``body``, giving up with `LinkError.timeout` if it takes longer than ``seconds``.
func withTimeout<T>(_ seconds: TimeInterval, _ body: @escaping () async throws -> T) async throws -> T {
    try await withThrowingTaskGroup(of: T.self) { group in
        group.addTask { try await body() }
        group.addTask {
            try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            throw LinkError.timeout
        }
        guard let first = try await group.next() else { throw LinkError.timeout }
        group.cancelAll()
        return first
    }
}

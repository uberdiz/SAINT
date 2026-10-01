import Foundation
import XCTest
@testable import SaintCore

/// The JSON files in Tests/SaintCoreTests/Resources are copied from the desktop repo's tests/data, where the
/// Python tests check the very same vectors and cases.
enum TestData {
    static func object(_ name: String) throws -> [String: Any] {
        guard let url = Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Resources") else {
            throw NSError(domain: "TestData", code: 1, userInfo: [NSLocalizedDescriptionKey: "missing \(name).json"])
        }
        let data = try Data(contentsOf: url)
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw NSError(domain: "TestData", code: 2, userInfo: [NSLocalizedDescriptionKey: "\(name).json isn't an object"])
        }
        return object
    }
}

func hexData(_ text: String) -> Data { Data(hex: text) ?? Data() }

/// A fresh folder for one test's stores.
func temporaryDirectory(_ name: String = "saint") -> URL {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(name)-\(UUID().uuidString)", isDirectory: true)
    try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url
}

/// Two ends of an in-memory connection.
final class MemoryTransport: ByteTransport {
    private var iterator: AsyncStream<Data>.AsyncIterator
    private var continuation: AsyncStream<Data>.Continuation?
    weak var other: MemoryTransport?
    private(set) var isClosed = false

    init() {
        var c: AsyncStream<Data>.Continuation?
        let stream = AsyncStream<Data> { c = $0 }
        iterator = stream.makeAsyncIterator()
        continuation = c
    }

    static func pair() -> (MemoryTransport, MemoryTransport) {
        let a = MemoryTransport(), b = MemoryTransport()
        a.other = b
        b.other = a
        return (a, b)
    }

    func connect() async throws {}

    func send(_ data: Data) async throws {
        guard !isClosed, let other = other else { throw LinkError.closed }
        other.continuation?.yield(data)
    }

    func receive() async throws -> Data {
        if let next = await iterator.next() { return next }
        throw LinkError.closed
    }

    func close() {
        isClosed = true
        continuation?.finish()
        other?.continuation?.finish()
    }
}

/// A stand-in for the desktop at the other end of the link: the responder side of the same Noise handshakes,
/// then a few request handlers.
final class FakePC {
    let name = "Test PC"
    let privateKey: Data
    let publicKey: Data
    var deviceID: String { Pairing.deviceID(forPublicKey: publicKey) }
    var pairingToken: Data?
    var handlers: [String: (JSONObject) throws -> JSONObject] = [:]
    var engine: SyncEngine?
    private(set) var received: [(type: String, data: JSONObject)] = []
    private(set) var responses: [Int: JSONObject] = [:]
    /// file chunks that arrived, by transfer id
    private(set) var chunks: [Data: Data] = [:]
    private(set) var clientStatic = Data()
    private let transport: MemoryTransport
    private var channel: SecureChannel?
    private var decoder = FrameDecoder()

    init(transport: MemoryTransport, privateKey: Data? = nil) {
        self.transport = transport
        let pair = SaintCrypto.generateKeyPair()
        self.privateKey = privateKey ?? pair.privateKey
        self.publicKey = (try? SaintCrypto.publicKey(forPrivate: self.privateKey)) ?? pair.publicKey
    }

    func hello() -> JSONObject { ["id": deviceID, "name": name, "platform": "windows", "v": 1] }

    /// Serve one connection until it closes.
    func serve() {
        Task { [self] in
            do {
                var buffer = Data()
                while buffer.count < 4 { buffer.append(try await transport.receive()) }
                let mode = buffer[3]
                buffer = Data(buffer.dropFirst(4))
                let pattern: NoisePattern = mode == LinkWire.modePair ? .xxpsk3 : .ik
                let psk = mode == LinkWire.modePair ? pairingToken.map { Pairing.psk(forToken: $0) } : nil
                let state = try HandshakeState(pattern: pattern, initiator: false, staticPrivate: privateKey, remoteStatic: nil,
                                               psk: psk, prologue: LinkWire.prologue(mode: mode), ephemeralPrivate: nil)
                var frames = decoder.feed(buffer)
                while frames.isEmpty { frames = decoder.feed(try await transport.receive()) }
                _ = try state.readMessage(frames.removeFirst())
                try await transport.send(try LinkWire.frame(try state.writeMessage(payload: try LinkWire.encodeJSON(hello()))))
                if pattern == .xxpsk3 {
                    while frames.isEmpty { frames = decoder.feed(try await transport.receive()) }
                    _ = try state.readMessage(frames.removeFirst())
                }
                let (send, receive) = try state.split()
                guard let remote = state.remoteStatic else { return }
                clientStatic = remote
                let channel = SecureChannel(send: send, receive: receive, remoteStatic: remote)
                self.channel = channel
                if pattern == .xxpsk3 {
                    try await sendJSON(["t": "pair.ok", "d": ["hello": hello(), "role": "own"]])
                }
                while true {
                    for frame in frames { try await handle(channel, frame) }
                    frames = []
                    frames = decoder.feed(try await transport.receive())
                }
            } catch {
                transport.close()
            }
        }
    }

    func sendJSON(_ object: JSONObject) async throws {
        guard let channel = channel else { return }
        for frame in try channel.encode(json: object) { try await transport.send(frame) }
    }

    private var nextID = 1000

    /// Ask the phone something, as the desktop's sync loop does, and wait for its answer.
    func call(_ type: String, _ data: JSONObject = [:]) async throws -> JSONObject {
        nextID += 1
        let id = nextID
        try await sendJSON(["t": type, "id": id, "d": data])
        for _ in 0..<300 {
            if let answer = responses[id] { return answer }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        throw LinkError.timeout
    }

    func sendChunk(id: Data, offset: UInt64, data: Data) async throws {
        guard let channel = channel else { return }
        try await transport.send(try channel.encodeChunk(transferID: id, offset: offset, data: data))
    }

    private func handle(_ channel: SecureChannel, _ frame: Data) async throws {
        guard let message = try channel.decode(frame: frame) else { return }
        if case .chunk(let id, _, let bytes) = message {
            chunks[id, default: Data()].append(bytes)
            return
        }
        guard case .json(let object) = message else { return }
        if let re = (object["re"] as? NSNumber)?.intValue {
            responses[re] = object
            return
        }
        guard let type = object["t"] as? String else { return }
        if type == "ping" { return }
        let data = (object["d"] as? JSONObject) ?? [:]
        received.append((type: type, data: data))
        guard let id = (object["id"] as? NSNumber)?.intValue else { return }
        if let handler = handlers[type] {
            do {
                try await sendJSON(["re": id, "ok": true, "d": try handler(data)])
            } catch {
                try await sendJSON(["re": id, "ok": false, "e": ["code": "failed", "msg": "\(error)"]])
            }
        } else {
            try await sendJSON(["re": id, "ok": false, "e": ["code": "unknown", "msg": "unknown request '\(type)'"]])
        }
    }
}

/// Music that records what it was asked.
final class FakeMusic: MusicService {
    var asked: [MusicIntent] = []
    var reply = "Playing Test Song by Test Artist."
    func perform(_ intent: MusicIntent) async -> String {
        asked.append(intent)
        return reply
    }
}

/// The PC, as the brain sees it.
final class FakeBridge: PCBridge {
    var peers: [PeerInfo] = []
    var asked: [(peer: String, text: String, language: String)] = []
    var automations: [(peer: String, name: String, args: JSONObject)] = []
    var answer = AskAnswer(text: "Done on the PC.")
    func ask(peerID: String, text: String, language: String) async throws -> AskAnswer {
        asked.append((peer: peerID, text: text, language: language))
        return answer
    }
    func runAutomation(peerID: String, name: String, args: JSONObject) async throws -> String {
        automations.append((peer: peerID, name: name, args: args))
        return "Sent."
    }
    func syncNow() async -> Int { 1 }
}

final class FakeModel: LanguageModel {
    var prompts: [(system: String, prompt: String)] = []
    var reply = "Because of Rayleigh scattering."
    func respond(system: String, prompt: String) async throws -> String {
        prompts.append((system: system, prompt: prompt))
        return reply
    }
}

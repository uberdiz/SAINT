import Foundation

public enum LinkError: Error, Equatable, LocalizedError {
    case closed
    case timeout
    case handshake(String)
    case denied(String)
    case remote(code: String, message: String)
    case badMessage(String)
    case unreachable(String)
    case notConnected
    case pairingFailed(String)

    public var errorDescription: String? {
        switch self {
        case .closed: return "The connection closed."
        case .timeout: return "The other device didn't answer in time."
        case .handshake(let m): return "Couldn't set up a secure connection: \(m)"
        case .denied(let m): return m
        case .remote(_, let m): return m
        case .badMessage(let m): return "Unreadable message: \(m)"
        case .unreachable(let m): return m
        case .notConnected: return "That device isn't connected."
        case .pairingFailed(let m): return m
        }
    }

    public var code: String {
        switch self {
        case .remote(let code, _): return code
        case .denied: return "denied"
        case .timeout: return "timeout"
        case .closed: return "closed"
        case .notConnected: return "offline"
        default: return "error"
        }
    }
}

public typealias JSONObject = [String: Any]

/// SAINT Link on the wire (see modules/link/wire.py): TCP, every frame `<uint16 length><bytes>`.
///
///     connection start (clear):  'S' 'L' <version=1> <mode>      mode 1 = reconnect (IK), 2 = pairing (XXpsk3)
///     handshake:                 2 or 3 Noise messages, one frame each
///     afterwards:                each frame is one ChaCha20-Poly1305 message, plaintext = <type byte> <body>
///         0x01  a whole JSON message
///         0x02  a JSON fragment: <flags: 1 = last> <bytes>     (messages over 60 kB)
///         0x03  a file chunk: <transfer id: 16> <offset: 8, big endian> <bytes>
public enum LinkWire {
    public static let version: UInt8 = 1
    public static let modeReconnect: UInt8 = 1
    public static let modePair: UInt8 = 2
    public static let preamble = Data([0x53, 0x4C])
    public static let typeJSON: UInt8 = 1
    public static let typeFragment: UInt8 = 2
    public static let typeChunk: UInt8 = 3
    public static let maxPlain = noiseMaxMessage - 16 - 1
    public static let fragmentSize = 60000
    public static let chunkSize = 32 * 1024
    public static let maxMessageBytes = 32 * 1024 * 1024

    public static func prologue(mode: UInt8) -> Data {
        var d = Data("SAINT-LINK/\(version)/".utf8)
        d.append(mode)
        return d
    }

    public static func frame(_ payload: Data) throws -> Data {
        guard payload.count <= noiseMaxMessage else { throw LinkError.badMessage("frame too large") }
        var out = Data()
        out.append(UInt8(payload.count >> 8))
        out.append(UInt8(payload.count & 0xFF))
        out.append(payload)
        return out
    }

    public static func encodeJSON(_ object: JSONObject) throws -> Data {
        guard JSONSerialization.isValidJSONObject(object) else { throw LinkError.badMessage("not JSON") }
        return try JSONSerialization.data(withJSONObject: object, options: [])
    }

    public static func decodeJSON(_ data: Data) throws -> JSONObject {
        guard let object = try? JSONSerialization.jsonObject(with: data, options: []),
              let dict = object as? JSONObject else { throw LinkError.badMessage("not a JSON object") }
        return dict
    }
}

/// Collects bytes from a stream into whole frames.
public struct FrameDecoder {
    private var buffer = Data()

    public init() {}

    public mutating func feed(_ data: Data) -> [Data] {
        buffer.append(data)
        var frames: [Data] = []
        while buffer.count >= 2 {
            let length = Int(buffer[buffer.startIndex]) << 8 | Int(buffer[buffer.startIndex + 1])
            guard buffer.count >= 2 + length else { break }
            frames.append(Data(buffer[(buffer.startIndex + 2)..<(buffer.startIndex + 2 + length)]))
            buffer = Data(buffer[(buffer.startIndex + 2 + length)...])
        }
        return frames
    }

    public var pendingBytes: Int { buffer.count }
}

public enum IncomingMessage {
    case json(JSONObject)
    case chunk(transferID: Data, offset: UInt64, data: Data)
}

/// The encrypted stream after the handshake: turns messages into frames and back.
public final class SecureChannel {
    private let send: CipherState
    private let receive: CipherState
    private var fragments = Data()
    public let remoteStatic: Data

    public init(send: CipherState, receive: CipherState, remoteStatic: Data) {
        self.send = send
        self.receive = receive
        self.remoteStatic = remoteStatic
    }

    private func sealFrame(_ plain: Data) throws -> Data {
        try LinkWire.frame(try send.encrypt(ad: Data(), plaintext: plain))
    }

    /// Wire bytes (already framed) for one JSON message, in order.
    public func encode(json object: JSONObject) throws -> [Data] {
        let body = try LinkWire.encodeJSON(object)
        guard body.count <= LinkWire.maxMessageBytes else { throw LinkError.badMessage("message too large") }
        if body.count <= LinkWire.maxPlain {
            var plain = Data([LinkWire.typeJSON])
            plain.append(body)
            return [try sealFrame(plain)]
        }
        var frames: [Data] = []
        var offset = 0
        while offset < body.count {
            let end = min(offset + LinkWire.fragmentSize, body.count)
            var plain = Data([LinkWire.typeFragment, end >= body.count ? 1 : 0])
            plain.append(body[(body.startIndex + offset)..<(body.startIndex + end)])
            frames.append(try sealFrame(plain))
            offset = end
        }
        return frames
    }

    public func encodeChunk(transferID: Data, offset: UInt64, data: Data) throws -> Data {
        guard transferID.count == 16 else { throw LinkError.badMessage("bad transfer id") }
        var plain = Data([LinkWire.typeChunk])
        plain.append(transferID)
        var be = offset.bigEndian
        withUnsafeBytes(of: &be) { plain.append(contentsOf: $0) }
        plain.append(data)
        return try sealFrame(plain)
    }

    /// One received frame; nil while a fragmented message is still arriving.
    public func decode(frame: Data) throws -> IncomingMessage? {
        let plain: Data
        do {
            plain = try receive.decrypt(ad: Data(), ciphertext: frame)
        } catch {
            throw LinkError.badMessage("a message failed authentication")
        }
        guard let kind = plain.first else { throw LinkError.badMessage("empty message") }
        let body = Data(plain.dropFirst())
        switch kind {
        case LinkWire.typeJSON:
            return .json(try LinkWire.decodeJSON(body))
        case LinkWire.typeFragment:
            guard let flags = body.first else { throw LinkError.badMessage("bad fragment") }
            fragments.append(body.dropFirst())
            if fragments.count > LinkWire.maxMessageBytes { throw LinkError.badMessage("message too large") }
            if flags & 1 == 1 {
                let whole = fragments
                fragments = Data()
                return .json(try LinkWire.decodeJSON(whole))
            }
            return nil
        case LinkWire.typeChunk:
            guard body.count >= 24 else { throw LinkError.badMessage("bad chunk") }
            let id = Data(body[0..<16])
            var offset: UInt64 = 0
            for i in 16..<24 { offset = offset << 8 | UInt64(body[i]) }
            return .chunk(transferID: id, offset: offset, data: Data(body[24...]))
        default:
            throw LinkError.badMessage("unknown message type")
        }
    }
}

/// The initiator side of both handshakes, without any I/O: feed it what arrives, send what it returns.
public final class ClientHandshake {
    public enum Step {
        /// The handshake is finished and the secure channel is ready. `hello` is the peer's hello (IK only).
        case ready(SecureChannel, hello: JSONObject?)
        /// Send these bytes, after which the channel is ready (pairing: the last of three messages).
        case sendAndReady(Data, SecureChannel)
    }

    private let state: HandshakeState
    private let hello: JSONObject
    private let mode: UInt8

    public init(staticPrivate: Data, hello: JSONObject, remoteStatic: Data?, psk: Data?,
                ephemeralPrivate: Data? = nil) throws {
        self.hello = hello
        if psk != nil {
            mode = LinkWire.modePair
            state = try HandshakeState(pattern: .xxpsk3, initiator: true, staticPrivate: staticPrivate,
                                       remoteStatic: nil, psk: psk, prologue: LinkWire.prologue(mode: LinkWire.modePair),
                                       ephemeralPrivate: ephemeralPrivate)
        } else {
            mode = LinkWire.modeReconnect
            state = try HandshakeState(pattern: .ik, initiator: true, staticPrivate: staticPrivate,
                                       remoteStatic: remoteStatic, psk: nil,
                                       prologue: LinkWire.prologue(mode: LinkWire.modeReconnect),
                                       ephemeralPrivate: ephemeralPrivate)
        }
    }

    /// The first bytes to send: the preamble and message 1.
    public func begin() throws -> Data {
        var out = LinkWire.preamble
        out.append(LinkWire.version)
        out.append(mode)
        if mode == LinkWire.modeReconnect {
            out.append(try LinkWire.frame(try state.writeMessage(payload: try LinkWire.encodeJSON(hello))))
        } else {
            out.append(try LinkWire.frame(try state.writeMessage()))
        }
        return out
    }

    public func receive(frame: Data) throws -> Step {
        do {
            if mode == LinkWire.modeReconnect {
                let payload = try state.readMessage(frame)
                let (send, recv) = try state.split()
                guard let rs = state.remoteStatic else { throw LinkError.handshake("no remote key") }
                let channel = SecureChannel(send: send, receive: recv, remoteStatic: rs)
                return .ready(channel, hello: try? LinkWire.decodeJSON(payload))
            }
            _ = try state.readMessage(frame)
            let third = try LinkWire.frame(try state.writeMessage(payload: try LinkWire.encodeJSON(hello)))
            let (send, recv) = try state.split()
            guard let rs = state.remoteStatic else { throw LinkError.handshake("no remote key") }
            return .sendAndReady(third, SecureChannel(send: send, receive: recv, remoteStatic: rs))
        } catch let error as NoiseError {
            throw LinkError.handshake("\(error)")
        }
    }
}

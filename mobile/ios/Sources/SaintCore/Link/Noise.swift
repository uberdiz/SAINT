import Foundation

public enum NoiseError: Error, Equatable {
    case decryptionFailed
    case outOfTurn
    case shortMessage
    case tooLarge
    case badPattern
    case missingKey
}

/// The Noise Protocol Framework (revision 34), reduced to SAINT Link's two handshakes:
///
///     Noise_IK_25519_ChaChaPoly_SHA256        reconnecting to a paired device
///     Noise_XXpsk3_25519_ChaChaPoly_SHA256    first pairing (psk = the one-time pairing code)
///
/// A port of modules/link/noise.py; the byte-exact vectors in link_vectors.json keep them in step.
public let noiseMaxMessage = 65535
private let dhLength = 32
private let tagLength = 16

func noiseNonce(_ n: UInt64) -> Data {
    var d = Data(count: 4)
    var value = n.littleEndian
    withUnsafeBytes(of: &value) { d.append(contentsOf: $0) }
    return d
}

private func noiseHKDF(chainingKey: Data, ikm: Data, outputs: Int) -> [Data] {
    let all = SaintCrypto.hkdf(ikm: ikm, salt: chainingKey, info: Data(), length: 32 * outputs)
    return (0..<outputs).map { Data(all[(all.startIndex + $0 * 32)..<(all.startIndex + ($0 + 1) * 32)]) }
}

public final class CipherState {
    var key: Data?
    public private(set) var counter: UInt64 = 0

    public init(key: Data? = nil) { self.key = key }

    var hasKey: Bool { key != nil }

    public func encrypt(ad: Data, plaintext: Data) throws -> Data {
        guard let key = key else { return plaintext }
        let out = try SaintCrypto.seal(key: key, nonce: noiseNonce(counter), plaintext: plaintext, aad: ad)
        counter += 1
        return out
    }

    public func decrypt(ad: Data, ciphertext: Data) throws -> Data {
        guard let key = key else { return ciphertext }
        do {
            let out = try SaintCrypto.open(key: key, nonce: noiseNonce(counter), sealed: ciphertext, aad: ad)
            counter += 1
            return out
        } catch {
            throw NoiseError.decryptionFailed
        }
    }
}

final class SymmetricState {
    var h: Data
    var ck: Data
    var cipher = CipherState()

    init(protocolName: Data) {
        if protocolName.count <= 32 {
            var padded = protocolName
            padded.append(Data(count: 32 - protocolName.count))
            h = padded
        } else {
            h = SaintCrypto.sha256(protocolName)
        }
        ck = h
    }

    func mixKey(_ ikm: Data) {
        let out = noiseHKDF(chainingKey: ck, ikm: ikm, outputs: 2)
        ck = out[0]
        cipher = CipherState(key: out[1])
    }

    func mixHash(_ data: Data) {
        var input = h
        input.append(data)
        h = SaintCrypto.sha256(input)
    }

    func mixKeyAndHash(_ ikm: Data) {
        let out = noiseHKDF(chainingKey: ck, ikm: ikm, outputs: 3)
        ck = out[0]
        mixHash(out[1])
        cipher = CipherState(key: out[2])
    }

    func encryptAndHash(_ plaintext: Data) throws -> Data {
        let ct = try cipher.encrypt(ad: h, plaintext: plaintext)
        mixHash(ct)
        return ct
    }

    func decryptAndHash(_ ciphertext: Data) throws -> Data {
        let pt = try cipher.decrypt(ad: h, ciphertext: ciphertext)
        mixHash(ciphertext)
        return pt
    }

    func split() -> (CipherState, CipherState) {
        let out = noiseHKDF(chainingKey: ck, ikm: Data(), outputs: 2)
        return (CipherState(key: out[0]), CipherState(key: out[1]))
    }
}

public enum NoisePattern {
    case ik
    case xxpsk3

    var name: Data {
        switch self {
        case .ik: return Data("Noise_IK_25519_ChaChaPoly_SHA256".utf8)
        case .xxpsk3: return Data("Noise_XXpsk3_25519_ChaChaPoly_SHA256".utf8)
        }
    }

    var messages: [[String]] {
        switch self {
        case .ik: return [["e", "es", "s", "ss"], ["e", "ee", "se"]]
        case .xxpsk3: return [["e"], ["e", "ee", "s", "es"], ["s", "se", "psk"]]
        }
    }

    var usesPsk: Bool { self == .xxpsk3 }
}

public final class HandshakeState {
    public let pattern: NoisePattern
    public let initiator: Bool
    private let ss: SymmetricState
    private let staticPrivate: Data
    private let staticPublic: Data
    private var ephemeralPrivate: Data?
    private var ephemeralPublic: Data?
    private let fixedEphemeral: Data?
    public private(set) var remoteStatic: Data?
    private var remoteEphemeral: Data?
    private let psk: Data?
    private var index = 0

    public init(pattern: NoisePattern, initiator: Bool, staticPrivate: Data, remoteStatic: Data? = nil,
                psk: Data? = nil, prologue: Data = Data(), ephemeralPrivate: Data? = nil) throws {
        self.pattern = pattern
        self.initiator = initiator
        self.staticPrivate = staticPrivate
        self.staticPublic = try SaintCrypto.publicKey(forPrivate: staticPrivate)
        self.remoteStatic = remoteStatic
        self.psk = psk
        self.fixedEphemeral = ephemeralPrivate
        if pattern.usesPsk, psk?.count != 32 { throw NoiseError.missingKey }
        ss = SymmetricState(protocolName: pattern.name)
        ss.mixHash(prologue)
        if pattern == .ik {                              // <- s  (pre-message)
            if initiator {
                guard let rs = remoteStatic else { throw NoiseError.missingKey }
                ss.mixHash(rs)
            } else {
                ss.mixHash(staticPublic)
            }
        }
    }

    public var isFinished: Bool { index >= pattern.messages.count }
    public var myTurnToWrite: Bool { !isFinished && ((index % 2 == 0) == initiator) }

    private func dh(_ token: String) throws -> Data {
        switch token {
        case "ee":
            guard let e = ephemeralPrivate, let re = remoteEphemeral else { throw NoiseError.missingKey }
            return try SaintCrypto.sharedSecret(privateKey: e, peerPublic: re)
        case "ss":
            guard let rs = remoteStatic else { throw NoiseError.missingKey }
            return try SaintCrypto.sharedSecret(privateKey: staticPrivate, peerPublic: rs)
        case "es":   // the initiator's ephemeral with the responder's static
            if initiator {
                guard let e = ephemeralPrivate, let rs = remoteStatic else { throw NoiseError.missingKey }
                return try SaintCrypto.sharedSecret(privateKey: e, peerPublic: rs)
            }
            guard let re = remoteEphemeral else { throw NoiseError.missingKey }
            return try SaintCrypto.sharedSecret(privateKey: staticPrivate, peerPublic: re)
        case "se":   // the initiator's static with the responder's ephemeral
            if initiator {
                guard let re = remoteEphemeral else { throw NoiseError.missingKey }
                return try SaintCrypto.sharedSecret(privateKey: staticPrivate, peerPublic: re)
            }
            guard let e = ephemeralPrivate, let rs = remoteStatic else { throw NoiseError.missingKey }
            return try SaintCrypto.sharedSecret(privateKey: e, peerPublic: rs)
        default:
            throw NoiseError.badPattern
        }
    }

    public func writeMessage(payload: Data = Data()) throws -> Data {
        guard myTurnToWrite else { throw NoiseError.outOfTurn }
        var out = Data()
        for token in pattern.messages[index] {
            switch token {
            case "e":
                let priv = fixedEphemeral ?? SaintCrypto.generateKeyPair().privateKey
                ephemeralPrivate = priv
                let pub = try SaintCrypto.publicKey(forPrivate: priv)
                ephemeralPublic = pub
                out.append(pub)
                ss.mixHash(pub)
                if pattern.usesPsk { ss.mixKey(pub) }
            case "s":
                out.append(try ss.encryptAndHash(staticPublic))
            case "psk":
                guard let psk = psk else { throw NoiseError.missingKey }
                ss.mixKeyAndHash(psk)
            default:
                ss.mixKey(try dh(token))
            }
        }
        out.append(try ss.encryptAndHash(payload))
        if out.count > noiseMaxMessage { throw NoiseError.tooLarge }
        index += 1
        return out
    }

    public func readMessage(_ message: Data) throws -> Data {
        guard !isFinished, !myTurnToWrite else { throw NoiseError.outOfTurn }
        if message.count > noiseMaxMessage { throw NoiseError.tooLarge }
        let bytes = Data(message)            // re-base indices at 0
        var pos = 0
        for token in pattern.messages[index] {
            switch token {
            case "e":
                guard bytes.count - pos >= dhLength else { throw NoiseError.shortMessage }
                let re = Data(bytes[pos..<(pos + dhLength)])
                pos += dhLength
                remoteEphemeral = re
                ss.mixHash(re)
                if pattern.usesPsk { ss.mixKey(re) }
            case "s":
                let size = dhLength + (ss.cipher.hasKey ? tagLength : 0)
                guard bytes.count - pos >= size else { throw NoiseError.shortMessage }
                remoteStatic = try ss.decryptAndHash(Data(bytes[pos..<(pos + size)]))
                pos += size
            case "psk":
                guard let psk = psk else { throw NoiseError.missingKey }
                ss.mixKeyAndHash(psk)
            default:
                ss.mixKey(try dh(token))
            }
        }
        let payload = try ss.decryptAndHash(Data(bytes[pos...]))
        index += 1
        return payload
    }

    /// (send, receive) cipher states for this side.
    public func split() throws -> (send: CipherState, receive: CipherState) {
        guard isFinished else { throw NoiseError.outOfTurn }
        let (c1, c2) = ss.split()
        return initiator ? (c1, c2) : (c2, c1)
    }

    public var handshakeHash: Data { ss.h }
}

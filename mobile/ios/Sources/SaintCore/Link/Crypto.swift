import Foundation
#if canImport(CryptoKit)
import CryptoKit
#else
import Crypto
#endif

public enum CryptoError: Error, Equatable {
    case malformed
    case authFailed
    case zeroSharedSecret
    case badKey
}

/// The three primitives SAINT Link needs, on CryptoKit: X25519, HKDF-SHA256, ChaCha20-Poly1305.
/// They are the same RFC algorithms the desktop implements in modules/link/crypto.py; the known-answer
/// vectors in Tests/SaintCoreTests/Resources/link_vectors.json are checked on both sides.
public enum SaintCrypto {
    public static let keyLength = 32
    public static let nonceLength = 12
    public static let tagLength = 16

    // MARK: X25519

    public static func generateKeyPair() -> (privateKey: Data, publicKey: Data) {
        let key = Curve25519.KeyAgreement.PrivateKey()
        return (key.rawRepresentation, key.publicKey.rawRepresentation)
    }

    public static func publicKey(forPrivate privateKey: Data) throws -> Data {
        do {
            let key = try Curve25519.KeyAgreement.PrivateKey(rawRepresentation: privateKey)
            return key.publicKey.rawRepresentation
        } catch {
            throw CryptoError.badKey
        }
    }

    public static func sharedSecret(privateKey: Data, peerPublic: Data) throws -> Data {
        let secretBytes: Data
        do {
            let priv = try Curve25519.KeyAgreement.PrivateKey(rawRepresentation: privateKey)
            let pub = try Curve25519.KeyAgreement.PublicKey(rawRepresentation: peerPublic)
            let secret = try priv.sharedSecretFromKeyAgreement(with: pub)
            secretBytes = secret.withUnsafeBytes { Data($0) }
        } catch {
            throw CryptoError.badKey
        }
        if secretBytes.allSatisfy({ $0 == 0 }) { throw CryptoError.zeroSharedSecret }
        return secretBytes
    }

    // MARK: hashes

    public static func sha256(_ data: Data) -> Data {
        Data(SHA256.hash(data: data))
    }

    /// SHA-256 of a file, read a megabyte at a time.
    public static func sha256Hex(ofFile url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while true {
            let block = try handle.read(upToCount: 1 << 20) ?? Data()
            if block.isEmpty { break }
            hasher.update(data: block)
        }
        return Data(hasher.finalize()).hex
    }

    public static func hmacSHA256(key: Data, data: Data) -> Data {
        let code = HMAC<SHA256>.authenticationCode(for: data, using: SymmetricKey(data: key))
        return Data(code)
    }

    /// RFC 5869 HKDF-SHA256 (extract then expand). With an empty `info` it is exactly Noise's HKDF.
    public static func hkdf(ikm: Data, salt: Data, info: Data, length: Int) -> Data {
        let realSalt = salt.isEmpty ? Data(count: 32) : salt
        let prk = hmacSHA256(key: realSalt, data: ikm)
        var output = Data()
        var block = Data()
        var counter: UInt8 = 1
        while output.count < length {
            var input = block
            input.append(info)
            input.append(counter)
            block = hmacSHA256(key: prk, data: input)
            output.append(block)
            counter &+= 1
        }
        return output.prefix(length)
    }

    // MARK: ChaCha20-Poly1305

    /// ciphertext || 16-byte tag
    public static func seal(key: Data, nonce: Data, plaintext: Data, aad: Data = Data()) throws -> Data {
        guard key.count == keyLength, nonce.count == nonceLength else { throw CryptoError.malformed }
        do {
            let box = try ChaChaPoly.seal(plaintext, using: SymmetricKey(data: key),
                                          nonce: try ChaChaPoly.Nonce(data: nonce), authenticating: aad)
            var out = Data()
            out.append(box.ciphertext)
            out.append(box.tag)
            return out
        } catch {
            throw CryptoError.malformed
        }
    }

    public static func open(key: Data, nonce: Data, sealed: Data, aad: Data = Data()) throws -> Data {
        guard key.count == keyLength, nonce.count == nonceLength, sealed.count >= tagLength else {
            throw CryptoError.malformed
        }
        let split = sealed.count - tagLength
        let start = sealed.startIndex
        let ciphertext = Data(sealed[start..<(start + split)])
        let tag = Data(sealed[(start + split)...])
        do {
            let box = try ChaChaPoly.SealedBox(nonce: try ChaChaPoly.Nonce(data: nonce), ciphertext: ciphertext, tag: tag)
            return try ChaChaPoly.open(box, using: SymmetricKey(data: key), authenticating: aad)
        } catch {
            throw CryptoError.authFailed
        }
    }

    public static func randomBytes(_ count: Int) -> Data {
        var bytes = [UInt8](repeating: 0, count: count)
        for i in 0..<count { bytes[i] = UInt8.random(in: 0...255) }
        return Data(bytes)
    }
}

extension Data {
    public init?(hex: String) {
        let chars = Array(hex.utf8)
        guard chars.count % 2 == 0 else { return nil }
        var bytes = [UInt8]()
        bytes.reserveCapacity(chars.count / 2)
        func value(_ c: UInt8) -> UInt8? {
            switch c {
            case 48...57: return c - 48
            case 97...102: return c - 87
            case 65...70: return c - 55
            default: return nil
            }
        }
        var i = 0
        while i < chars.count {
            guard let hi = value(chars[i]), let lo = value(chars[i + 1]) else { return nil }
            bytes.append(hi << 4 | lo)
            i += 2
        }
        self.init(bytes)
    }

    public var hex: String {
        map { String(format: "%02x", $0) }.joined()
    }
}

/// A SHA-256 that is fed piece by piece (a file arriving in chunks).
public final class StreamingSHA256 {
    private var hasher = SHA256()
    public init() {}
    public func update(_ data: Data) { hasher.update(data: data) }
    public func finish() -> String { Data(hasher.finalize()).hex }
}

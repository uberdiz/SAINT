import Foundation

/// Pairing codes and links (see modules/link/identity.py).
///
/// A pairing code is 16 random bytes shown as 26 base32 characters in groups of four. It is the
/// pre-shared key of the pairing handshake, so it must be guessed in full — there is no short PIN.
public enum Pairing {
    private static let alphabet = Array("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567".utf8)

    public enum PairingError: Error, Equatable, LocalizedError {
        case badCharacters
        case wrongLength
        case notASaintLink
        case incompleteLink

        public var errorDescription: String? {
            switch self {
            case .badCharacters: return "That code has characters that aren't in a SAINT pairing code."
            case .wrongLength: return "That pairing code is the wrong length."
            case .notASaintLink: return "That isn't a SAINT pairing link."
            case .incompleteLink: return "That pairing link is incomplete."
            }
        }
    }

    // MARK: base32 (RFC 4648, no padding on output)

    public static func base32Encode(_ data: Data) -> String {
        var out = [UInt8]()
        var buffer: UInt32 = 0
        var bits = 0
        for byte in data {
            buffer = (buffer << 8) | UInt32(byte)
            bits += 8
            while bits >= 5 {
                out.append(alphabet[Int((buffer >> UInt32(bits - 5)) & 31)])
                bits -= 5
            }
            buffer &= (1 << UInt32(bits)) - 1
        }
        if bits > 0 {
            out.append(alphabet[Int((buffer << UInt32(5 - bits)) & 31)])
        }
        return String(decoding: out, as: UTF8.self)
    }

    public static func base32Decode(_ text: String) -> Data? {
        var bytes = [UInt8]()
        var buffer: UInt32 = 0
        var bits = 0
        for ch in text.utf8 {
            guard let value = alphabet.firstIndex(of: ch) else { return nil }
            buffer = (buffer << 5) | UInt32(value)
            bits += 5
            if bits >= 8 {
                bytes.append(UInt8((buffer >> UInt32(bits - 8)) & 0xFF))
                bits -= 8
                buffer &= (1 << UInt32(bits)) - 1
            }
        }
        return Data(bytes)
    }

    // MARK: codes

    public static func encodeCode(_ token: Data) -> String {
        let raw = base32Encode(token)
        var groups = [String]()
        var index = raw.startIndex
        while index < raw.endIndex {
            let end = raw.index(index, offsetBy: 4, limitedBy: raw.endIndex) ?? raw.endIndex
            groups.append(String(raw[index..<end]))
            index = end
        }
        return groups.joined(separator: "-")
    }

    /// Forgiving: spaces, dashes, lower case, 0/1/8 typed for O/I/B.
    public static func decodeCode(_ code: String) throws -> Data {
        var cleaned = ""
        for ch in code.uppercased() where !" -_.".contains(ch) {
            switch ch {
            case "0": cleaned.append("O")
            case "1": cleaned.append("I")
            case "8": cleaned.append("B")
            default: cleaned.append(ch)
            }
        }
        guard !cleaned.isEmpty, cleaned.utf8.allSatisfy({ alphabet.contains($0) }) else {
            throw PairingError.badCharacters
        }
        guard let token = base32Decode(cleaned), token.count == 16 else { throw PairingError.wrongLength }
        return token
    }

    /// The pre-shared key of the pairing handshake.
    public static func psk(forToken token: Data) -> Data {
        SaintCrypto.hkdf(ikm: token, salt: Data("SAINT-LINK-PAIRING".utf8), info: Data("psk".utf8), length: 32)
    }

    // MARK: links and addresses

    public struct PairLink: Equatable {
        public var host: String
        public var port: Int
        public var token: Data
        public var role: String          // "own" or "collaborator"
        public var name: String
    }

    /// `saint://pair?h=..&p=..&t=..&r=..&n=..` — what the QR code on the PC holds.
    public static func parse(link: String) throws -> PairLink {
        guard let comps = URLComponents(string: link.trimmingCharacters(in: .whitespacesAndNewlines)),
              comps.scheme == "saint", comps.host == "pair" else { throw PairingError.notASaintLink }
        var query = [String: String]()
        for item in comps.queryItems ?? [] { if let v = item.value { query[item.name] = v } }
        guard let host = query["h"], let portText = query["p"], let port = Int(portText), let t = query["t"] else {
            throw PairingError.incompleteLink
        }
        let token = try decodeCode(t)
        let role = query["r"] == "collaborator" ? "collaborator" : "own"
        // Python's urlencode writes spaces as "+", which URLComponents leaves alone.
        let name = (query["n"] ?? "").replacingOccurrences(of: "+", with: " ")
        return PairLink(host: host, port: port, token: token, role: role, name: name)
    }

    /// "192.168.1.20:8765", "192.168.1.20", "[fe80::1]:8765" -> (host, port)
    public static func parse(address: String, defaultPort: Int = 8765) -> (host: String, port: Int) {
        let t = address.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.hasPrefix("[") {
            if let close = t.firstIndex(of: "]") {
                let host = String(t[t.index(after: t.startIndex)..<close])
                let rest = t[t.index(after: close)...]
                if rest.hasPrefix(":"), let port = Int(rest.dropFirst()) { return (host, port) }
                return (host, defaultPort)
            }
        }
        let parts = t.split(separator: ":", omittingEmptySubsequences: false)
        if parts.count == 2, let port = Int(parts[1]) { return (String(parts[0]), port) }
        return (t, defaultPort)
    }

    /// The device id of a public key: the first 16 hex characters of its SHA-256.
    public static func deviceID(forPublicKey key: Data) -> String {
        String(SaintCrypto.sha256(key).hex.prefix(16))
    }
}

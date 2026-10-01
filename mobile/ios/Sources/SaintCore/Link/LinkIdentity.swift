import Foundation

/// Where the phone keeps its private key. The app uses the Keychain; tests use memory.
public protocol SecretStore: AnyObject {
    func load(account: String) -> Data?
    func save(_ data: Data, account: String)
}

public final class MemorySecretStore: SecretStore {
    private var items: [String: Data] = [:]
    public init() {}
    public func load(account: String) -> Data? { items[account] }
    public func save(_ data: Data, account: String) { items[account] = data }
}

/// This device on the link: a long-lived X25519 key. The device id is the first 16 hex characters of the SHA-256
/// of the public key, so an id can't be claimed without the key.
public struct LinkIdentity {
    public let privateKey: Data
    public let publicKey: Data
    public var name: String

    public var deviceID: String { Pairing.deviceID(forPublicKey: publicKey) }

    public init(privateKey: Data, publicKey: Data, name: String) {
        self.privateKey = privateKey
        self.publicKey = publicKey
        self.name = name
    }

    public static let account = "saint.link.identity.v1"

    public static func loadOrCreate(secrets: SecretStore, name: String) -> LinkIdentity {
        if let stored = secrets.load(account: account), stored.count == 32,
           let publicKey = try? SaintCrypto.publicKey(forPrivate: stored) {
            return LinkIdentity(privateKey: stored, publicKey: publicKey, name: name)
        }
        let pair = SaintCrypto.generateKeyPair()
        secrets.save(pair.privateKey, account: account)
        return LinkIdentity(privateKey: pair.privateKey, publicKey: pair.publicKey, name: name)
    }

    /// What this device says about itself inside the handshake.
    public func hello(role: String? = nil) -> JSONObject {
        var h: JSONObject = ["id": deviceID, "name": String(name.prefix(40)), "platform": "ios", "v": 1]
        if let role = role { h["role"] = role }
        return h
    }
}

/// A device this phone has paired with.
public struct LinkPeer: Codable, Equatable, Identifiable {
    public var id: String
    public var name: String
    public var publicKey: String               // hex
    public var role: String                    // "own" (your device) or "collaborator" (a friend's SAINT)
    public var platform: String
    public var host: String
    public var port: Int
    public var nicknames: [String]
    public var autoConnect: Bool
    public var added: Date
    public var lastSeen: Date?

    public init(id: String, name: String, publicKey: String, role: String, platform: String = "", host: String = "",
                port: Int = 0, nicknames: [String] = [], autoConnect: Bool = true, added: Date = Date(), lastSeen: Date? = nil) {
        self.id = id
        self.name = name
        self.publicKey = publicKey
        self.role = role
        self.platform = platform
        self.host = host
        self.port = port
        self.nicknames = nicknames
        self.autoConnect = autoConnect
        self.added = added
        self.lastSeen = lastSeen
    }

    public var key: Data { Data(hex: publicKey) ?? Data() }
    public var isOwn: Bool { role == "own" }
}

public final class PeerStore {
    private let file: JSONFile<[LinkPeer]>

    public init(directory: URL?) {
        file = JSONFile(url: directory?.appendingPathComponent("peers.json"), empty: [])
    }

    public func all() -> [LinkPeer] {
        file.read { (list: [LinkPeer]) -> [LinkPeer] in list.sorted { $0.added < $1.added } }
    }

    public func get(_ id: String) -> LinkPeer? {
        file.read { (list: [LinkPeer]) -> LinkPeer? in list.first { $0.id == id } }
    }

    public func save(_ peer: LinkPeer) {
        file.write { (list: inout [LinkPeer]) -> Void in
            if let i = list.firstIndex(where: { $0.id == peer.id }) {
                var merged = peer
                merged.nicknames = list[i].nicknames.isEmpty ? peer.nicknames : list[i].nicknames   // re-pairing keeps what you set
                merged.added = list[i].added
                list[i] = merged
            } else {
                list.append(peer)
            }
        }
    }

    public func update(_ id: String, _ change: (inout LinkPeer) -> Void) {
        file.write { (list: inout [LinkPeer]) -> Void in
            if let i = list.firstIndex(where: { $0.id == id }) { change(&list[i]) }
        }
    }

    public func remove(_ id: String) {
        file.write { (list: inout [LinkPeer]) -> Void in list.removeAll { $0.id == id } }
    }
}

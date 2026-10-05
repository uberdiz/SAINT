import Foundation
import Security
import SaintCore

/// Secrets live in the Keychain, never in UserDefaults or the sync data: the link's private key, the Spotify
/// refresh token, an optional Claude API key.
final class Keychain: SecretStore {
    private let service = "app.saint.mobile"

    func load(account: String) -> Data? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess else { return nil }
        return item as? Data
    }

    func save(_ data: Data, account: String) {
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(base as CFDictionary)
        var add = base
        add[kSecValueData as String] = data
        // available after the first unlock, so the always-listening app can reconnect while the phone is locked
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(add as CFDictionary, nil)
    }

    func delete(account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
    }

    func string(_ account: String) -> String? {
        load(account: account).flatMap { String(data: $0, encoding: .utf8) }
    }

    func setString(_ value: String?, account: String) {
        if let value = value, !value.isEmpty { save(Data(value.utf8), account: account) } else { delete(account: account) }
    }
}

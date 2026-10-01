import Foundation

public extension Notification.Name {
    /// Posted after any store changes, so screens can refresh and the link can sync soon.
    static let saintDataChanged = Notification.Name("SaintDataChanged")
}

public enum Ids {
    /// A random lower-case hex id (the desktop uses 8 for skills and scenes, 16 for memory).
    public static func make(_ length: Int = 8) -> String {
        let hex = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        return String(hex.prefix(length))
    }
}

/// A small value kept in memory and rewritten atomically to one JSON file after every change.
final class JSONFile<Value: Codable> {
    private let url: URL?
    private let lock = NSRecursiveLock()
    private var value: Value

    init(url: URL?, empty: Value) {
        self.url = url
        self.value = empty
        if let url = url, let data = try? Data(contentsOf: url),
           let decoded = try? JSONDecoder().decode(Value.self, from: data) {
            self.value = decoded
        }
    }

    func read<R>(_ body: (Value) -> R) -> R {
        lock.lock()
        defer { lock.unlock() }
        return body(value)
    }

    @discardableResult
    func write<R>(announce: Bool = true, _ body: (inout Value) -> R) -> R {
        lock.lock()
        let result = body(&value)
        save()
        lock.unlock()
        if announce { NotificationCenter.default.post(name: .saintDataChanged, object: nil) }
        return result
    }

    private func save() {
        guard let url = url, let data = try? JSONEncoder().encode(value) else { return }
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: url, options: .atomic)
    }
}

public extension String {
    var trimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }
}

import Foundation

/// The app's own log — connections, syncs, errors, voice problems — as numbered lines kept in memory, so the PC
/// collects it over SAINT Link (`log.get`) into one folder with every device's log (desktop: modules/link/logs.py).
/// The reader asks for "lines after N"; `boot` changes each launch, so after a restart it reads from the start.
public final class AppLog {
    public static let shared = AppLog()

    public let boot: String
    private let capacity: Int
    private var lines: [(seq: Int, text: String)] = []
    private var seq = 0
    private let lock = NSLock()
    private let formatter: DateFormatter

    public init(capacity: Int = 3000) {
        self.capacity = max(10, capacity)
        boot = String(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(12)).lowercased()
        formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
    }

    /// One line, in the desktop log's format: `2026-10-06 10:57:33 [INFO] ios.link: connected to Home PC`.
    public func log(_ level: String, _ category: String, _ message: String) {
        lock.lock()
        let line = "\(formatter.string(from: Date())) [\(level)] ios.\(category): \(message)"
        seq += 1
        lines.append((seq: seq, text: line))
        if lines.count > capacity { lines.removeFirst(lines.count - capacity) }
        lock.unlock()
        #if DEBUG
        print(line)
        #endif
    }

    public func info(_ category: String, _ message: String) { log("INFO", category, message) }
    public func warning(_ category: String, _ message: String) { log("WARNING", category, message) }
    public func error(_ category: String, _ message: String) { log("ERROR", category, message) }

    /// The answer to `log.get {after, boot}`: `{lines, cursor, dropped, boot}`.
    public func since(after: Int, boot other: String = "", limit: Int = 2000) -> JSONObject {
        lock.lock()
        defer { lock.unlock() }
        var from = max(0, after)
        if (!other.isEmpty && other != boot) || from > seq { from = 0 }
        let first = lines.first?.seq ?? seq + 1
        let out = lines.filter { $0.seq > from }.prefix(limit).map { $0.text }
        let cursor = min(seq, max(from, first - 1) + out.count)
        let dropped = max(0, first - 1 - from)
        return ["lines": Array(out), "cursor": cursor, "dropped": dropped, "boot": boot]
    }
}

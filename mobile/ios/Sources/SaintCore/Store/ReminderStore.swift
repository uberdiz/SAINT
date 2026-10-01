import Foundation

/// A schedule is saved as its JSON text, the same shape the desktop syncs.
extension Schedule: Codable {
    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let text = try container.decode(String.self)
        guard let raw = text.data(using: .utf8),
              let object = (try? JSONSerialization.jsonObject(with: raw)) as? JSONObject,
              let schedule = Schedule(json: object) else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "not a schedule")
        }
        self = schedule
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(SyncEngine.canonical(json))
    }
}

public enum ReminderStatus {
    public static let active = "active"
    public static let paused = "paused"
    public static let completed = "completed"
    public static let cancelled = "cancelled"
    public static let missed = "missed"
}

public struct Reminder: Codable, Equatable, Identifiable {
    public var id: String
    public var title: String
    public var message: String
    public var schedule: Schedule
    public var status: String
    public var lastRun: Date?
    public var created: Date
    public var isTimer: Bool

    public init(id: String = Ids.make(8), title: String? = nil, message: String, schedule: Schedule,
                status: String = ReminderStatus.active, lastRun: Date? = nil, created: Date = Date(), isTimer: Bool = false) {
        self.id = id
        self.title = title ?? String(message.prefix(60))
        self.message = message
        self.schedule = schedule
        self.status = status
        self.lastRun = lastRun
        self.created = created
        self.isTimer = isTimer
    }

    public var isActive: Bool { status == ReminderStatus.active }
}

/// What the app should ask iOS to deliver: reminders are local notifications, so they ring even when SAINT
/// isn't running. The store only describes them; the app turns each into a UNNotificationRequest.
public struct NotificationPlan: Equatable {
    public enum Trigger: Equatable {
        case at(Date)
        case every(seconds: Int)
        /// weekdays use Apple's numbering (1 = Sunday ... 7 = Saturday); empty means every day
        case daily(hour: Int, minute: Int, weekdays: [Int])
    }
    public var id: String
    public var title: String
    public var body: String
    public var trigger: Trigger
}

public final class ReminderStore: SyncAdapter {
    public let kind = "reminder"
    private let file: JSONFile<[Reminder]>
    /// A reminder that came due more than this long ago is not announced aloud (its notification already rang).
    public static let staleAfter: TimeInterval = 120

    public init(directory: URL?) {
        file = JSONFile(url: directory?.appendingPathComponent("reminders.json"), empty: [])
    }

    public func all() -> [Reminder] {
        file.read { (list: [Reminder]) -> [Reminder] in list.sorted { $0.created > $1.created } }
    }

    public func upcoming(now: Date = Date()) -> [(reminder: Reminder, due: Date)] {
        var out: [(reminder: Reminder, due: Date)] = []
        for r in all() where r.isActive {
            if let due = TimeParse.nextRun(r.schedule, after: r.lastRun ?? r.created.addingTimeInterval(-1)),
               due > now.addingTimeInterval(-ReminderStore.staleAfter) {
                out.append((reminder: r, due: due))
            }
        }
        out.sort { $0.due < $1.due }
        return out
    }

    @discardableResult
    public func add(message: String, schedule: Schedule, isTimer: Bool = false, title: String? = nil,
                    created: Date = Date()) -> Reminder {
        let reminder = Reminder(title: title, message: message, schedule: schedule, created: created, isTimer: isTimer)
        file.write { (list: inout [Reminder]) -> Void in list.append(reminder) }
        return reminder
    }

    @discardableResult
    public func cancel(id: String) -> Bool {
        file.write { (list: inout [Reminder]) -> Bool in
            guard let i = list.firstIndex(where: { $0.id == id }) else { return false }
            list[i].status = ReminderStatus.cancelled
            return true
        }
    }

    @discardableResult
    public func cancelAll() -> Int {
        file.write { (list: inout [Reminder]) -> Int in
            var n = 0
            for i in list.indices where list[i].isActive {
                list[i].status = ReminderStatus.cancelled
                n += 1
            }
            return n
        }
    }

    /// Cancel the active reminders whose text mentions ``words``.
    @discardableResult
    public func cancel(matching words: String) -> [Reminder] {
        let wanted = MemoryStore.keywords(words)
        if wanted.isEmpty { return [] }
        let hits = all().filter { r in
            r.isActive && wanted.allSatisfy { MemoryStore.keywords(r.title + " " + r.message).contains($0) }
        }
        for h in hits { cancel(id: h.id) }
        return hits
    }

    public func remove(id: String) {
        file.write { (list: inout [Reminder]) -> Void in list.removeAll { $0.id == id } }
    }

    /// Reminders that have come due. Once-only ones are completed, repeating ones advance; anything that came
    /// due more than ``staleAfter`` ago is advanced quietly (the notification already told you).
    public func tick(now: Date = Date()) -> [Reminder] {
        file.write(announce: false) { (list: inout [Reminder]) -> [Reminder] in
            var fired: [Reminder] = []
            for i in list.indices where list[i].isActive {
                let r = list[i]
                guard var due = TimeParse.nextRun(r.schedule, after: r.lastRun ?? r.created.addingTimeInterval(-1)) else {
                    list[i].status = ReminderStatus.completed
                    continue
                }
                if due > now { continue }
                // Catch up through occurrences that passed while SAINT wasn't running: only the latest can be announced.
                var latest = due
                var steps = 0
                while due <= now && steps < 500 {
                    latest = due
                    steps += 1
                    guard let next = TimeParse.nextRun(r.schedule, after: due) else { break }
                    due = next
                }
                list[i].lastRun = steps >= 500 ? now : latest
                if !r.schedule.isRecurring { list[i].status = ReminderStatus.completed }
                if now.timeIntervalSince(latest) <= ReminderStore.staleAfter { fired.append(list[i]) }
            }
            return fired
        }
    }

    public func notificationPlans(now: Date = Date()) -> [NotificationPlan] {
        var plans: [NotificationPlan] = []
        for r in all() where r.isActive {
            let body = r.isTimer ? "Time's up." : r.message
            let title = r.isTimer ? "Timer" : "Reminder"
            switch r.schedule {
            case .once(let at):
                if at > now { plans.append(NotificationPlan(id: r.id, title: title, body: body, trigger: .at(at))) }
            case .interval(let every, _):
                plans.append(NotificationPlan(id: r.id, title: title, body: body, trigger: .every(seconds: max(60, every))))
            case .daily(let time, let days):
                let parts = time.split(separator: ":").compactMap { Int($0) }
                let weekdays = (days ?? []).map { ($0 + 1) % 7 + 1 }       // Monday = 0 here, Sunday = 1 for Apple
                plans.append(NotificationPlan(id: r.id, title: title, body: body,
                                              trigger: .daily(hour: parts.first ?? 9, minute: parts.count > 1 ? parts[1] : 0,
                                                              weekdays: weekdays)))
            }
        }
        return plans
    }

    // MARK: SyncAdapter

    public func snapshot() -> [String: JSONObject] {
        let cutoff = Date().addingTimeInterval(-30 * 86400)
        var out: [String: JSONObject] = [:]
        for r in all() {
            if !(r.isActive || r.status == ReminderStatus.paused) && (r.lastRun ?? r.created) < cutoff { continue }
            out[r.id] = ["title": r.title, "message": r.message, "schedule": r.schedule.json, "status": r.status,
                         "last_run": Int((r.lastRun?.timeIntervalSince1970 ?? 0).rounded())]
        }
        return out
    }

    public func fingerprint(_ data: JSONObject) -> String {
        var d = data
        let ran = ((d["last_run"] as? NSNumber)?.intValue ?? 0) != 0
        d["last_run"] = nil
        d["ran"] = ran                       // that it ran matters; exactly when doesn't
        return SyncEngine.fingerprint(d)
    }

    public func apply(changes: [(uid: String, data: JSONObject?)]) -> [String] {
        file.write { (list: inout [Reminder]) -> [String] in
            for change in changes {
                guard let data = change.data else {
                    list.removeAll { $0.id == change.uid }
                    continue
                }
                let index = list.firstIndex(where: { $0.id == change.uid })
                let schedule = (data["schedule"] as? JSONObject).flatMap { Schedule(json: $0) } ?? index.map { list[$0].schedule }
                guard let sched = schedule else { continue }
                var r = index.map { list[$0] } ?? Reminder(id: change.uid, message: (data["message"] as? String) ?? "", schedule: sched)
                r.title = (data["title"] as? String) ?? r.title
                r.message = (data["message"] as? String) ?? r.message
                r.schedule = sched
                r.status = (data["status"] as? String) ?? ReminderStatus.active
                let lastRun = (data["last_run"] as? NSNumber)?.doubleValue ?? 0
                if lastRun > 0 { r.lastRun = Date(timeIntervalSince1970: lastRun) }
                if r.status == ReminderStatus.active, TimeParse.nextRun(r.schedule, after: Date().addingTimeInterval(-1)) == nil {
                    r.status = ReminderStatus.missed
                }
                if let i = index { list[i] = r } else { list.append(r) }
            }
            return []
        }
    }
}

import Foundation

/// A reminder schedule, in the same three shapes the desktop stores (modules/automation/timeparse.py):
///     once      {"type": "once", "at": <epoch seconds>}
///     interval  {"type": "interval", "every_sec": N, "start": <epoch>}
///     daily     {"type": "daily", "time": "HH:MM", "days": [0..6] | null}     (0 = Monday)
public enum Schedule: Equatable {
    case once(at: Date)
    case interval(everySec: Int, start: Date)
    case daily(time: String, days: [Int]?)

    public var json: JSONObject {
        switch self {
        case .once(let at): return ["type": "once", "at": at.timeIntervalSince1970]
        case .interval(let every, let start): return ["type": "interval", "every_sec": every, "start": start.timeIntervalSince1970]
        case .daily(let time, let days):
            var o: JSONObject = ["type": "daily", "time": time]
            o["days"] = days ?? NSNull()
            return o
        }
    }

    public init?(json: JSONObject) {
        switch json["type"] as? String {
        case "once":
            guard let at = (json["at"] as? NSNumber)?.doubleValue else { return nil }
            self = .once(at: Date(timeIntervalSince1970: at))
        case "interval":
            guard let every = (json["every_sec"] as? NSNumber)?.intValue else { return nil }
            let start = (json["start"] as? NSNumber)?.doubleValue ?? Date().timeIntervalSince1970
            self = .interval(everySec: every, start: Date(timeIntervalSince1970: start))
        case "daily":
            guard let time = json["time"] as? String else { return nil }
            self = .daily(time: time, days: (json["days"] as? [NSNumber])?.map { $0.intValue })
        default:
            return nil
        }
    }

    public var isRecurring: Bool {
        if case .once = self { return false }
        return true
    }
}

public struct ParsedReminder: Equatable {
    public var schedule: Schedule?       // nil: it is a reminder but no time was understood
    public var message: String
    public var isTimer: Bool
}

/// Natural-language schedules for reminders and timers, English (local time). Other languages reach this
/// through LangNormalize, which turns them into the English phrases below.
///
/// Forms: "in 30 minutes", "in an hour and a half", "at 5 pm", "at 17:30", "at noon", "tomorrow at 9",
/// "tonight", "on friday at 3pm", "every morning", "every weekday at 8:30", "every monday and thursday at 7 pm",
/// "every 2 hours", "set a timer for 10 minutes".
public enum TimeParse {
    private static let numWords: [String: Double] = [
        "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
        "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "a couple": 2, "a couple of": 2, "a few": 3, "few": 3,
    ]
    private static let units: [String: Int] = ["second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600,
                                               "day": 86400, "week": 604800]
    private static let days: [String: Int] = [
        "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3,
        "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
    ]
    private static let partsOfDay: [String: String] = [
        "morning": "08:00", "afternoon": "14:00", "evening": "18:00", "night": "21:00", "tonight": "20:00",
        "noon": "12:00", "midday": "12:00", "midnight": "00:00",
    ]

    private static let NUM = "(\\d+(?:\\.\\d+)?|a couple of|a couple|a few|an|a|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty five|forty|fifty|sixty|ninety|few)"
    private static let UNIT = "(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)"
    private static let CLOCK = "(\\d{1,2})(?:[:.](\\d{2}))?\\s*(a\\.?m\\.?|p\\.?m\\.?)?"
    private static let DAY_NAMES = "(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thurs?|thu|fri|sat|sun)"

    /// Default time for "every morning" and friends (desktop: automation.default_morning_time).
    public static var defaultMorning = "08:00"

    // MARK: helpers

    private static func rx(_ p: String) -> Rx { Rx(p, caseInsensitive: false)! }

    private static func group(_ m: NSTextCheckingResult, _ i: Int, _ ns: NSString) -> String? {
        guard i < m.numberOfRanges else { return nil }
        let r = m.range(at: i)
        return r.location == NSNotFound ? nil : ns.substring(with: r)
    }

    private static func num(_ s: String) -> Double {
        let t = s.lowercased().trimmingCharacters(in: .whitespaces)
        if let d = Double(t) { return d }
        return numWords[t] ?? 1
    }

    private static func unitSeconds(_ u: String) -> Int {
        var t = u.lowercased()
        if t.hasSuffix("s") { t.removeLast() }
        return units[t] ?? units[String(t.prefix(3))] ?? 60
    }

    /// (hour, minute), or nil for a time that doesn't exist. "at 5" almost always means 5 PM for reminders.
    private static func clock(_ h: String, _ m: String?, _ ampm: String?, defaultPM: Bool = true) -> (Int, Int)? {
        guard var hour = Int(h), let minute = Int(m ?? "0") else { return nil }
        let ap = (ampm ?? "").replacingOccurrences(of: ".", with: "").lowercased()
        if ap == "pm" && hour < 12 { hour += 12 }
        else if ap == "am" && hour == 12 { hour = 0 }
        else if ap.isEmpty && defaultPM && hour >= 1 && hour <= 7 { hour += 12 }
        guard hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59 else { return nil }
        return (hour, minute)
    }

    private static func hhmm(_ h: Int, _ m: Int) -> String { String(format: "%02d:%02d", h, m) }

    static var calendar: Calendar { Calendar.current }

    /// Monday = 0
    static func weekday(_ date: Date) -> Int { (calendar.component(.weekday, from: date) + 5) % 7 }

    private static func at(_ date: Date, hour: Int, minute: Int) -> Date {
        calendar.date(bySettingHour: hour, minute: minute, second: 0, of: date) ?? date
    }

    private static func strip(_ text: String, _ r: NSRange) -> String {
        let ns = text as NSString
        let out = (ns.substring(to: r.location) + " " + ns.substring(from: r.location + r.length))
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let collapsed = rx("\\s{2,}").sub(" ", in: out)
        return collapsed.trimmingCharacters(in: CharacterSet(charactersIn: " ,."))
    }

    // MARK: next run

    /// Next occurrence of HH:MM on one of ``days`` (nil = every day), strictly after ``now``.
    public static func nextDaily(time: String, days: [Int]?, now: Date) -> Date {
        let parts = time.split(separator: ":").compactMap { Int($0) }
        let hour = parts.first ?? 9, minute = parts.count > 1 ? parts[1] : 0
        for offset in 0..<8 {
            guard let day = calendar.date(byAdding: .day, value: offset, to: now) else { continue }
            let cand = at(day, hour: hour, minute: minute)
            if cand > now && (days == nil || days!.contains(weekday(cand))) { return cand }
        }
        let tomorrow = calendar.date(byAdding: .day, value: 1, to: now) ?? now
        return at(tomorrow, hour: hour, minute: minute)
    }

    /// Next fire time for a schedule strictly after ``after``; nil when it will never fire again.
    public static func nextRun(_ schedule: Schedule, after: Date) -> Date? {
        switch schedule {
        case .once(let at): return at > after ? at : nil
        case .interval(let every, let start):
            let e = max(1, every)
            if start > after { return start }
            let n = Int(after.timeIntervalSince(start) / Double(e)) + 1
            return start.addingTimeInterval(Double(n * e))
        case .daily(let time, let days): return nextDaily(time: time, days: days, now: after)
        }
    }

    /// "today at 5:00 PM", "every weekday at 8:30 AM" — the same words the desktop says.
    public static func describe(_ schedule: Schedule, now: Date = Date()) -> String {
        func clockText(_ date: Date) -> String {
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.dateFormat = "h:mm a"
            return f.string(from: date)
        }
        switch schedule {
        case .once(let at):
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.dateFormat = "EEEE MMM dd"
            let day: String
            if calendar.isDate(at, inSameDayAs: now) { day = "today" }
            else if let t = calendar.date(byAdding: .day, value: 1, to: now), calendar.isDate(at, inSameDayAs: t) { day = "tomorrow" }
            else { day = f.string(from: at) }
            return "\(day) at \(clockText(at))"
        case .interval(let every, _):
            if every % 3600 == 0 { let n = every / 3600; return n == 1 ? "every hour" : "every \(n) hours" }
            if every % 60 == 0 { let n = every / 60; return n == 1 ? "every minute" : "every \(n) minutes" }
            return "every \(every) seconds"
        case .daily(let time, let days):
            let parts = time.split(separator: ":").compactMap { Int($0) }
            let date = at(Date(timeIntervalSince1970: 946_728_000), hour: parts.first ?? 9, minute: parts.count > 1 ? parts[1] : 0)
            let clock = clockText(date)
            let names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            guard let days = days, days.count != 7, !days.isEmpty else { return "every day at \(clock)" }
            let sorted = days.sorted()
            if sorted == [0, 1, 2, 3, 4] { return "every weekday at \(clock)" }
            if sorted == [5, 6] { return "every weekend day at \(clock)" }
            return "every " + sorted.map { names[$0] }.joined(separator: " and ") + " at \(clock)"
        }
    }

    // MARK: parse_schedule

    public static func parseSchedule(_ text: String, now: Date = Date()) -> (schedule: Schedule?, rest: String) {
        let s = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let low = s.lowercased()
        let lowNS = low as NSString

        // ---- recurring: every N units
        if let m = rx("\\bevery\\s+(?:\(NUM)\\s+)?\(UNIT)\\b").search(low),
           rx("\\bevery\\s+(day|week)\\b").search(lowNS.substring(with: m.range)) == nil {
            let n = group(m, 1, lowNS).map { num($0) } ?? 1
            let every = Int(n * Double(unitSeconds(group(m, 2, lowNS) ?? "minute")))
            if every >= 60 {
                return (.interval(everySec: every, start: now.addingTimeInterval(Double(every))), strip(s, m.range))
            }
        }

        // ---- recurring: every <day part / day / weekday> [at time]
        let recurring = "\\b(?:every|each|daily|on)\\s*(morning|afternoon|evening|night|day|weekday|weekend|\(DAY_NAMES)(?:\\s*(?:and|,)\\s*\(DAY_NAMES))*s?)?\\b(?:\\s+(?:at|@)\\s+\(CLOCK)|\\s+(morning|afternoon|evening|night))?"
        if let m = rx(recurring).search(low) {
            let whole = lowNS.substring(with: m.range)
            if whole.hasPrefix("every") || whole.hasPrefix("each") || whole.hasPrefix("daily")
                || (whole.hasPrefix("on") && low.contains("every")) {
                let what = (group(m, 1, lowNS) ?? "day").trimmingCharacters(in: .whitespaces)
                var clockText: String?
                if let h = group(m, 4, lowNS) {
                    guard let (hh, mm) = clock(h, group(m, 5, lowNS), group(m, 6, lowNS), defaultPM: what != "morning") else {
                        return (nil, text)
                    }
                    clockText = hhmm(hh, mm)
                }
                var dayList: [Int]?
                if partsOfDay[what] != nil {
                    clockText = clockText ?? (what == "morning" ? defaultMorning : partsOfDay[what])
                } else if what == "weekday" {
                    dayList = [0, 1, 2, 3, 4]
                } else if what == "weekend" {
                    dayList = [5, 6]
                } else if what != "day" {
                    let ns = what as NSString
                    let found = rx(DAY_NAMES + "s?").regex.matches(in: what, options: [], range: NSRange(location: 0, length: ns.length))
                    var set = Set<Int>()
                    for f in found {
                        var name = ns.substring(with: f.range(at: 1))
                        if days[name] == nil, name.hasSuffix("s") { name.removeLast() }
                        if let d = days[name] { set.insert(d) }
                    }
                    dayList = set.sorted()
                }
                if let part = group(m, 7, lowNS) { clockText = clockText ?? partsOfDay[part] }
                if clockText == nil {
                    if let tm = rx("\\b(?:at|@)\\s+\(CLOCK)").search(low) {
                        guard let (hh, mm) = clock(group(tm, 1, lowNS) ?? "", group(tm, 2, lowNS), group(tm, 3, lowNS)) else {
                            return (nil, text)
                        }
                        let s2 = strip(s, tm.range)
                        let rest: String
                        if let m2 = rx(NSRegularExpression.escapedPattern(for: whole)).search(s2.lowercased()) {
                            rest = strip(s2, m2.range)
                        } else { rest = s2 }
                        return (.daily(time: hhmm(hh, mm), days: dayList), rest)
                    }
                    clockText = dayList != nil ? defaultMorning : "09:00"
                }
                return (.daily(time: clockText ?? "09:00", days: dayList), strip(s, m.range))
            }
        }

        // ---- one-shot: N units from now
        if let m = rx("\\b(?:\(NUM)\\s+\(UNIT)(?:\\s+and\\s+\(NUM)\\s+\(UNIT))?)\\s+from\\s+now\\b").search(low) {
            let whole = lowNS.substring(with: m.range)
            let ns = whole as NSString
            var secs = 0.0
            for f in rx("\(NUM)\\s+\(UNIT)").regex.matches(in: whole, options: [], range: NSRange(location: 0, length: ns.length)) {
                secs += num(ns.substring(with: f.range(at: 1))) * Double(unitSeconds(ns.substring(with: f.range(at: 2))))
            }
            if secs > 0 { return (.once(at: now.addingTimeInterval(secs)), strip(s, m.range)) }
        }

        // ---- one-shot: in N units
        if let m = rx("\\b(?:in|after)\\s+(half an hour|\(NUM)\\s+\(UNIT)(?:\\s+and\\s+(?:a\\s+)?(?:half|\(NUM)\\s+\(UNIT)))?)\\b").search(low) {
            let expr = group(m, 1, lowNS) ?? ""
            var secs = 0.0
            if expr == "half an hour" {
                secs = 1800
            } else {
                let ns = expr as NSString
                for f in rx("\(NUM)\\s+\(UNIT)").regex.matches(in: expr, options: [], range: NSRange(location: 0, length: ns.length)) {
                    secs += num(ns.substring(with: f.range(at: 1))) * Double(unitSeconds(ns.substring(with: f.range(at: 2))))
                }
                if rx("and\\s+(a\\s+)?half").search(expr) != nil, let u = rx(UNIT).search(expr) {
                    secs += Double(unitSeconds(ns.substring(with: u.range(at: 1)))) / 2
                }
            }
            if secs > 0 { return (.once(at: now.addingTimeInterval(secs)), strip(s, m.range)) }
        }

        // ---- one-shot: [tomorrow|on <day>|tonight] [at time]
        var dayOffset: Int?
        var dayMatch: NSTextCheckingResult?
        if let dm = rx("\\b(tomorrow|tonight|today|this (?:morning|afternoon|evening))\\b").search(low) {
            dayMatch = dm
            dayOffset = group(dm, 1, lowNS) == "tomorrow" ? 1 : 0
        } else if let dm = rx("\\b(?:on\\s+|next\\s+|this\\s+)?\(DAY_NAMES)\\b").search(low) {
            dayMatch = dm
            let target = days[group(dm, 1, lowNS) ?? ""] ?? 0
            var off = (target - weekday(now) + 7) % 7
            if off == 0 || lowNS.substring(with: dm.range).contains("next") { off = off == 0 ? 7 : off }
            dayOffset = off
        }
        let tm = rx("\\b(?:at|@|by)\\s+(?:\(CLOCK)|(noon|midday|midnight))").search(low)
        let part: NSTextCheckingResult? = dayMatch != nil ? rx("\\b(morning|afternoon|evening|night)\\b").search(low) : nil
        if tm != nil || dayMatch != nil {
            var hour = 0, minute = 0
            if let tm = tm {
                if let special = group(tm, 4, lowNS) {
                    let p = (partsOfDay[special] ?? "09:00").split(separator: ":").compactMap { Int($0) }
                    hour = p[0]; minute = p[1]
                } else {
                    let isMorning = rx("\\bmorning\\b").search(low) != nil
                    guard let (hh, mm) = clock(group(tm, 1, lowNS) ?? "", group(tm, 2, lowNS), group(tm, 3, lowNS),
                                              defaultPM: !isMorning) else { return (nil, text) }
                    hour = hh; minute = mm
                }
            } else if part != nil || (dayMatch.map { lowNS.substring(with: $0.range).hasPrefix("tonight") || lowNS.substring(with: $0.range).hasPrefix("this") } ?? false) {
                var key = part.flatMap { group($0, 1, lowNS) } ?? ""
                if key.isEmpty, let dm = dayMatch { key = lowNS.substring(with: dm.range).split(separator: " ").last.map(String.init) ?? "" }
                let hh = key == "morning" ? defaultMorning : (partsOfDay[key] ?? "09:00")
                let p = hh.split(separator: ":").compactMap { Int($0) }
                hour = p[0]; minute = p[1]
            } else {
                let p = defaultMorning.split(separator: ":").compactMap { Int($0) }
                hour = p[0]; minute = p[1]
            }
            let base = calendar.date(byAdding: .day, value: dayOffset ?? 0, to: now) ?? now
            var when = at(base, hour: hour, minute: minute)
            if dayOffset == nil && when <= now {
                when = calendar.date(byAdding: .day, value: 1, to: when) ?? when      // "at 5 pm" after 5 pm -> tomorrow
            } else if when <= now {
                return (nil, text)                                                     // "today at 9" when it's already past
            }
            var rest = s
            var ranges = [tm, dayMatch, part].compactMap { $0 }.map { $0.range }
            // "this evening" contains "evening": cutting both would take a second bite out of the wrong place.
            var kept: [NSRange] = []
            for (i, r) in ranges.enumerated() {
                var inside = false
                for (j, o) in ranges.enumerated() where j != i {
                    if o.location <= r.location && r.location + r.length <= o.location + o.length {
                        if o.location == r.location && o.length == r.length && j > i { continue }   // identical: keep the first
                        inside = true
                    }
                }
                if !inside { kept.append(r) }
            }
            ranges = kept
            ranges.sort { $0.location > $1.location }
            for r in ranges {
                let ns = rest as NSString
                if r.location + r.length <= ns.length {
                    rest = ns.substring(to: r.location) + " " + ns.substring(from: r.location + r.length)
                }
            }
            rest = rx("\\s{2,}").sub(" ", in: rest).trimmingCharacters(in: CharacterSet(charactersIn: " ,."))
            return (.once(at: when), rest)
        }
        return (nil, text)
    }

    // MARK: reminder phrasing

    /// "remind me at 5 PM to work on AIDE" -> schedule + message. nil when it isn't a reminder request.
    public static func extractReminder(_ text: String, now: Date = Date()) -> ParsedReminder? {
        let low = text.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
        guard rx("\\b(remind me|reminder|timer|alarm|wake me)\\b").search(low) != nil else { return nil }
        if rx("\\b(cancel|delete|remove|stop|list|show|what|any)\\b.*\\b(reminders?|timers?|alarms?)\\b").search(low) != nil {
            return nil                        // managing reminders, not creating one
        }
        let isTimer = rx("\\b(timer|alarm)\\b").search(low) != nil
        var work = text
        if isTimer {
            work = replaceAllCI("\\b\(NUM)[\\s-]+\(UNIT)\\s+(timer|alarm)\\b", in: work, with: "$3 in $1 $2")
            work = replaceFirstCI("\\bfor\\s+(?=\(NUM)\\s+\(UNIT))", in: work, with: "in ")
            work = replaceFirstCI("\\b(alarm|timer)\\s+for\\s+(?=\\d)", in: work, with: "$1 at ")
        }
        let (schedule, rest) = parseSchedule(work, now: now)
        var msg = rest
        msg = replaceFirstCI("^(?:hey\\s+)?(?:saint[,\\s]+)?(?:can you|could you|please|would you)?\\s*", in: msg, with: "")
        msg = replaceAllCI("\\b(?:remind me|set (?:a |an )?(?:reminder|timer|alarm)|create (?:a )?reminder|add (?:a )?reminder|wake me up|wake me|timer|alarm)\\b", in: msg, with: " ")
        msg = replaceFirstCI("^\\s*(?:for|to|that|about|of)\\b", in: msg.trimmingCharacters(in: .whitespacesAndNewlines), with: " ")
        msg = replaceFirstCI("\\b(?:for|to)\\s*$", in: msg.trimmingCharacters(in: .whitespacesAndNewlines), with: "")
        msg = rx("\\s{2,}").sub(" ", in: msg).trimmingCharacters(in: CharacterSet(charactersIn: " ,.?!"))
        msg = replaceFirstCI("^(to|that|about|a|an)\\s+", in: msg, with: "")
        if Rx("^(a|an)$")!.match(msg) != nil { msg = "" }
        if msg.isEmpty { msg = isTimer ? (low.contains("alarm") ? "Alarm" : "Timer finished") : "Reminder" }
        msg = replaceAllCI("\\bmy\\b", in: msg, with: "your")
        let cased = String(msg.prefix(1)).uppercased() + String(msg.dropFirst())
        return ParsedReminder(schedule: schedule, message: cased, isTimer: isTimer)
    }

    private static func replaceFirstCI(_ pattern: String, in text: String, with template: String) -> String {
        guard let r = Rx(pattern, caseInsensitive: true) else { return text }
        let ns = text as NSString
        guard let m = r.regex.firstMatch(in: text, options: [], range: NSRange(location: 0, length: ns.length)) else { return text }
        let replacement = r.regex.replacementString(for: m, in: text, offset: 0, template: template)
        return ns.replacingCharacters(in: m.range, with: replacement)
    }

    private static func replaceAllCI(_ pattern: String, in text: String, with template: String) -> String {
        guard let r = Rx(pattern, caseInsensitive: true) else { return text }
        return r.regex.stringByReplacingMatches(in: text, options: [], range: NSRange(location: 0, length: (text as NSString).length),
                                                withTemplate: template)
    }
}

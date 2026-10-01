import Foundation

/// A whole-utterance pattern. A pattern that fails to compile matches nothing instead of crashing.
fileprivate func R(_ body: String) -> Rx { Rx("^(?:" + body + ")$") ?? Rx("a^")! }

/// The phone's own understanding of what you said. It speaks the same canonical English commands the language
/// layer produces from Spanish, French, Portuguese, German and Italian, so one set of patterns serves every
/// language, and the English replies go back out through the same phrasebook. Whatever it can't do itself —
/// opening a window on your PC, answering a hard question — goes to your PC's SAINT when it is connected.
public final class Brain {
    public let memory: MemoryStore
    public let skills: SkillStore
    public let aliases: AliasStore
    public let scenes: SceneStore
    public let reminders: ReminderStore
    public let settings: SettingsStore
    public let lang: LangEngine
    public let feed: ContextFeed
    /// What SAINT did on this phone (the Activity tab; synced to your PC).
    public let actions: ActionLog

    public var music: MusicService?
    /// The phone itself: camera, flashlight, calls, texts, apps, Shortcuts.
    public var phone: PhoneService?
    /// This phone's name, written into the activity log.
    public var deviceName = "iPhone"
    public var pc: PCBridge?
    public var model: LanguageModel?
    /// Send what the phone can't do itself to your PC, before trying the on-device model.
    public var preferPC = true
    public var clock: () -> Date = { Date() }

    private enum Pending {
        case reminderTime(message: String)
        case pcFollowUp(peerID: String)
    }
    private var pending: Pending?
    private var translations: [String: String] = [:]
    private var history: [(user: String, reply: String)] = []
    /// What kind of request the last route handled (music, phone, reminder, memory, pc, chat), for the log.
    private var routeKind = "chat"

    public init(directory: URL?, lang: LangEngine = LangEngine(), feed: ContextFeed = ContextFeed()) {
        memory = MemoryStore(directory: directory)
        skills = SkillStore(directory: directory)
        aliases = AliasStore(directory: directory)
        scenes = SceneStore(directory: directory)
        reminders = ReminderStore(directory: directory)
        settings = SettingsStore(directory: directory)
        actions = ActionLog(directory: directory)
        self.lang = lang
        self.feed = feed
        settings.apply(to: &self.lang.settings)
    }

    /// The stores the sync engine exchanges with your PC.
    public var adapters: [SyncAdapter] { [memory, skills, aliases, scenes, reminders, settings, actions] }

    public func reloadSettings() { settings.apply(to: &lang.settings) }

    // MARK: outcome

    private struct Outcome {
        var text: String
        var localized = false
        var expectsReply = false
        var ok = true
        var stop = false
        var source = "phone"
        var language = ""
        init(_ text: String, ok: Bool = true) {
            self.text = text
            self.ok = ok
        }
    }

    // MARK: patterns

    private struct Hit {
        let text: NSString
        let result: NSTextCheckingResult
        func group(_ i: Int) -> String {
            guard i < result.numberOfRanges else { return "" }
            let r = result.range(at: i)
            return r.location == NSNotFound ? "" : text.substring(with: r).trimmed
        }
    }

    private static func hit(_ rx: Rx, _ text: String) -> Hit? {
        guard let m = rx.search(text) else { return nil }
        return Hit(text: text as NSString, result: m)
    }

    private enum P {
        // music
        static let pause = R(#"pause|pause (?:the )?(?:music|song|track|playback)|stop (?:the )?(?:music|song|track)"#)
        static let resume = R(#"resume|resume (?:the )?(?:music|song|track|playback)|continue|unpause|play|play (?:the )?(?:music|song)|keep playing"#)
        static let skip = R(#"skip|next|next (?:song|track)|skip (?:this |the )?(?:song|track)"#)
        static let previous = R(#"go back|previous|previous (?:song|track)|last (?:song|track)|play (?:the )?previous(?: song| track)?"#)
        static let restart = R(#"replay (?:this )?(?:song|track)|restart (?:this )?(?:song|track)|start (?:this )?(?:song|track) over|play (?:this )?(?:song|track) again"#)
        static let volumeUp = R(#"turn it up|louder|volume up|turn (?:the )?volume up|turn up (?:the )?volume"#)
        static let volumeDown = R(#"turn it down|quieter|softer|volume down|turn (?:the )?volume down|turn down (?:the )?volume"#)
        static let setVolume = R(#"(?:set (?:the )?)?volume(?: to)? (\d{1,3})\s*(?:%|percent)?"#)
        static let nowPlaying = R(#"what(?:'s| is| am i)? (?:am i )?(?:playing|listening to)(?: right now)?|what(?:'s| is) this (?:song|track)|what song is this|who(?:'s| is) (?:this|singing(?: this)?)"#)
        static let like = R(#"i (?:love|like) this(?: song| track)?|like this (?:song|track)|save this(?: song| track)?|add this to my liked songs"#)
        static let shuffle = R(#"(?:turn )?shuffle (on|off)"#)
        static let playAlbum = R(#"play (?:the )?album (.+)"#)
        static let playPlaylist = R(#"play (?:my |the )(.+?) playlist"#)
        static let playLike = R(#"play something like (.+)"#)
        static let playILike = R(#"play something i(?:'d| would) like"#)
        static let playSome = R(#"play some (.+)"#)
        static let play = R(#"(?:play|put on) (.+)"#)
        static let playLiked = R(#"(?:play|shuffle) (?:my |all my )?(?:liked|saved|favou?rite) (?:songs|tracks|music)|play my likes|play (?:the )?songs i(?:'ve)? liked"#)
        static let playRecent = R(#"play (?:what |the (?:songs|music) (?:that )?)i(?:'ve| have)? (?:been )?(?:listening to|listened to|played|playing)(?: today| lately| recently| this week)?|play my recent(?:ly played)?(?: songs| tracks| music)?"#)
        static let recentSummary = R(#"what (?:did|have) i (?:been )?(?:listen(?:ed|ing)? to|play(?:ed|ing)?)(?: today| lately| recently| yesterday| this week)?|what(?:'s| is| was) my listening (?:history|today)"#)
        static let topTrack = R(#"what(?:'s| is| was) (?:the )?(?:song|track) i(?:'ve| have)? (?:played|listened to|been playing|been listening to) (?:the )?most(?: lately| recently| this week| this month)?|what(?:'s| is) my (?:most played|top|favou?rite) (?:song|track)(?: lately| recently| right now)?"#)
        static let queue = R(#"(?:add|put) (.+?) (?:to|in|on|into) (?:the |my )?queue|queue(?: up)? (.+)|play (.+?) next"#)
        static let addToPlaylist = R(#"(?:add|save|put) (?:this|this song|this track|it|the song|that|that song) (?:to|in|into|on) (?:my |the )?(.+?)(?: playlist)?"#)
        static let recommend = R(#"what should i (?:listen to|play)(?: now| next)?|recommend (?:me )?(?:a song|some music|something(?: to listen to)?|music)|(?:any )?(?:music |song )?recommendations"#)
        static let transfer = R(#"(?:switch|move|transfer|send) (?:the )?(?:music|spotify|playback|it|this) to (?:my |the )?(.+)|play (?:it|this|the music) on (?:my |the )?(.+)"#)
        static let seek = R(#"(?:skip|go|jump|fast forward)(?: ahead| forward)? (\d{1,3}) seconds?|(?:go |jump )?back (\d{1,3}) seconds?|rewind (\d{1,3}) seconds?"#)

        // the phone itself
        static let photo = R(#"take (?:a |me a |another )?(?:picture|photo|pic|snapshot|selfie)(?: of (?:me|this|that|us))?|snap (?:a )?(?:picture|photo|pic|selfie)|(?:take|snap) (?:a )?(?:selfie|front) (?:picture|photo)"#)
        static let selfie = Rx(#"selfie|front|of me|of us"#) ?? Rx("a^")!
        static let camera = R(#"open (?:the |my )?camera(?: app)?|(?:launch|start) (?:the )?camera"#)
        static let video = R(#"(?:record|take|start recording|shoot) (?:a )?video"#)
        static let torch = R(#"(?:turn |switch )?(on|off) (?:the |my )?(?:flash ?light|torch|flash)|(?:turn |switch )?(?:the |my )?(?:flash ?light|torch|flash) (on|off)|(?:toggle )?(?:the )?(?:flash ?light|torch)"#)
        static let brightness = R(#"(?:set |turn )?(?:the |my )?(?:screen )?brightness(?: to| at)? (\d{1,3})\s*(?:%|percent)?"#)
        static let brighter = R(#"(?:make (?:the |my )?screen |make it )?(brighter|dimmer|darker)|(?:turn )?(?:the |my )?(?:screen )?brightness (up|down)|turn (up|down) (?:the )?brightness"#)
        static let call = R(#"(?:call|phone|ring|dial)(?: up)? (.+)"#)
        static let facetime = R(#"face ?time (.+)|(?:video call|video chat) (.+)"#)
        static let text = R(#"(?:text|imessage|send (?:a )?(?:text|message|imessage)(?: to)?|message) (.+)"#)
        static let openApp = R(#"(?:open|launch|go to) (?:the |my )?(.+?)(?: app)?"#)
        static let navigate = R(#"(?:navigate|get directions|directions|give me directions|take me|drive me|route me)(?: to)? (.+)|how do i get to (.+)"#)
        static let battery = R(#"(?:what(?:'s| is) )?(?:my |the )?battery(?: level| percentage| life)?|how much battery(?: do i have)?(?: left)?|how(?:'s| is) my battery"#)
        static let system = R(#"(?:turn |switch )?(on|off) (?:the |my )?(low power(?: mode)?|wi-?fi|bluetooth|do not disturb|airplane mode|dark mode|focus(?: mode)?|(?:personal )?hotspot)|(?:turn |switch )?(?:the |my )?(low power(?: mode)?|wi-?fi|bluetooth|do not disturb|airplane mode|dark mode|focus(?: mode)?|(?:personal )?hotspot) (on|off)|(enable|disable|activate|deactivate) (?:the |my )?(low power(?: mode)?|wi-?fi|bluetooth|do not disturb|airplane mode|dark mode|focus(?: mode)?|(?:personal )?hotspot)"#)
        static let shortcut = R(#"run (?:the |my )?(.+?) shortcut|run shortcut (.+)"#)
        static let settingsApp = R(#"open (?:the |my )?(?:phone |iphone )?settings(?: app)?"#)
        static let webSearch = R(#"(?:search|google|look up)(?: the web| google| online)?(?: for)? (.+)"#)

        // reminders
        static let remList = R(#"(?:what|which) (?:reminders|timers|alarms)(?: do i have| are (?:set|there))?(?: today| now)?|(?:list|show|read) (?:me )?(?:my |the )?(?:reminders|timers|alarms)|what(?:'s| is) on my (?:reminders|schedule)"#)
        static let remCancelAll = R(#"(?:cancel|delete|clear|remove) (?:all )?(?:of )?(?:my )?(?:reminders|timers|alarms)"#)
        static let remCancelOne = R(#"(?:cancel|delete|remove) (?:the |my )?(.+?) (?:reminder|timer|alarm)"#)
        static let remCancelThe = R(#"(?:cancel|delete|remove) (?:the|my|that) (?:reminder|timer|alarm)"#)

        // memory
        static let favorite = R(#"my favou?rite (.+?) is (.+)"#)
        static let askFavorite = R(#"what(?:'s| is) my favou?rite (.+?)"#)
        static let rememberThat = R(#"remember (?:that )?(.+)"#)
        static let callMe = R(#"call me (.+)"#)
        static let liveIn = R(#"i live in (.+)"#)
        static let workAt = R(#"i work (?:at|for) (.+)"#)
        static let forget = R(#"forget (?:about )?(?:that )?(.+)"#)
        static let whatKnow = R(#"what do you (?:know|remember) about me"#)
        static let doYouRemember = R(#"(?:do you remember|what did i (?:tell|say to) you about|what do you know about) (.+)"#)
        static let aliasTeach = R(#"when i say (.+?),? i mean (.+)"#)
        static let skillTeach = R(#"when i say (.+?)(?:,\s*|\s+then\s+|\s+you\s+(?:should|can|could)\s+|\s+please\s+)(?:then )?(.+)"#)

        // small talk
        static let time = R(#"what(?:'s| is)? the time|what time is it(?: now)?|do you have the time|current time|tell me the time"#)
        static let date = R(#"what(?:'s| is)? (?:the |today's )?date(?: today)?|what day is it(?: today)?|what(?:'s| is) today"#)
        static let thanks = R(#"thanks|thank you|thank you (?:so|very) much|thanks (?:a lot|so much)|cheers"#)
        static let stop = R(#"stop|stop everything|be quiet|shut up|enough|that's enough|stop talking"#)
        static let neverMind = R(#"never ?mind|cancel that|forget it|ignore that"#)
        static let help = R(#"what can you do|help|what do you do|help me"#)
        static let hello = R(#"hi|hello|hey|yo|good (?:morning|afternoon|evening)|hi there|hello there"#)
        static let yes = R(#"yes|yeah|yep|sure|ok|okay|do it"#)
        static let no = R(#"no|nope|don't|do not"#)

        // devices and the link
        static let sendPrompt = R(#"(?:send|type|put|ask) (?:this |that |the |a )?prompt to (.+?)(?:'s)?\s+(?:pc|computer|laptop|desktop|saint|machine)\s+(?:on|in|using|with|to)\s+([a-z0-9 ]+?)\s*[:,]\s*(.+)"#)
        static let sendPromptKnown = R(#"(?:send|type|put|ask) (?:this |that |the |a )?prompt to (.+?)(?:'s)?\s+(?:pc|computer|laptop|desktop|saint|machine)\s+(?:on|in|using|with|to)\s+(claude(?: ai)?|chat ?gpt|gemini|copilot|perplexity|grok)(?:\s*[:,]\s*|\s+)(?:to |and )?(.+)"#)
        static let askPeer = R(#"(?:ask|tell|have|get|make) (my|[^']+?(?:'s)?) (?:pc|computer|laptop|desktop|saint|machine)(?: saint)?(?: to)? (.+)"#)
        static let messagePeer = R(#"(?:send (?:a )?message to|message|text) (.+?)(?:'s (?:pc|computer|laptop|desktop))?(?: saying|:|,) (.+)"#)
        static let playOnPeer = R(#"(?:play|put on) (.+?) on (.+?)'s (?:pc|computer|laptop|desktop)"#)
        static let openOnPeer = R(#"open (https?://\S+) on (.+?)'s (?:pc|computer|laptop|desktop)"#)
        static let sceneOnPeer = R(#"(?:run|start) (?:the )?(.+?) scene on (.+?)'s (?:pc|computer|laptop|desktop)"#)
        static let onMyPC = R(#"(?:on|from) my (?:pc|computer|laptop|desktop)[, ]+(.+)|(.+?) on my (?:pc|computer|laptop|desktop)"#)
        static let mentionsMyPC = Rx(#"\bmy (?:pc|computer|laptop|desktop)\b"#) ?? Rx("a^")!
        static let devices = R(#"what devices are connected|(?:list|show) (?:me )?(?:my )?devices|who(?:'s| is) connected|what(?:'s| is) connected"#)
        static let sync = R(#"sync(?: now| everything| my devices| with my (?:pc|computer|laptop))?"#)
        static let sendFile = R(#"send that to (.+)"#)
        static let pairPhone = R(#"pair my phone"#)

        // things only the PC can do
        static let pcOnly = R(#"(?:close|minimize|maximize) this window|show the desktop|scroll (?:up|down)|take a screenshot|(?:un)?mute(?: my mic)?|brightness \d+|lock my pc|(?:open|close|switch to) .+|search .+ for .+|search google for .+|play .+ on steam|be quiet for .+|you can talk again|what are you doing"#)

        // the polite wrapping around any command
        static let politeHead = Rx(#"^(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"#) ?? Rx("a^")!
        static let politeTail = Rx(#"\s+(?:please|for me|now)$"#) ?? Rx("a^")!
    }

    // MARK: the front door

    /// Understand ``utterance`` and answer it. Never throws: trouble becomes a spoken apology.
    public func handle(_ utterance: String, hint: String = "") async -> BrainReply {
        let turn = lang.analyze(utterance, hint: hint)
        let command = Brain.tidy(turn.routedText)
        var outcome: Outcome
        if command.isEmpty {
            outcome = Outcome("", ok: false)
        } else {
            outcome = await route(command, turn: turn, original: utterance)
        }
        if outcome.text.isEmpty && !outcome.stop {
            outcome = Outcome("I don't know how to do that yet.", ok: false)
        }

        var text = outcome.text
        var language = turn.language
        if outcome.localized {
            if !outcome.language.isEmpty { language = outcome.language }
        } else if !text.isEmpty {
            let local = lang.localize(text, for: turn)
            text = local.text
            if !local.complete, language != "en", language != "und", !language.isEmpty,
               let translated = await translate(text, to: language) {
                text = translated
            }
        }
        var reply = BrainReply(text: text, english: outcome.localized ? "" : outcome.text, language: language.isEmpty ? "en" : language)
        reply.secondary = turn.secondary
        reply.mixed = turn.mixed
        reply.expectsReply = outcome.expectsReply
        reply.ok = outcome.ok
        reply.stopSpeaking = outcome.stop
        reply.source = outcome.source
        history.append((user: utterance, reply: text))
        if history.count > 6 { history.removeFirst(history.count - 6) }
        logAction(utterance, outcome: outcome, reply: text)
        return reply
    }

    static func tidy(_ text: String) -> String {
        var t = LangNormalize.clean(text.replacingOccurrences(of: "’", with: "'"))
        t = P.politeHead.sub("", in: t)
        t = P.politeTail.sub("", in: t)
        return t.trimmed
    }

    // MARK: routing

    private func route(_ command: String, turn: LangTurn, original: String) async -> Outcome {
        routeKind = "chat"
        if let p = pending {
            pending = nil
            if let o = await resolvePending(p, command: command, turn: turn, original: original) { return o }
        }
        if let o = await linkCommands(command, turn: turn, original: original) { routeKind = "pc"; return o }
        if let o = await skillsAndScenes(command, turn: turn, original: original) { routeKind = "skill"; return o }   // what you taught beats built-in small talk
        if let o = basics(command) { return o }
        if let o = reminderCommands(command, turn: turn) { routeKind = "reminder"; return o }
        if let o = memoryCommands(command) { routeKind = "memory"; return o }
        // The phone before music ("open Spotify" opens the app) and before "that needs your PC".
        if let o = await phoneCommands(command) { routeKind = "phone"; return o }
        if let o = await musicCommands(command) { routeKind = "music"; return o }
        if P.pcOnly.matches(command) {
            routeKind = "pc"
            return await forwardToOwnPC(original, turn: turn, because: "That one needs your PC.")
        }
        return await fallback(command, turn: turn, original: original)
    }

    private func logAction(_ utterance: String, outcome: Outcome, reply: String) {
        if utterance.trimmed.isEmpty { return }
        let kind = outcome.source == "pc" ? "pc" : routeKind
        let status = !outcome.ok ? "failed" : (outcome.source == "pc" ? "sent" : "done")
        actions.add(ActionEntry(request: utterance.trimmed, action: reply.isEmpty ? outcome.text : reply, kind: kind,
                                status: status, source: outcome.source, device: deviceName))
    }

    private func resolvePending(_ p: Pending, command: String, turn: LangTurn, original: String) async -> Outcome? {
        switch p {
        case .reminderTime(let message):
            if P.neverMind.matches(command) || P.no.matches(command) { return Outcome("Okay, never mind.") }
            var phrase = command
            if let pack = LangPack.pack(turn.language), turn.language != "en" {
                phrase = LangNormalize.translateTime(command, pack: pack)
            }
            let parsed = TimeParse.parseSchedule(phrase, now: clock())
            guard let schedule = parsed.schedule else { return nil }      // not a time: treat it as a fresh request
            return scheduleReminder(message: message, schedule: schedule, isTimer: false)
        case .pcFollowUp(let peerID):
            guard let bridge = pc else { return nil }
            do {
                let answer = try await bridge.ask(peerID: peerID, text: original, language: turn.language)
                var o = Outcome(answer.text)
                o.localized = true
                o.source = "pc"
                o.language = answer.language
                o.expectsReply = answer.expectsReply
                if answer.expectsReply { pending = .pcFollowUp(peerID: peerID) }
                return o
            } catch {
                return nil
            }
        }
    }

    // MARK: the basics

    private func basics(_ command: String) -> Outcome? {
        if P.time.matches(command) {
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.dateFormat = "h:mm a"
            return Outcome("It's \(f.string(from: clock())).")
        }
        if P.date.matches(command) {
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.dateFormat = "EEEE, MMMM d"
            return Outcome("It's \(f.string(from: clock())).")
        }
        if P.thanks.matches(command) { return Outcome("You're welcome.") }
        if P.neverMind.matches(command) { return Outcome("Okay, never mind.") }
        if P.stop.matches(command) {
            var o = Outcome("Okay.")
            o.stop = true
            return o
        }
        if P.help.matches(command) {
            return Outcome("I can play music, set reminders and timers, remember things for you, and control your PC. "
                           + "Just say SAINT, then what you want, in any language.")
        }
        if P.hello.matches(command) { return Outcome("Hi — what can I do for you?") }
        if P.yes.matches(command) || P.no.matches(command) { return Outcome("Okay.") }
        return nil
    }

    // MARK: skills and scenes

    private func skillsAndScenes(_ command: String, turn: LangTurn, original: String) async -> Outcome? {
        if let h = Brain.hit(P.aliasTeach, command) {
            aliases.set(h.group(1), to: h.group(2))
            return Outcome("Got it — I'll remember that \(h.group(1)) means \(h.group(2)).")
        }
        if let h = Brain.hit(P.skillTeach, command) {
            let steps = h.group(2).replacingOccurrences(of: " and then ", with: " then ").components(separatedBy: " then ")
            if skills.learn(phrase: h.group(1), steps: steps, how: "told") != nil {
                return Outcome("Got it — when you say “\(h.group(1))”, I'll \(steps.joined(separator: ", then ")).")
            }
            return Outcome("I can't learn that one.", ok: false)
        }
        if let skill = skills.match(command) {
            skills.noteUse(id: skill.id)
            return await runSteps(skill.steps, turn: turn)
        }
        let spoken = command.replacingOccurrences(of: "^(?:run|start|activate|turn on|do|launch) ", with: "", options: [.regularExpression, .caseInsensitive])
        if let scene = scenes.find(spoken) {
            if let own = ownPC(), pc != nil, let bridge = pc {
                do {
                    let answer = try await bridge.ask(peerID: own.id, text: "run the \(scene.name) scene", language: "en")
                    var o = Outcome(answer.text)
                    o.source = "pc"
                    return o
                } catch {
                    // fall through and run its steps here
                }
            }
            return await runSteps(scene.steps, turn: turn)
        }
        return nil
    }

    private func runSteps(_ steps: [String], turn: LangTurn) async -> Outcome {
        var said: [String] = []
        for step in steps {
            let command = Brain.tidy(step)
            let o: Outcome
            if let m = await musicCommands(command) { o = m }
            else if let r = reminderCommands(command, turn: turn) { o = r }
            else if let b = basics(command) { o = b }
            else if pc != nil { o = await forwardToOwnPC(step, turn: turn, because: "That step needs your PC.") }
            else { o = Outcome("I can't do “\(step)” without your PC.", ok: false) }
            if !o.text.isEmpty && !o.text.hasPrefix("Okay") { said.append(o.text) }
        }
        return Outcome(said.isEmpty ? "Done." : said.joined(separator: " "))
    }

    // MARK: reminders and timers

    private func scheduleReminder(message: String, schedule: Schedule, isTimer: Bool) -> Outcome {
        reminders.add(message: message, schedule: schedule, isTimer: isTimer, created: clock())
        let what = isTimer ? "timer" : "reminder"
        return Outcome("Okay — \(what) set for \(TimeParse.describe(schedule, now: clock())): \(message).")
    }

    private func reminderCommands(_ command: String, turn: LangTurn) -> Outcome? {
        if P.remList.matches(command) {
            let items = reminders.upcoming(now: clock())
            if items.isEmpty { return Outcome("You don't have any active reminders or automations.") }
            let parts = items.prefix(5).map { "\($0.reminder.title) — \(TimeParse.describe($0.reminder.schedule, now: clock()))" }
            let more = items.count > 5 ? ", plus \(items.count - 5) more" : ""
            return Outcome("You have \(items.count): " + parts.joined(separator: "; ") + more + ".")
        }
        if P.remCancelAll.matches(command) {
            let n = reminders.cancelAll()
            if n == 0 { return Outcome("You don't have any active reminders or automations.") }
            let what = n == 1 ? "your reminder" : "all \(n) of your reminders"
            return Outcome("Cancelled: \(what).")
        }
        if let h = Brain.hit(P.remCancelOne, command), !["the", "my", "that"].contains(h.group(1).lowercased()) {
            let hits = reminders.cancel(matching: h.group(1))
            if let first = hits.first { return Outcome("Cancelled: \(first.title).") }
            return Outcome("I couldn't find a reminder about \(h.group(1)).", ok: false)
        }
        if P.remCancelThe.matches(command) {
            let active = reminders.upcoming(now: clock())
            if active.count == 1 {
                reminders.cancel(id: active[0].reminder.id)
                return Outcome("Cancelled: \(active[0].reminder.title).")
            }
            if active.isEmpty { return Outcome("You don't have any active reminders or automations.") }
            var o = Outcome("Which one — \(active.prefix(3).map { $0.reminder.title }.joined(separator: ", or "))?", ok: false)
            o.expectsReply = true
            return o
        }
        if let rem = TimeParse.extractReminder(command, now: clock()) {
            guard let schedule = rem.schedule else {
                pending = .reminderTime(message: rem.message)
                var o = Outcome("When should I remind you to \(rem.message.lowercased())?", ok: false)
                o.expectsReply = true
                return o
            }
            return scheduleReminder(message: rem.message, schedule: schedule, isTimer: rem.isTimer)
        }
        return nil
    }

    // MARK: memory

    /// "my mom's birthday" -> "your mom's birthday": what was said about me, said back to me.
    public static func flip(_ text: String) -> String {
        var out = text
        let swaps: [(String, String)] = [
            (#"\bI'm\b"#, "you're"), (#"\bI am\b"#, "you are"), (#"\bI've\b"#, "you've"), (#"\bI'll\b"#, "you'll"),
            (#"\bI\b"#, "you"), (#"\bmy\b"#, "your"), (#"\bmine\b"#, "yours"), (#"\bme\b"#, "you"), (#"\bmyself\b"#, "yourself"),
        ]
        for (pattern, replacement) in swaps {
            if let rx = Rx(pattern, caseInsensitive: pattern.contains("I") ? false : true) {
                out = rx.sub(replacement, in: out)
            }
        }
        return out
    }

    public static func sentence(_ text: String) -> String {
        guard let first = text.first else { return text }
        return String(first).uppercased() + String(text.dropFirst())
    }

    private func memoryCommands(_ command: String) -> Outcome? {
        if P.whatKnow.matches(command) {
            let all = memory.all()
            if all.isEmpty { return Outcome("I don't have anything stored about you yet.") }
            let lines = all.prefix(6).map { Brain.flip($0.content) }
            return Outcome("Here's what I know: " + lines.joined(separator: "; ") + ".")
        }
        if let h = Brain.hit(P.favorite, command) {
            let key = h.group(1), value = h.group(2)
            let old = memory.all().first { fold($0.key) == fold("favorite " + key) }?.value
            memory.remember(content: "your favorite \(key) is \(value)", key: "favorite " + key, value: value,
                            category: "preference", how: "told")
            if let old = old, !old.isEmpty, fold(old) != fold(value) {
                return Outcome("Updated — your favorite \(key) is now \(value) (it was \(old)).")
            }
            return Outcome("Got it — I'll remember that your favorite \(key) is \(value).")
        }
        if let h = Brain.hit(P.askFavorite, command) {
            let key = h.group(1)
            if let found = memory.all().first(where: { fold($0.key) == fold("favorite " + key) }), !found.value.isEmpty {
                return Outcome("Your favorite \(key) is \(found.value).")
            }
            return Outcome("I don't know your favorite \(key) yet.", ok: false)
        }
        if let h = Brain.hit(P.callMe, command) {
            let name = h.group(1)
            memory.remember(content: "your name is \(name)", key: "name", value: name, category: "identity", how: "told")
            return Outcome("Got it — I'll remember that your name is \(name).")
        }
        if let h = Brain.hit(P.liveIn, command) {
            memory.remember(content: "you live in \(h.group(1))", key: "home", value: h.group(1), category: "personal", how: "told")
            return Outcome("Got it — I'll remember that you live in \(h.group(1)).")
        }
        if let h = Brain.hit(P.workAt, command) {
            memory.remember(content: "you work at \(h.group(1))", key: "work", value: h.group(1), category: "personal", how: "told")
            return Outcome("Got it — I'll remember that you work at \(h.group(1)).")
        }
        if let h = Brain.hit(P.doYouRemember, command) {
            let found = memory.search(h.group(1), limit: 3)
            if found.isEmpty { return Outcome("I don't have anything stored about that.", ok: false) }
            return Outcome(found.map { Brain.sentence(Brain.flip($0.content)) }.joined(separator: ". ") + ".")
        }
        if let h = Brain.hit(P.forget, command) {
            let gone = memory.forget(matching: h.group(1))
            return gone.isEmpty ? Outcome("I don't have anything stored about that.", ok: false) : Outcome("Okay, I've forgotten that.")
        }
        if let h = Brain.hit(P.rememberThat, command) {
            let fact = h.group(1)
            if fact.lowercased().hasPrefix("to ") {                  // "remember to call mom" is a reminder without a time
                pending = .reminderTime(message: String(fact.dropFirst(3)))
                var o = Outcome("When should I remind you to \(fact.dropFirst(3).lowercased())?", ok: false)
                o.expectsReply = true
                return o
            }
            memory.remember(content: fact, category: "fact", how: "told")
            return Outcome("Got it — I'll remember that \(Brain.flip(fact)).")
        }
        return nil
    }

    // MARK: music

    private func musicIntent(_ command: String) -> MusicIntent? {
        if command.lowercased().hasSuffix(" on steam") { return nil }       // a game on the PC, not a song
        if P.pause.matches(command) { return .pause }
        if P.resume.matches(command) { return .resume }
        if P.skip.matches(command) { return .skip }
        if P.previous.matches(command) { return .previous }
        if P.restart.matches(command) { return .restart }
        if P.volumeUp.matches(command) { return .volumeUp }
        if P.volumeDown.matches(command) { return .volumeDown }
        if let h = Brain.hit(P.setVolume, command), let n = Int(h.group(1)) { return .volume(min(100, max(0, n))) }
        if P.nowPlaying.matches(command) { return .nowPlaying }
        if P.like.matches(command) { return .like }
        if P.playLiked.matches(command) { return .playLiked }
        if P.playRecent.matches(command) { return .playRecent }
        if P.recentSummary.matches(command) { return .recentSummary }
        if P.topTrack.matches(command) { return .topTrack }
        if P.recommend.matches(command) { return .recommend }
        if let h = Brain.hit(P.seek, command) {
            if let n = Int(h.group(1)) { return .seek(n) }
            if let n = Int(h.group(2).isEmpty ? h.group(3) : h.group(2)) { return .seek(-n) }
        }
        if let h = Brain.hit(P.addToPlaylist, command) {
            let name = h.group(1)
            if !["liked songs", "likes", "favorites", "favourites", "queue"].contains(name.lowercased()) { return .addToPlaylist(name) }
        }
        if let h = Brain.hit(P.queue, command) {
            let what = [h.group(1), h.group(2), h.group(3)].first { !$0.isEmpty } ?? ""
            if !what.isEmpty && !["the", "a", "the song", "the next song", "something", "song", "it"].contains(what.lowercased()) {
                return .queue(what)
            }
        }
        if let h = Brain.hit(P.transfer, command) {
            let target = h.group(1).isEmpty ? h.group(2) : h.group(1)
            if !target.isEmpty { return .transfer(target) }
        }
        if let h = Brain.hit(P.shuffle, command) { return .shuffle(h.group(1).lowercased() == "on") }
        if let h = Brain.hit(P.playAlbum, command) { return .playAlbum(h.group(1)) }
        if let h = Brain.hit(P.playPlaylist, command) { return .playPlaylist(h.group(1)) }
        if let h = Brain.hit(P.playLike, command) { return .playLike(h.group(1)) }
        if P.playILike.matches(command) { return .playSomethingILike }
        if let h = Brain.hit(P.playSome, command) {
            let what = h.group(1).lowercased()
            if ["music", "songs", "tunes", "something", "anything", "stuff"].contains(what) { return .resume }
            return .playGenre(h.group(1))
        }
        if let h = Brain.hit(P.play, command) {
            let what = h.group(1)
            if what.lowercased().hasSuffix(" on steam") { return nil }
            return .play(what)
        }
        return nil
    }

    private func musicCommands(_ command: String) async -> Outcome? {
        guard let intent = musicIntent(command) else { return nil }
        guard let service = music else { return Outcome("Spotify isn't connected.", ok: false) }
        let text = await service.perform(intent)
        return Outcome(text, ok: !text.lowercased().contains("isn't"))
    }

    // MARK: the phone itself

    static func systemName(_ raw: String) -> String {
        let s = raw.lowercased()
        if s.hasPrefix("low power") { return "low power mode" }
        if s.replacingOccurrences(of: "-", with: "") == "wifi" { return "wi-fi" }
        if s.hasPrefix("focus") { return "focus" }
        if s.hasSuffix("hotspot") { return "hotspot" }
        return s
    }

    func phoneIntent(_ command: String) -> PhoneIntent? {
        if P.camera.matches(command) { return .openCamera }
        if P.photo.matches(command) { return .takePhoto(selfie: P.selfie.search(command) != nil) }
        if P.video.matches(command) { return .recordVideo }
        if let h = Brain.hit(P.torch, command) {
            let v = (h.group(1).isEmpty ? h.group(2) : h.group(1)).lowercased()
            return .flashlight(v.isEmpty ? nil : v == "on")
        }
        if let h = Brain.hit(P.brightness, command), let n = Int(h.group(1)) { return .brightness(min(100, max(0, n))) }
        if let h = Brain.hit(P.brighter, command) {
            let w = [h.group(1), h.group(2), h.group(3)].first { !$0.isEmpty }?.lowercased() ?? ""
            return .brightnessStep(up: w == "brighter" || w == "up")
        }
        if let h = Brain.hit(P.system, command) {
            if !h.group(1).isEmpty { return .system(Brain.systemName(h.group(2)), h.group(1).lowercased() == "on") }
            if !h.group(3).isEmpty { return .system(Brain.systemName(h.group(3)), h.group(4).lowercased() == "on") }
            let verb = h.group(5).lowercased()
            return .system(Brain.systemName(h.group(6)), verb == "enable" || verb == "activate")
        }
        if P.battery.matches(command) { return .battery }
        if let h = Brain.hit(P.shortcut, command) { return .shortcut(h.group(1).isEmpty ? h.group(2) : h.group(1)) }
        if P.settingsApp.matches(command) { return .openSettings }
        if let h = Brain.hit(P.facetime, command) { return .facetime(h.group(1).isEmpty ? h.group(2) : h.group(1)) }
        if let h = Brain.hit(P.call, command) {
            let who = h.group(1)
            if who.lowercased().hasPrefix("me ") { return nil }               // "call me Seb" is a name, not a call
            return .call(who)
        }
        if let h = Brain.hit(P.text, command) { return .text(h.group(1)) }
        if let h = Brain.hit(P.navigate, command) { return .navigate(h.group(1).isEmpty ? h.group(2) : h.group(1)) }
        if let h = Brain.hit(P.webSearch, command) { return .webSearch(h.group(1)) }
        if let h = Brain.hit(P.openApp, command) { return .openApp(h.group(1)) }
        return nil
    }

    private func phoneCommands(_ command: String) async -> Outcome? {
        guard let intent = phoneIntent(command), let service = phone else { return nil }
        guard let result = await service.perform(intent) else { return nil }    // not something this phone does
        return Outcome(result.text, ok: result.ok)
    }

    // MARK: your PC and your friends' SAINTs

    public func ownPC() -> PeerInfo? {
        let own = (pc?.peers ?? []).filter { $0.isOwn && $0.platform != "ios" }
        return own.first(where: { $0.online }) ?? own.first
    }

    func resolvePeer(_ spoken: String) -> PeerInfo? {
        var name = fold(spoken).trimmingCharacters(in: CharacterSet(charactersIn: " .,!?\"'"))
        if name.hasSuffix("'s") { name = String(name.dropLast(2)) }
        for prefix in ["the ", "my "] where name.hasPrefix(prefix) { name = String(name.dropFirst(prefix.count)) }
        if name.isEmpty { return nil }
        let peers = pc?.peers ?? []
        if ["my", "me", "pc", "computer", "laptop", "desktop", "saint", "machine", "home pc"].contains(name) { return ownPC() }
        if let exact = peers.first(where: { fold($0.name) == name || $0.nicknames.map { fold($0) }.contains(name) }) { return exact }
        return peers.first { peer in
            let theirs = fold(peer.name)
            return !theirs.isEmpty && (theirs.hasPrefix(name) || name.hasPrefix(theirs))
        }
    }

    private func linkError(_ error: Error, peer: PeerInfo?) -> Outcome {
        if let link = error as? LinkError {
            switch link {
            case .notConnected, .unreachable, .closed:
                return Outcome("\(peer?.name ?? "That device") isn't connected right now.", ok: false)
            default:
                return Outcome(link.errorDescription ?? "That didn't work.", ok: false)
            }
        }
        return Outcome("That didn't work: \(error.localizedDescription)", ok: false)
    }

    private func forwardToOwnPC(_ text: String, turn: LangTurn, because: String) async -> Outcome {
        guard let bridge = pc, let own = ownPC() else {
            return Outcome("\(because) Your PC isn't connected.", ok: false)
        }
        return await ask(bridge, peer: own, text: text, turn: turn)
    }

    private func ask(_ bridge: PCBridge, peer: PeerInfo, text: String, turn: LangTurn) async -> Outcome {
        do {
            let answer = try await bridge.ask(peerID: peer.id, text: text, language: turn.language)
            var o = Outcome(answer.text)
            o.localized = true
            o.source = "pc"
            o.language = answer.language
            o.expectsReply = answer.expectsReply
            if answer.expectsReply { pending = .pcFollowUp(peerID: peer.id) }
            return o
        } catch {
            return linkError(error, peer: peer)
        }
    }

    private func runAutomation(_ name: String, args: JSONObject, on peer: PeerInfo, success: String) async -> Outcome {
        guard let bridge = pc else { return Outcome("Your devices aren't linked yet.", ok: false) }
        do {
            let result = try await bridge.runAutomation(peerID: peer.id, name: name, args: args)
            return Outcome(result.isEmpty ? success : result)
        } catch {
            return linkError(error, peer: peer)
        }
    }

    static func promptTarget(_ spoken: String) -> String {
        var t = fold(spoken).replacingOccurrences(of: " ", with: "")
        if t.hasSuffix("ai") && t.count > 4 { t = String(t.dropLast(2)) }
        return t
    }

    private func linkCommands(_ command: String, turn: LangTurn, original: String) async -> Outcome? {
        if P.pairPhone.matches(command) {
            return Outcome("To pair, open Devices and scan the code on your PC, or type its address and code.")
        }
        if P.devices.matches(command) {
            let peers = pc?.peers ?? []
            if peers.isEmpty { return Outcome("No devices are paired yet.") }
            let parts = peers.map { "\($0.name) (\($0.online ? "online" : "offline"))" }
            return Outcome("You have \(peers.count) device\(peers.count == 1 ? "" : "s"): " + parts.joined(separator: ", ") + ".")
        }
        if P.sync.matches(command) {
            guard let bridge = pc else { return Outcome("Your devices aren't linked yet.", ok: false) }
            let n = await bridge.syncNow()
            return Outcome(n > 0 ? "Synced with \(n) device\(n == 1 ? "" : "s")." : "No device to sync with right now.", ok: n > 0)
        }
        if P.sendFile.matches(command) {
            return Outcome("Open Devices, choose a device, and tap Send File — I can't pick a file by voice.", ok: false)
        }
        if let h = Brain.hit(P.sendPromptKnown, command) ?? Brain.hit(P.sendPrompt, command) {
            guard let peer = resolvePeer(h.group(1)) else { return Outcome("I don't know a device called \(h.group(1)).", ok: false) }
            let target = Brain.promptTarget(h.group(2))
            return await runAutomation("send_prompt", args: ["target": target, "prompt": h.group(3)], on: peer,
                                       success: "Sent to \(target.capitalized).")
        }
        if let h = Brain.hit(P.openOnPeer, command) {
            guard let peer = resolvePeer(h.group(2)) else { return Outcome("I don't know a device called \(h.group(2)).", ok: false) }
            return await runAutomation("open_url", args: ["url": h.group(1)], on: peer, success: "Opened.")
        }
        if let h = Brain.hit(P.sceneOnPeer, command) {
            guard let peer = resolvePeer(h.group(2)) else { return Outcome("I don't know a device called \(h.group(2)).", ok: false) }
            return await runAutomation("run_scene", args: ["scene": h.group(1)], on: peer, success: "Done.")
        }
        if let h = Brain.hit(P.playOnPeer, command) {
            guard let peer = resolvePeer(h.group(2)) else { return Outcome("I don't know a device called \(h.group(2)).", ok: false) }
            if peer.isOwn, let bridge = pc { return await ask(bridge, peer: peer, text: "play \(h.group(1))", turn: turn) }
            return await runAutomation("play_music", args: ["query": h.group(1)], on: peer, success: "Done.")
        }
        if let h = Brain.hit(P.messagePeer, command), let peer = resolvePeer(h.group(1)) {   // "text mom: ..." isn't ours
            return await runAutomation("message", args: ["text": h.group(2)], on: peer, success: "Delivered.")
        }
        if let h = Brain.hit(P.askPeer, command) {
            let who = h.group(1).lowercased() == "my" ? "my pc" : h.group(1)
            if let peer = resolvePeer(who), let bridge = pc {
                var rest = h.group(2)
                if rest.lowercased().hasPrefix("to ") { rest = String(rest.dropFirst(3)) }
                if peer.isOwn { return await ask(bridge, peer: peer, text: rest, turn: turn) }
                return await runAutomation("ask", args: ["question": rest], on: peer, success: "Done.")
            }
        }
        if let h = Brain.hit(P.onMyPC, command) {
            let rest = h.group(1).isEmpty ? h.group(2) : h.group(1)
            return await forwardToOwnPC(rest, turn: turn, because: "That one's for your PC.")
        }
        if P.mentionsMyPC.search(command) != nil {
            return await forwardToOwnPC(original, turn: turn, because: "That one's for your PC.")
        }
        return nil
    }

    // MARK: everything else

    private func fallback(_ command: String, turn: LangTurn, original: String) async -> Outcome {
        if preferPC, let bridge = pc, let own = ownPC(), own.online {
            let o = await ask(bridge, peer: own, text: original, turn: turn)
            if o.ok { return o }
        }
        if let model = model {
            do {
                let text = try await model.respond(system: systemPrompt(turn), prompt: command)
                var o = Outcome(text.trimmed)
                o.localized = true
                o.source = "model"
                o.language = turn.language
                if !o.text.isEmpty { return o }
            } catch {
                // fall through
            }
        }
        if !preferPC, let bridge = pc, let own = ownPC(), own.online {
            return await ask(bridge, peer: own, text: original, turn: turn)
        }
        return Outcome("I don't know how to do that yet.", ok: false)
    }

    private func systemPrompt(_ turn: LangTurn) -> String {
        var parts = ["You are SAINT, a voice assistant on the user's iPhone. Your answer is spoken aloud: be brief and conversational, "
                     + "one or two short sentences, no lists or markdown."]
        let directive = turn.directive(mixedMode: lang.settings.mixedMode)
        if !directive.isEmpty { parts.append(directive) }
        let facts = memory.search(turn.routedText, limit: 4).map { "- " + Brain.flip($0.content) }
        if !facts.isEmpty { parts.append("What you know about the user:\n" + facts.joined(separator: "\n")) }
        let elsewhere = feed.describe(now: clock())
        if !elsewhere.isEmpty { parts.append("What the user just did on their other devices:\n" + elsewhere) }
        if !history.isEmpty {
            parts.append("The conversation so far:\n" + history.suffix(4).map { "User: \($0.user)\nSAINT: \($0.reply)" }.joined(separator: "\n"))
        }
        return parts.joined(separator: "\n\n")
    }

    /// An English reply with no phrasebook entry, put into the user's language by the language model (cached).
    private func translate(_ text: String, to code: String) async -> String? {
        guard let model = model else { return nil }
        let key = code + "|" + text
        if let cached = translations[key] { return cached }
        let system = "Translate the user's message into \(LangPack.languageName(code)). Keep names, titles and numbers as they are. "
            + "Output only the translation."
        guard let out = try? await model.respond(system: system, prompt: text) else { return nil }
        let clean = out.trimmed
        if clean.isEmpty { return nil }
        translations[key] = clean
        return clean
    }
}

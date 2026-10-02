import XCTest
@testable import SaintCore

final class BrainTests: XCTestCase {
    private let noon: Date = {
        var c = DateComponents()
        c.year = 2026; c.month = 9; c.day = 30; c.hour = 12; c.minute = 0
        return Calendar.current.date(from: c) ?? Date()
    }()

    private struct Rig {
        let brain: Brain
        let music: FakeMusic
        let bridge: FakeBridge
        let model: FakeModel
    }

    private func rig(pc: Bool = false, model: Bool = false) -> Rig {
        let brain = Brain(directory: temporaryDirectory("brain"))
        brain.clock = { [noon] in noon }
        let music = FakeMusic(), bridge = FakeBridge(), llm = FakeModel()
        brain.music = music
        if pc {
            bridge.peers = [PeerInfo(id: "home", name: "Home PC", role: "own", platform: "windows", online: true),
                            PeerInfo(id: "gian", name: "Gian", role: "collaborator", platform: "windows", online: true)]
            brain.pc = bridge
        }
        if model { brain.model = llm }
        return Rig(brain: brain, music: music, bridge: bridge, model: llm)
    }

    // MARK: music

    func testEnglishMusicCommands() async {
        let r = rig()
        r.music.reply = "Paused."
        let paused = await r.brain.handle("pause the music")
        XCTAssertEqual(paused.text, "Paused.")
        XCTAssertEqual(r.music.asked, [.pause])
        _ = await r.brain.handle("play Blinding Lights by The Weeknd")
        _ = await r.brain.handle("turn it up")
        _ = await r.brain.handle("set the volume to 30")
        _ = await r.brain.handle("play my gym playlist")
        _ = await r.brain.handle("play something like Daft Punk")
        _ = await r.brain.handle("what's playing")
        XCTAssertEqual(r.music.asked, [.pause, .play("Blinding Lights by The Weeknd"), .volumeUp, .volume(30),
                                       .playPlaylist("gym"), .playLike("Daft Punk"), .nowPlaying])
    }

    func testMusicWithoutSpotifyConnected() async {
        let r = rig()
        r.brain.music = nil
        let reply = await r.brain.handle("play some jazz")
        XCTAssertEqual(reply.text, "Spotify isn't connected.")
        XCTAssertFalse(reply.ok)
    }

    func testSpanishMusicIsUnderstoodAndAnsweredInSpanish() async {
        let r = rig()
        let reply = await r.brain.handle("pon música de Bad Bunny")
        XCTAssertEqual(r.music.asked, [.play("Bad Bunny")])
        XCTAssertEqual(reply.language, "es")
        XCTAssertEqual(reply.text, "Reproduciendo Test Song de Test Artist.")
        r.music.reply = "Paused."
        let paused = await r.brain.handle("pausa")
        XCTAssertEqual(paused.text, "En pausa.")
    }

    func testMixedLanguageSentencesWork() async {
        let r = rig()
        var s = LangSettings()
        s.preferred = ["es"]
        r.brain.lang.settings = s
        let reply = await r.brain.handle("pon some jazz")
        XCTAssertEqual(r.music.asked, [.playGenre("jazz")])
        XCTAssertTrue(reply.mixed)
    }

    // MARK: reminders

    func testSettingAReminderInEnglish() async {
        let r = rig()
        let reply = await r.brain.handle("remind me at 5 pm to call mom")
        XCTAssertEqual(reply.text, "Okay — reminder set for today at 5:00 PM: Call mom.")
        XCTAssertEqual(r.brain.reminders.all().count, 1)
    }

    func testSettingAReminderInSpanish() async {
        let r = rig()
        let reply = await r.brain.handle("recuérdame llamar a mamá a las 5 de la tarde")
        XCTAssertEqual(reply.language, "es")
        XCTAssertTrue(reply.text.hasPrefix("Vale, recordatorio para"), reply.text)
        XCTAssertEqual(r.brain.reminders.all().first?.message.lowercased().hasPrefix("llamar a mam"), true)
    }

    func testAReminderWithoutATimeAsksWhenAndThenSetsIt() async {
        let r = rig()
        let ask = await r.brain.handle("remind me to call the dentist")
        XCTAssertEqual(ask.text, "When should I remind you to call the dentist?")
        XCTAssertTrue(ask.expectsReply)
        XCTAssertTrue(r.brain.reminders.all().isEmpty)
        let done = await r.brain.handle("tomorrow at 9 am")
        XCTAssertTrue(done.text.hasPrefix("Okay — reminder set for tomorrow at 9:00 AM"), done.text)
        XCTAssertEqual(r.brain.reminders.all().count, 1)
    }

    func testTimersListingAndCancelling() async {
        let r = rig()
        let timer = await r.brain.handle("set a timer for 10 minutes")
        XCTAssertTrue(timer.text.hasPrefix("Okay — timer set for"), timer.text)
        _ = await r.brain.handle("remind me at 5 pm to call mom")
        let list = await r.brain.handle("what reminders do I have")
        XCTAssertTrue(list.text.hasPrefix("You have 2:"), list.text)
        let cancelled = await r.brain.handle("cancel the mom reminder")
        XCTAssertTrue(cancelled.text.hasPrefix("Cancelled:"), cancelled.text)
        let none = await r.brain.handle("cancel my reminders")
        XCTAssertTrue(none.text.hasPrefix("Cancelled:"), none.text)
        let empty = await r.brain.handle("what reminders do I have")
        XCTAssertEqual(empty.text, "You don't have any active reminders or automations.")
    }

    // MARK: memory

    func testRememberingAndRecallingPreferences() async {
        let r = rig()
        let told = await r.brain.handle("my favorite color is green")
        XCTAssertEqual(told.text, "Got it — I'll remember that your favorite color is green.")
        let asked = await r.brain.handle("what's my favorite color")
        XCTAssertEqual(asked.text, "Your favorite color is green.")
        let changed = await r.brain.handle("my favorite color is blue")
        XCTAssertEqual(changed.text, "Updated — your favorite color is now blue (it was green).")
        let known = await r.brain.handle("what do you know about me")
        XCTAssertTrue(known.text.contains("blue"), known.text)
        let forgot = await r.brain.handle("forget my favorite color")
        XCTAssertEqual(forgot.text, "Okay, I've forgotten that.")
        XCTAssertEqual(r.brain.memory.count, 0)
    }

    func testRememberThatTurnsFirstPersonIntoSecond() async {
        let r = rig()
        let reply = await r.brain.handle("remember that my sister is called Ana")
        XCTAssertEqual(reply.text, "Got it — I'll remember that your sister is called Ana.")
        let spanish = await r.brain.handle("llámame Gian")
        XCTAssertEqual(spanish.language, "es")
        XCTAssertEqual(r.brain.memory.search("name").first?.value, "Gian")
    }

    func testTeachingASkill() async {
        let r = rig()
        let taught = await r.brain.handle("when I say good morning, play my morning playlist then what time is it")
        XCTAssertTrue(taught.text.hasPrefix("Got it"), taught.text)
        r.music.reply = "Playing the morning playlist."
        let ran = await r.brain.handle("good morning")
        XCTAssertEqual(r.music.asked, [.playPlaylist("morning")])
        XCTAssertTrue(ran.text.contains("It's 12:00 PM."), ran.text)
    }

    // MARK: small talk

    func testTimeAndDate() async {
        let r = rig()
        let time = await r.brain.handle("what time is it")
        XCTAssertEqual(time.text, "It's 12:00 PM.")
        let date = await r.brain.handle("what's the date")
        XCTAssertEqual(date.text, "It's Wednesday, September 30.")
        let spanishTime = await r.brain.handle("¿qué hora es?")
        XCTAssertEqual(spanishTime.text, "Son las 12:00 PM.")
        let thanks = await r.brain.handle("thanks")
        XCTAssertEqual(thanks.text, "You're welcome.")
        let gracias = await r.brain.handle("gracias")
        XCTAssertEqual(gracias.text, "De nada.")
        let stop = await r.brain.handle("stop")
        XCTAssertTrue(stop.stopSpeaking)
    }

    // MARK: the PC and friends

    func testDesktopThingsGoToYourPC() async {
        let r = rig(pc: true)
        let reply = await r.brain.handle("lock my pc")
        XCTAssertEqual(r.bridge.asked.map { $0.peer }, ["home"])
        XCTAssertEqual(r.bridge.asked.first?.text, "lock my pc")
        XCTAssertEqual(reply.text, "Done on the PC.")
        XCTAssertEqual(reply.source, "pc")
        _ = await r.brain.handle("close this window")
        _ = await r.brain.handle("on my pc open Chrome")
        XCTAssertEqual(r.bridge.asked.map { $0.text }, ["lock my pc", "close this window", "open Chrome"])
    }

    func testSpanishDesktopCommandsReachThePCInSpanish() async {
        let r = rig(pc: true)
        let reply = await r.brain.handle("bloquea mi pc")
        XCTAssertEqual(r.bridge.asked.first?.text, "bloquea mi pc")
        XCTAssertEqual(r.bridge.asked.first?.language, "es")
        XCTAssertEqual(reply.source, "pc")
    }

    func testWithoutAPCTheAnswerSaysSo() async {
        let r = rig()
        let reply = await r.brain.handle("take a screenshot")
        XCTAssertTrue(reply.text.contains("isn't connected"), reply.text)
        XCTAssertFalse(reply.ok)
    }

    func testSendingAPromptToAFriendsPC() async {
        let r = rig(pc: true)
        let reply = await r.brain.handle("send this prompt to Gian's PC on Claude: summarize my notes from today")
        XCTAssertEqual(r.bridge.automations.count, 1)
        XCTAssertEqual(r.bridge.automations.first?.peer, "gian")
        XCTAssertEqual(r.bridge.automations.first?.name, "send_prompt")
        XCTAssertEqual(r.bridge.automations.first?.args["target"] as? String, "claude")
        XCTAssertEqual(r.bridge.automations.first?.args["prompt"] as? String, "summarize my notes from today")
        XCTAssertEqual(reply.text, "Sent.")
        let spanish = await r.brain.handle("manda este prompt al PC de Gian en Claude: resume mis notas de hoy")
        XCTAssertEqual(r.bridge.automations.count, 2)
        XCTAssertEqual(r.bridge.automations.last?.args["prompt"] as? String, "resume mis notas de hoy")
        XCTAssertEqual(spanish.language, "es")
    }

    func testSpokenPromptWithoutPunctuation() async {
        let r = rig(pc: true)
        _ = await r.brain.handle("send a prompt to Gian's PC on ChatGPT write me a haiku")
        XCTAssertEqual(r.bridge.automations.first?.args["target"] as? String, "chatgpt")
        XCTAssertEqual(r.bridge.automations.first?.args["prompt"] as? String, "write me a haiku")
    }

    func testOtherAutomationsOnAFriendsPC() async {
        let r = rig(pc: true)
        _ = await r.brain.handle("play lofi on Gian's PC")
        _ = await r.brain.handle("open https://example.com on Gian's PC")
        _ = await r.brain.handle("send a message to Gian saying dinner is ready")
        XCTAssertEqual(r.bridge.automations.map { $0.name }, ["play_music", "open_url", "message"])
        XCTAssertEqual(r.bridge.automations[2].args["text"] as? String, "dinner is ready")
    }

    func testAnUnknownFriendIsNamed() async {
        let r = rig(pc: true)
        let reply = await r.brain.handle("send this prompt to Zed's PC on Claude: hello")
        XCTAssertEqual(reply.text, "I don't know a device called Zed.")
        XCTAssertTrue(r.bridge.automations.isEmpty)
    }

    func testDeviceList() async {
        let r = rig(pc: true)
        let reply = await r.brain.handle("what devices are connected")
        XCTAssertEqual(reply.text, "You have 2 devices: Home PC (online), Gian (online).")
    }

    // MARK: everything else

    func testMusicThePhonesSpotifyCantPlayGoesThroughThePC() async {
        let r = rig(pc: true)
        r.music.reply = "Spotify isn't connected."
        let reply = await r.brain.handle("play Blinding Lights")
        XCTAssertEqual(r.bridge.asked.first?.text, "play Blinding Lights")
        XCTAssertEqual(reply.source, "pc")
        XCTAssertTrue(reply.ok)
        XCTAssertTrue(r.brain.actions.all().first?.detail?.contains("Played through Home PC") ?? false)
    }

    func testAMusicFailureIsLoggedAsFailedWithTheReason() async {
        let r = rig()
        r.music.reply = "Open Spotify on a device first, then try again."
        let reply = await r.brain.handle("play some jazz")
        XCTAssertFalse(reply.ok)
        XCTAssertEqual(r.brain.actions.all().first?.status, "failed")
    }

    func testAPCWhoseLinkIsDownIsDialledBeforeThePhoneAnswers() async {
        let r = rig(pc: true, model: true)
        r.bridge.peers = [PeerInfo(id: "home", name: "Home PC", role: "own", platform: "windows", online: false)]
        r.bridge.canReach = true
        let reply = await r.brain.handle("why is the sky blue")
        XCTAssertEqual(reply.source, "pc")
        XCTAssertTrue(r.model.prompts.isEmpty)
        XCTAssertEqual(r.brain.actions.all().first?.detail, "Ran on Home PC")
    }

    func testWhenThePCCantBeReachedThePhoneAnswersAndTheLogSaysWhy() async {
        let r = rig(pc: true, model: true)
        r.bridge.peers = [PeerInfo(id: "home", name: "Home PC", role: "own", platform: "windows", online: false)]
        let reply = await r.brain.handle("why is the sky blue")
        XCTAssertEqual(reply.source, "model")
        XCTAssertTrue(r.bridge.asked.isEmpty)
        XCTAssertEqual(r.brain.actions.all().first?.detail, "Answered on this phone — Home PC wasn't reachable")
    }

    func testAnEmptyAnswerFromThePCIsNotTakenAsTheReply() async {
        let r = rig(pc: true, model: true)
        r.bridge.answer = AskAnswer(text: "")
        let reply = await r.brain.handle("why is the sky blue")
        XCTAssertEqual(reply.source, "model")
        XCTAssertEqual(reply.text, "Because of Rayleigh scattering.")
    }

    func testATaskTaughtStepByStepOnThePCRunsThere() async {
        let r = rig(pc: true)
        r.brain.skills.learn(phrase: "write an email", steps: ["ask: Who's it to? -> recipient", "type {recipient}"],
                             how: "lesson")
        let reply = await r.brain.handle("write an email")
        XCTAssertEqual(r.bridge.asked.first?.text, "write an email")
        XCTAssertEqual(reply.source, "pc")
    }

    func testUnknownThingsGoToThePCFirstThenTheModel() async {
        let withPC = rig(pc: true, model: true)
        let viaPC = await withPC.brain.handle("why is the sky blue")
        XCTAssertEqual(viaPC.source, "pc")
        XCTAssertTrue(withPC.model.prompts.isEmpty)

        let phoneOnly = rig(model: true)
        let viaModel = await phoneOnly.brain.handle("why is the sky blue")
        XCTAssertEqual(viaModel.source, "model")
        XCTAssertEqual(viaModel.text, "Because of Rayleigh scattering.")
        XCTAssertTrue(phoneOnly.model.prompts[0].system.contains("SAINT"))
    }

    func testTheModelIsToldToAnswerInTheUsersLanguageAndKnowsWhatYouToldIt() async {
        let r = rig(model: true)
        _ = await r.brain.handle("my favorite color is green")
        _ = await r.brain.handle("¿por qué el cielo es azul? mi color favorito")
        let system = r.model.prompts.last?.system ?? ""
        XCTAssertTrue(system.contains("Spanish"), system)
        XCTAssertTrue(system.contains("your favorite color is green"), system)
    }

    func testWhatHappenedOnYourOtherDevicesReachesTheModel() async {
        let r = rig(model: true)
        r.brain.feed.add(source: "Home PC", user: "remind me about the dentist", reply: "Okay.", ts: noon.addingTimeInterval(-60))
        _ = await r.brain.handle("what did I just ask")
        XCTAssertTrue(r.model.prompts.last?.system.contains("Home PC") ?? false)
    }

    func testUntranslatedRepliesAreTranslatedByTheModel() async {
        let r = rig(model: true)
        r.model.reply = "Las cosas de hoy."
        let reply = await r.brain.handle("¿qué sabes de mí?")        // the phone has nothing stored: that has a phrasebook line
        XCTAssertEqual(reply.language, "es")
        XCTAssertEqual(reply.text, "Todavía no tengo nada guardado sobre ti.")
        r.brain.memory.remember(content: "your sister is called Ana", key: "sister", value: "Ana")
        let list = await r.brain.handle("¿qué sabes de mí?")        // "Here's what I know: …" is not in the phrasebook
        XCTAssertEqual(list.text, "Las cosas de hoy.")
    }

    func testNoPCNoModelSaysSo() async {
        let r = rig()
        let reply = await r.brain.handle("why is the sky blue")
        XCTAssertEqual(reply.text, "I don't know how to do that yet.")
        XCTAssertFalse(reply.ok)
    }

    /// The translation is only worth anything if the phone understands the English it produces.
    func testEveryEnglishCommandTheLanguageLayerProducesIsUnderstood() async throws {
        let cases = try XCTUnwrap(TestData.object("lang_cases")["commands"] as? [[String: Any]])
        var lost: [String] = []
        for c in cases {
            guard let english = c["english"] as? String, !english.isEmpty else { continue }
            let r = rig()
            let reply = await r.brain.handle(english)
            if reply.text == "I don't know how to do that yet." { lost.append(english) }
        }
        XCTAssertEqual(lost, [])
    }
}

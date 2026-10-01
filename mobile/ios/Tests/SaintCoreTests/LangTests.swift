import XCTest
@testable import SaintCore

/// The same cases the desktop's Python tests run (tests/data/lang_cases.json).
final class LangTests: XCTestCase {
    private func engine(prefer: [String] = []) -> LangEngine {
        var s = LangSettings()
        s.preferred = prefer
        return LangEngine(settings: s)
    }

    func testEveryPackLoads() {
        XCTAssertEqual(Set(LangPack.available()), Set(["es", "fr", "pt", "de", "it"]))
        for code in LangPack.available() {
            let p = LangPack.pack(code)
            XCTAssertNotNil(p)
            XCTAssertFalse(p?.commands.isEmpty ?? true, code)
            XCTAssertFalse(p?.phrasebook.isEmpty ?? true, code)
            XCTAssertFalse(p?.timeRules.isEmpty ?? true, code)
            XCTAssertEqual(p?.months.count, 12, code)
            XCTAssertEqual(p?.weekdays.count, 7, code)
        }
    }

    func testCommandsBecomeTheEnglishTheRouterKnows() throws {
        let cases = try XCTUnwrap(TestData.object("lang_cases")["commands"] as? [[String: Any]])
        XCTAssertGreaterThan(cases.count, 50)
        for c in cases {
            let said = c["said"] as? String ?? ""
            let turn = engine(prefer: c["prefer"] as? [String] ?? []).analyze(said)
            let wanted = (c["lang"] as? [String]) ?? [c["lang"] as? String ?? ""]
            XCTAssertTrue(wanted.contains(turn.language), "“\(said)” was read as \(turn.language), wanted \(wanted)")
            XCTAssertEqual(turn.english, c["english"] as? String, "“\(said)”")
            if let mixed = c["mixed"] as? Bool { XCTAssertEqual(turn.mixed, mixed, "“\(said)” mixed") }
        }
    }

    func testPlainEnglishIsLeftAlone() throws {
        let texts = try XCTUnwrap(TestData.object("lang_cases")["english_unchanged"] as? [String])
        for text in texts {
            let turn = engine().analyze(text)
            XCTAssertEqual(turn.language, "en", text)
            XCTAssertFalse(turn.translated, text)
            XCTAssertEqual(turn.routedText, text)
        }
    }

    func testRepliesComeBackInTheUsersLanguage() throws {
        let cases = try XCTUnwrap(TestData.object("lang_cases")["replies"] as? [[String: Any]])
        for c in cases {
            let out = LangReply.localize(c["en"] as? String ?? "", code: c["lang"] as? String ?? "")
            XCTAssertEqual(out.text, c["out"] as? String, c["en"] as? String ?? "")
        }
    }

    func testDetection() throws {
        let cases = try XCTUnwrap(TestData.object("lang_cases")["detect"] as? [[String: Any]])
        for c in cases {
            let text = c["text"] as? String ?? ""
            let det = LangDetect.detect(text, prefer: c["prefer"] as? [String] ?? [])
            XCTAssertEqual(det.primary, c["primary"] as? String, text)
            XCTAssertEqual(det.mixed, c["mixed"] as? Bool, text)
            if let secondary = c["secondary"] as? String, !secondary.isEmpty { XCTAssertEqual(det.secondary, secondary, text) }
        }
    }

    func testAOneWordAnswerKeepsTheLanguageOfTheConversation() {
        let e = engine()
        XCTAssertEqual(e.analyze("¿qué hora es?").language, "es")
        XCTAssertEqual(e.analyze("ok").language, "es")                    // no language of its own
        XCTAssertEqual(e.analyze("what time is it").language, "en")       // a clear English sentence switches back
        XCTAssertEqual(e.analyze("ok").language, "en")
    }

    func testPreferredLanguagesBreakTies() {
        XCTAssertEqual(LangDetect.detect("pausa", prefer: ["pt", "es"]).candidates.first, "pt")
        XCTAssertEqual(engine().analyze("pausa").language, "es")
        XCTAssertEqual(engine(prefer: ["it"]).analyze("pausa").language, "it")     // "pausa" is Italian too
    }

    func testTurningDetectionOffMakesEverythingPlainEnglish() {
        var s = LangSettings()
        s.autoDetect = false
        let turn = LangEngine(settings: s).analyze("pon música de Bad Bunny")
        XCTAssertFalse(turn.translated)
        XCTAssertEqual(turn.language, "en")

        var quiet = LangSettings()
        quiet.replyInUserLanguage = false
        let understood = LangEngine(settings: quiet).analyze("pon música de Bad Bunny")
        XCTAssertEqual(understood.english, "play Bad Bunny")                 // understood, answered in English
        XCTAssertEqual(understood.language, "en")
    }

    func testDirectiveForTheLanguageModel() {
        XCTAssertEqual(engine().analyze("what time is it").directive(), "")
        let spanish = engine().analyze("¿qué hora es?").directive()
        XCTAssertTrue(spanish.contains("Spanish") && spanish.contains("only"))
        let mixed = engine(prefer: ["es"]).analyze("pon some jazz")
        XCTAssertTrue(mixed.mixed)
        XCTAssertTrue(mixed.directive().contains("mixes Spanish and English"))
        XCTAssertFalse(mixed.directive(mixedMode: "dominant").contains("mixes"))
    }

    func testSegmentsSplitMixedSpeechForTextToSpeech() {
        XCTAssertEqual(LangSegments.split("Reproduciendo Bad Bunny").map { $0.language }, ["es"])
        XCTAssertEqual(LangSegments.split("What time is it").map { $0.language }, ["en"])
        let text = "Reproduciendo la canción, and I'll queue up more songs like it"
        let parts = LangSegments.split(text)
        XCTAssertEqual(parts.map { $0.language }, ["es", "en"])
        XCTAssertEqual(parts.map { $0.text }.joined(), text)
        XCTAssertTrue(LangSegments.split("").isEmpty)
        XCTAssertTrue(LangSegments.split("   ").isEmpty)
    }

    func testUnknownLanguagesAndSentencesAreLeftInEnglish() {
        XCTAssertEqual(LangReply.localize("Something nobody translated.", code: "es").text, "Something nobody translated.")
        XCTAssertFalse(LangReply.localize("Something nobody translated.", code: "es").complete)
        XCTAssertEqual(LangReply.localize("Paused.", code: "zz").text, "Paused.")
    }

    func testASpanishReminderBecomesARealSchedule() throws {
        let english = try XCTUnwrap(engine().analyze("recuérdame llamar a mamá a las 5 de la tarde").english)
        let rem = try XCTUnwrap(TimeParse.extractReminder(english))
        XCTAssertTrue(rem.message.lowercased().hasPrefix("llamar a mam"))
        guard case .once(let at)? = rem.schedule else { return XCTFail("expected a one-off time") }
        let parts = Calendar.current.dateComponents([.hour, .minute], from: at)
        XCTAssertEqual(parts.hour, 17)
        XCTAssertEqual(parts.minute, 0)
        let weekly = try XCTUnwrap(TimeParse.extractReminder(try XCTUnwrap(engine().analyze("recuérdame los lunes a las 7 hacer ejercicio").english)))
        XCTAssertEqual(weekly.schedule, .daily(time: "19:00", days: [0]))
        let timer = try XCTUnwrap(TimeParse.extractReminder(try XCTUnwrap(engine().analyze("pon un temporizador de 10 minutos").english)))
        XCTAssertTrue(timer.isTimer)
    }
}

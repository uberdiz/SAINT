import XCTest
@testable import SaintCore

final class WakeWordTests: XCTestCase {
    func testTheCommandIsWhatFollowsTheWakeWord() {
        XCTAssertEqual(WakeWord.find(in: "Hey SAINT, play some jazz")?.command, "play some jazz")
        XCTAssertEqual(WakeWord.find(in: "saint what time is it")?.command, "what time is it")
        XCTAssertEqual(WakeWord.find(in: "Okay, Saint! Pause.")?.command, "Pause")
        XCTAssertEqual(WakeWord.find(in: "Saint")?.command, "")
        XCTAssertEqual(WakeWord.find(in: "Saint")?.wakeEnd, 5)
    }

    func testOtherLanguagesAndMixedSpeech() {
        let spanish = WakeWord.variants(languages: ["es"])
        XCTAssertEqual(WakeWord.find(in: "oye seint pon música de Bad Bunny", variants: spanish)?.command, "pon música de Bad Bunny")
        XCTAssertEqual(WakeWord.find(in: "hola saint recuérdame to call mom at 5", variants: spanish)?.command, "recuérdame to call mom at 5")
        // a spelling only some recognisers produce isn't accepted unless that language is on
        XCTAssertNil(WakeWord.find(in: "sein pon música"))
    }

    func testSaintInTheMiddleOfASentenceDoesNotWakeAnything() {
        XCTAssertNil(WakeWord.find(in: "play Saint Louis Blues"))
        XCTAssertNil(WakeWord.find(in: "my friend Saint called yesterday"))
        XCTAssertNil(WakeWord.find(in: "I went to the Saints game"))
    }

    func testEverydayWordsAndNamesDoNotWakeSAINT() {
        // ordinary words that used to count as the wake word
        XCTAssertNil(WakeWord.find(in: "sant jordi is on Sunday"))
        XCTAssertNil(WakeWord.find(in: "sain et sauf"))
        XCTAssertNil(WakeWord.find(in: "sein Bruder kommt morgen", variants: WakeWord.variants(languages: ["de"])))
        // a saint's name at the start of what was said
        XCTAssertNil(WakeWord.find(in: "Saint Louis is lovely in spring"))
        XCTAssertNil(WakeWord.find(in: "Saint Patrick's Day is next week"))
        // but a pause after the word means it was SAINT being called, then a command
        XCTAssertEqual(WakeWord.find(in: "Saint Louis Blues please", pauseBefore: { $0 == 1 })?.command, "Louis Blues please")
        XCTAssertEqual(WakeWord.find(in: "saint play Louis Armstrong")?.command, "play Louis Armstrong")
    }

    func testAPauseBeforeTheWordCountsAsTheStartOfASentence() {
        let text = "I was just saying that saint play jazz"
        XCTAssertNil(WakeWord.find(in: text))
        let found = WakeWord.find(in: text, pauseBefore: { $0 == 5 })
        XCTAssertEqual(found?.command, "play jazz")
    }

    func testHowTheRecogniserMisspellsSAINTStillWakes() {
        XCTAssertEqual(WakeWord.find(in: "St. play some jazz")?.command, "play some jazz")
        XCTAssertEqual(WakeWord.find(in: "Hey St, what time is it")?.command, "what time is it")
        XCTAssertEqual(WakeWord.find(in: "Sane, pause")?.command, "pause")
        XCTAssertEqual(WakeWord.find(in: "Hey sent turn it up")?.command, "turn it up")
        // …but only where a wake word is expected
        XCTAssertNil(WakeWord.find(in: "sent it to you yesterday"))
        XCTAssertNil(WakeWord.find(in: "that's the 1st one"))
        XCTAssertNil(WakeWord.find(in: "I think he is sane honestly", pauseBefore: { $0 == 4 }))
        XCTAssertNil(WakeWord.find(in: "St. Louis is lovely"))
    }

    func testCustomWakeWords() {
        let custom = WakeWord.variants(extra: ["Jarvis"])
        XCTAssertEqual(WakeWord.find(in: "Jarvis open Spotify", variants: custom)?.command, "open Spotify")
        XCTAssertNil(WakeWord.find(in: "Jarvis open Spotify"))
    }

    func testTheCommandKeepsGrowingAfterTheWakeWord() {
        let first = WakeWord.find(in: "saint remind me")
        XCTAssertEqual(first?.command, "remind me")
        let later = WakeWord.command(in: "saint remind me at five to call mom", after: first?.wakeEnd ?? 0)
        XCTAssertEqual(later, "remind me at five to call mom")
        XCTAssertEqual(WakeWord.command(in: "saint", after: 5), "")
    }

    func testChoosingBetweenTwoRecognisersVersions() {
        // an English recogniser mangles Spanish and is unsure; the Spanish one is sure
        let picked = WakeWord.best([("en", "pawn music a day bad bunny", 0.3), ("es", "pon música de Bad Bunny", 0.9)])
        XCTAssertEqual(picked?.language, "es")
        XCTAssertEqual(WakeWord.best([("en", "what time is it", 0.9), ("es", "what time is it", 0.5)])?.language, "en")
        // no confidence from either: the text in the recogniser's own language wins
        XCTAssertEqual(WakeWord.best([("en", "pon música de Bad Bunny", 0), ("es", "pon música de Bad Bunny", 0)])?.language, "es")
        XCTAssertNil(WakeWord.best([("en", "  ", 0.9), ("es", "", 0.9)]))
    }
}

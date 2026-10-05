import XCTest
@testable import SaintCore

/// The phone, as the brain sees it: records what it was asked; apps in ``unknownApps`` aren't on this phone.
final class FakePhone: PhoneService {
    var asked: [PhoneIntent] = []
    var unknownApps: Set<String> = []
    func perform(_ intent: PhoneIntent) async -> PhoneResult? {
        asked.append(intent)
        if case .openApp(let name) = intent, unknownApps.contains(name.lowercased()) { return nil }
        return PhoneResult("Done.")
    }
}

final class PhoneAndActivityTests: XCTestCase {
    private func brain(phone: FakePhone? = FakePhone(), pc: FakeBridge? = nil) -> Brain {
        let brain = Brain(directory: temporaryDirectory("phone"))
        brain.music = FakeMusic()
        brain.phone = phone
        if let pc = pc {
            pc.peers = [PeerInfo(id: "home", name: "Home PC", role: "own", platform: "windows", online: true)]
            brain.pc = pc
        }
        return brain
    }

    // MARK: understanding phone commands

    func testPhoneIntents() {
        let b = brain()
        let cases: [(String, PhoneIntent)] = [
            ("take a picture", .takePhoto(selfie: false)),
            ("take a selfie", .takePhoto(selfie: true)),
            ("take a picture of me", .takePhoto(selfie: true)),
            ("open the camera", .openCamera),
            ("record a video", .recordVideo),
            ("turn on the flashlight", .flashlight(true)),
            ("flashlight off", .flashlight(false)),
            ("flashlight", .flashlight(nil)),
            ("brightness 40%", .brightness(40)),
            ("set brightness to 70", .brightness(70)),
            ("make the screen brighter", .brightnessStep(up: true)),
            ("turn the brightness down", .brightnessStep(up: false)),
            ("turn on low power mode", .system("low power mode", true)),
            ("turn wifi off", .system("wi-fi", false)),
            ("enable do not disturb", .system("do not disturb", true)),
            ("what's my battery", .battery),
            ("how much battery do i have left", .battery),
            ("call mom", .call("mom")),
            ("facetime alex", .facetime("alex")),
            ("text mom i'm on my way", .text("mom i'm on my way")),
            ("navigate to the airport", .navigate("the airport")),
            ("open instagram", .openApp("instagram")),
            ("run my good morning shortcut", .shortcut("good morning")),
            ("open settings", .openSettings),
            ("look up the eiffel tower", .webSearch("the eiffel tower")),
        ]
        for (said, expected) in cases {
            XCTAssertEqual(b.phoneIntent(said), expected, said)
        }
        XCTAssertNil(b.phoneIntent("call me seb"))
        XCTAssertNil(b.phoneIntent("take a screenshot"))
        XCTAssertNil(b.phoneIntent("pause the music"))
    }

    func testPhoneCommandsRunOnThePhoneAndAreLogged() async {
        let phone = FakePhone()
        let b = brain(phone: phone)
        let reply = await b.handle("take a picture")
        XCTAssertEqual(phone.asked, [.takePhoto(selfie: false)])
        XCTAssertTrue(reply.ok)
        let entry = b.actions.all().first
        XCTAssertEqual(entry?.request, "take a picture")
        XCTAssertEqual(entry?.kind, "phone")
        XCTAssertEqual(entry?.status, "done")
        XCTAssertEqual(entry?.device, "iPhone")
    }

    func testAppsThePhoneDoesntHaveGoToThePC() async {
        let phone = FakePhone()
        phone.unknownApps = ["notepad"]
        let bridge = FakeBridge()
        let b = brain(phone: phone, pc: bridge)
        let reply = await b.handle("open notepad")
        XCTAssertEqual(phone.asked, [.openApp("notepad")])
        XCTAssertEqual(bridge.asked.map { $0.text }, ["open notepad"])
        XCTAssertEqual(reply.source, "pc")
        XCTAssertEqual(b.actions.all().first?.kind, "pc")
        XCTAssertEqual(b.actions.all().first?.status, "sent")
    }

    func testWithoutAPhoneServiceOpenStillGoesToThePC() async {
        let b = brain(phone: nil)
        let reply = await b.handle("open notepad")
        XCTAssertFalse(reply.ok)
        XCTAssertTrue(reply.text.contains("PC"), reply.text)
    }

    func testOpenSpotifyOpensTheAppNotMusic() async {
        let phone = FakePhone()
        let b = brain(phone: phone)
        _ = await b.handle("open spotify")
        XCTAssertEqual(phone.asked, [.openApp("spotify")])
    }

    // MARK: new music commands

    func testNewMusicCommands() async {
        let b = brain(phone: FakePhone())
        let music = FakeMusic()
        b.music = music
        for said in ["play my liked songs", "play what i've been listening to today", "what did i listen to today",
                     "what's the song i've played the most lately", "add levitating to the queue",
                     "add this to my workout playlist", "what should i listen to", "play it on my kitchen speaker",
                     "skip ahead 30 seconds", "go back 10 seconds"] {
            _ = await b.handle(said)
        }
        XCTAssertEqual(music.asked, [.playLiked, .playRecent, .recentSummary, .topTrack, .queue("levitating"),
                                     .addToPlaylist("workout"), .recommend, .transfer("kitchen speaker"), .seek(30), .seek(-10)])
        XCTAssertEqual(b.actions.all().first?.kind, "music")
    }

    func testFailedActionsAreLoggedAsFailed() async {
        let b = brain(phone: FakePhone())
        b.music = nil
        _ = await b.handle("play some jazz")
        XCTAssertEqual(b.actions.all().first?.status, "failed")
    }

    // MARK: the activity log

    func testActionLogSyncsAndPrunes() {
        let a = ActionLog(directory: temporaryDirectory("log-a"))
        let b = ActionLog(directory: temporaryDirectory("log-b"))
        let old = ActionEntry(ts: Date().addingTimeInterval(-40 * 86400), request: "old", action: "x", kind: "chat", status: "done")
        a.add(ActionEntry(request: "take a picture", action: "Saved the photo", kind: "phone", status: "done", device: "Seb's iPhone"))
        a.add(old)
        XCTAssertEqual(a.all().map(\.request), ["take a picture"])          // older than 30 days: gone

        let snap = a.snapshot()
        _ = b.apply(changes: snap.map { (uid: $0.key, data: Optional($0.value)) })
        XCTAssertEqual(b.all().first?.request, "take a picture")
        XCTAssertEqual(b.all().first?.device, "Seb's iPhone")
        XCTAssertEqual(b.all().first?.kind, "phone")

        _ = b.apply(changes: [(uid: snap.keys.first!, data: nil)])
        XCTAssertTrue(b.all().isEmpty)
    }

    func testActionLogIsPartOfSync() {
        let b = brain()
        XCTAssertTrue(b.adapters.contains { $0.kind == "actionlog" })
    }

    // MARK: reaching the PC from anywhere

    func testPairLinksCarryTailscaleAddresses() throws {
        let code = Pairing.encodeCode(Data(0..<16))
        let link = try Pairing.parse(link: "saint://pair?h=192.168.1.20&p=8765&t=\(code)&r=own&n=Home+PC"
                                     + "&a=100.101.102.103,home-pc.tail1234.ts.net,192.168.1.20")
        XCTAssertEqual(link.host, "192.168.1.20")
        XCTAssertEqual(link.alternates, ["100.101.102.103", "home-pc.tail1234.ts.net"])
        let plain = try Pairing.parse(link: "saint://pair?h=192.168.1.20&p=8765&t=\(code)")
        XCTAssertEqual(plain.alternates, [])
    }
}

import XCTest
@testable import SaintCore

final class StoreTests: XCTestCase {
    func testMemoryRemembersSearchesAndForgets() {
        let memory = MemoryStore(directory: temporaryDirectory())
        memory.remember(content: "your favorite color is green", key: "favorite color", value: "green", category: "preference")
        memory.remember(content: "your sister is called Ana", key: "sister", value: "Ana", category: "person")
        XCTAssertEqual(memory.count, 2)
        XCTAssertEqual(memory.search("what is my favorite colour color").first?.value, "green")
        XCTAssertEqual(memory.search("who is my sister").first?.value, "Ana")
        XCTAssertTrue(memory.search("xyzzy").isEmpty)
        // the same key updates instead of duplicating
        memory.remember(content: "your favorite color is blue", key: "favorite color", value: "blue", category: "preference")
        XCTAssertEqual(memory.count, 2)
        XCTAssertEqual(memory.search("favorite color").first?.value, "blue")
        XCTAssertEqual(memory.forget(matching: "my sister").count, 1)
        XCTAssertEqual(memory.count, 1)
    }

    func testStoresSurviveRestarts() {
        let dir = temporaryDirectory()
        let first = MemoryStore(directory: dir)
        first.remember(content: "you live in Austin", key: "home", value: "Austin")
        let second = MemoryStore(directory: dir)
        XCTAssertEqual(second.all().first?.value, "Austin")
    }

    func testSkillsAreLearnedAndMatchedByPhrase() {
        let skills = SkillStore(directory: temporaryDirectory())
        XCTAssertNotNil(skills.learn(phrase: "Good morning!", steps: ["play my morning playlist", "what time is it"]))
        XCTAssertEqual(skills.match("good morning")?.steps.count, 2)
        XCTAssertNil(skills.match("good evening"))
        XCTAssertNil(skills.learn(phrase: "play lofi", steps: ["play lofi"]), "teaching a command itself teaches nothing")
        XCTAssertNil(skills.learn(phrase: "ab", steps: ["x"]))
    }

    func testScenesAreFoundByNamePhraseOrSuffix() {
        let scenes = SceneStore(directory: temporaryDirectory())
        _ = scenes.apply(changes: [(uid: "s1", data: ["name": "Movie night", "steps": ["dim the lights"], "phrase": "popcorn time"])])
        XCTAssertEqual(scenes.find("movie night")?.id, "s1")
        XCTAssertEqual(scenes.find("the movie night scene")?.id, "s1")
        XCTAssertEqual(scenes.find("popcorn time")?.id, "s1")
        XCTAssertNil(scenes.find("gaming"))
    }

    func testRemindersFireOnceAndRepeatingOnesAdvance() {
        let reminders = ReminderStore(directory: temporaryDirectory())
        let start = Date(timeIntervalSince1970: 1_800_000_000)
        let oneOff = reminders.add(message: "call mom", schedule: .once(at: start.addingTimeInterval(300)), created: start)
        let hourly = reminders.add(message: "drink water", schedule: .interval(everySec: 3600, start: start.addingTimeInterval(3600)),
                                   created: start)
        // before anything is due
        XCTAssertTrue(reminders.tick(now: start).isEmpty)
        // five minutes later the one-off is due, and only once
        let fired = reminders.tick(now: start.addingTimeInterval(305))
        XCTAssertEqual(fired.map { $0.id }, [oneOff.id])
        XCTAssertTrue(reminders.tick(now: start.addingTimeInterval(310)).isEmpty)
        XCTAssertEqual(reminders.all().first { $0.id == oneOff.id }?.status, ReminderStatus.completed)
        // the repeating one fires at the hour and stays active
        XCTAssertEqual(reminders.tick(now: start.addingTimeInterval(3601)).map { $0.id }, [hourly.id])
        XCTAssertEqual(reminders.all().first { $0.id == hourly.id }?.status, ReminderStatus.active)
        // a day passes with the phone off: one quiet catch-up, not twenty-four announcements
        XCTAssertTrue(reminders.tick(now: start.addingTimeInterval(86_400 + 1800)).isEmpty)
        XCTAssertEqual(reminders.tick(now: start.addingTimeInterval(86_400 + 3600 + 5)).count, 1)
    }

    func testCancellingReminders() {
        let reminders = ReminderStore(directory: temporaryDirectory())
        let at = Date().addingTimeInterval(3600)
        reminders.add(message: "call the dentist", schedule: .once(at: at))
        reminders.add(message: "buy milk", schedule: .once(at: at))
        XCTAssertEqual(reminders.cancel(matching: "dentist").count, 1)
        XCTAssertEqual(reminders.upcoming().count, 1)
        XCTAssertEqual(reminders.cancelAll(), 1)
        XCTAssertTrue(reminders.upcoming().isEmpty)
    }

    func testNotificationPlans() {
        let reminders = ReminderStore(directory: temporaryDirectory())
        let now = Date()
        reminders.add(message: "stretch", schedule: .daily(time: "08:30", days: [0, 6]))     // Monday and Sunday
        reminders.add(message: "ten minutes", schedule: .once(at: now.addingTimeInterval(600)), isTimer: true)
        reminders.add(message: "in the past", schedule: .once(at: now.addingTimeInterval(-600)))
        let plans = reminders.notificationPlans(now: now)
        XCTAssertEqual(plans.count, 2)
        let daily = plans.first { $0.body == "stretch" }
        XCTAssertEqual(daily?.trigger, .daily(hour: 8, minute: 30, weekdays: [2, 1]))       // Apple: 1 = Sunday, 2 = Monday
        XCTAssertEqual(plans.first { $0.title == "Timer" }?.body, "Time's up.")
    }
}

final class SyncTests: XCTestCase {
    private func device(_ id: String) -> (engine: SyncEngine, brain: Brain) {
        let dir = temporaryDirectory(id)
        let brain = Brain(directory: dir)
        return (SyncEngine(deviceID: id, adapters: brain.adapters, storage: dir.appendingPathComponent("mirror.json")), brain)
    }

    /// What LinkManager.syncWith does, between two engines in one process: `a` is the caller.
    private func exchange(_ a: SyncEngine, _ b: SyncEngine) {
        a.scan()
        b.scan()
        let result = b.diff(remote: a.manifest())
        a.apply(result.offer)
        b.apply(a.items(for: result.want))
    }

    func testWhatOneDeviceLearnsTheOtherKnows() {
        let phone = device("aaaaaaaaaaaaaaaa"), pc = device("bbbbbbbbbbbbbbbb")
        pc.brain.memory.remember(content: "your favorite band is Rammstein", key: "favorite band", value: "Rammstein")
        pc.brain.skills.learn(phrase: "movie time", steps: ["dim the lights"], how: "shown")
        exchange(phone.engine, pc.engine)
        XCTAssertEqual(phone.brain.memory.search("favorite band").first?.value, "Rammstein")
        XCTAssertEqual(phone.brain.skills.match("movie time")?.steps, ["dim the lights"])
        // and back the other way
        phone.brain.memory.remember(content: "you live in Madrid", key: "home", value: "Madrid")
        exchange(phone.engine, pc.engine)
        XCTAssertEqual(pc.brain.memory.search("where do I live home").first?.value, "Madrid")
    }

    func testNothingEchoesBack() {
        let phone = device("aaaaaaaaaaaaaaaa"), pc = device("bbbbbbbbbbbbbbbb")
        pc.brain.memory.remember(content: "your name is Sam", key: "name", value: "Sam")
        exchange(phone.engine, pc.engine)
        XCTAssertEqual(phone.engine.scan(), 0, "applying a remote item must not look like a local edit")
        XCTAssertEqual(pc.engine.scan(), 0)
        let again = pc.engine.diff(remote: phone.engine.manifest())
        XCTAssertTrue(again.want.isEmpty && again.offer.isEmpty)
    }

    func testDeletionsPropagate() {
        let phone = device("aaaaaaaaaaaaaaaa"), pc = device("bbbbbbbbbbbbbbbb")
        let entry = pc.brain.memory.remember(content: "your cat is Tom", key: "cat", value: "Tom")
        exchange(phone.engine, pc.engine)
        XCTAssertEqual(phone.brain.memory.count, 1)
        pc.brain.memory.forget(id: entry.id)
        exchange(phone.engine, pc.engine)
        XCTAssertEqual(phone.brain.memory.count, 0)
    }

    func testTheNewerEditWins() throws {
        let phone = device("aaaaaaaaaaaaaaaa"), pc = device("bbbbbbbbbbbbbbbb")
        let entry = pc.brain.memory.remember(content: "your favorite color is green", key: "favorite color", value: "green")
        exchange(phone.engine, pc.engine)
        phone.brain.memory.remember(content: "your favorite color is red", key: "favorite color", value: "red")
        phone.engine.scan()
        Thread.sleep(forTimeInterval: 0.01)
        pc.brain.memory.remember(content: "your favorite color is blue", key: "favorite color", value: "blue")   // later
        exchange(phone.engine, pc.engine)
        XCTAssertEqual(phone.brain.memory.all().first { $0.id == entry.id }?.value, "blue")
        XCTAssertEqual(pc.brain.memory.all().first { $0.id == entry.id }?.value, "blue")
    }

    func testRemindersTravelBetweenDevices() {
        let phone = device("aaaaaaaaaaaaaaaa"), pc = device("bbbbbbbbbbbbbbbb")
        let at = Date().addingTimeInterval(7200)
        phone.brain.reminders.add(message: "pick up the package", schedule: .once(at: at))
        exchange(phone.engine, pc.engine)
        let copy = pc.brain.reminders.all().first
        XCTAssertEqual(copy?.message, "pick up the package")
        XCTAssertEqual(copy?.status, ReminderStatus.active)
        guard case .once(let when)? = copy?.schedule else { return XCTFail("schedule lost in transit") }
        XCTAssertEqual(when.timeIntervalSince1970, at.timeIntervalSince1970, accuracy: 1)
    }

    func testTheWireFormatMatchesTheDesktops() throws {
        let item = SyncItem(kind: "memory", uid: "abc", ts: 1_700_000_000_000, origin: "aaaaaaaaaaaaaaaa", deleted: false,
                            data: ["content": "x"])
        let json = item.toJSON()
        XCTAssertEqual(json["k"] as? String, "memory")
        XCTAssertEqual(json["u"] as? String, "abc")
        XCTAssertEqual((json["ts"] as? NSNumber)?.int64Value, 1_700_000_000_000)
        XCTAssertEqual(json["o"] as? String, "aaaaaaaaaaaaaaaa")
        XCTAssertEqual(json["x"] as? Bool, false)
        XCTAssertNotNil(SyncItem.from(json: json))
        let tombstone = SyncItem(kind: "memory", uid: "abc", ts: 1, origin: "o", deleted: true, data: nil).toJSON()
        XCTAssertTrue(tombstone["d"] is NSNull)
        XCTAssertTrue(JSONSerialization.isValidJSONObject(tombstone))
        XCTAssertTrue(JSONSerialization.isValidJSONObject(["want": [["memory", "abc"]], "items": [json]]))
    }

    func testLanguageSettingsSync() {
        let phone = device("aaaaaaaaaaaaaaaa"), pc = device("bbbbbbbbbbbbbbbb")
        pc.brain.settings.update(preferred: ["es", "en"], mixedMode: "dominant")
        exchange(phone.engine, pc.engine)
        XCTAssertEqual(phone.brain.settings.preferred, ["es", "en"])
        XCTAssertEqual(phone.brain.settings.mixedMode, "dominant")
    }
}

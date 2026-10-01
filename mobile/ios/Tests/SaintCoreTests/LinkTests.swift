import XCTest
@testable import SaintCore

/// The phone's side of SAINT Link against a stand-in PC that speaks the same handshakes and message format.
final class LinkTests: XCTestCase {
    private let token = Data(0..<16)

    private func identity() -> LinkIdentity { LinkIdentity.loadOrCreate(secrets: MemorySecretStore(), name: "Test iPhone") }

    private func peer(for pc: FakePC, role: String = "own") -> LinkPeer {
        LinkPeer(id: pc.deviceID, name: pc.name, publicKey: pc.publicKey.hex, role: role, platform: "windows",
                 host: "10.0.0.2", port: 8765)
    }

    // MARK: handshakes

    func testPairingWithTheCodeOnThePCsScreen() async throws {
        let (phoneEnd, pcEnd) = MemoryTransport.pair()
        let pc = FakePC(transport: pcEnd)
        pc.pairingToken = token
        pc.serve()
        let me = identity()
        let (found, connection) = try await LinkConnection.pair(transport: phoneEnd, identity: me, host: "10.0.0.2",
                                                                port: 8765, token: token, role: "own")
        XCTAssertEqual(found.id, pc.deviceID)
        XCTAssertEqual(found.name, "Test PC")
        XCTAssertEqual(found.key, pc.publicKey)
        XCTAssertEqual(found.host, "10.0.0.2")
        XCTAssertEqual(found.role, "own")
        XCTAssertEqual(pc.clientStatic, me.publicKey, "the PC learned the phone's key")
        connection.close()
    }

    func testAWrongPairingCodeIsRefused() async throws {
        let (phoneEnd, pcEnd) = MemoryTransport.pair()
        let pc = FakePC(transport: pcEnd)
        pc.pairingToken = Data(16..<32)
        pc.serve()
        do {
            _ = try await LinkConnection.pair(transport: phoneEnd, identity: identity(), host: "10.0.0.2", port: 8765,
                                              token: token, role: "own")
            XCTFail("pairing with the wrong code must fail")
        } catch let error as LinkError {
            guard case .pairingFailed = error else { return XCTFail("wrong error: \(error)") }
        }
    }

    func testReconnectingToAPairedPCAndAskingItSomething() async throws {
        let (phoneEnd, pcEnd) = MemoryTransport.pair()
        let pc = FakePC(transport: pcEnd)
        pc.handlers["chat.ask"] = { data in ["text": "Respuesta a \(data["text"] as? String ?? "")", "expects_reply": false, "lang": "es"] }
        pc.serve()
        let connection = try await LinkConnection.connect(transport: phoneEnd, identity: identity(), peer: peer(for: pc))
        connection.start()
        let answer = try await connection.request("chat.ask", ["text": "hola", "lang": "es"])
        XCTAssertEqual(answer["text"] as? String, "Respuesta a hola")
        XCTAssertEqual(answer["lang"] as? String, "es")
        do {
            _ = try await connection.request("nope", [:])
            XCTFail("an unknown request must be refused")
        } catch let error as LinkError {
            XCTAssertEqual(error.code, "unknown")
        }
        connection.close()
    }

    func testANeverPairedDeviceIsNotTrusted() async throws {
        let (phoneEnd, pcEnd) = MemoryTransport.pair()
        let pc = FakePC(transport: pcEnd)
        pc.serve()
        var wrong = peer(for: pc)
        wrong.publicKey = SaintCrypto.generateKeyPair().publicKey.hex          // the key we paired with isn't the one answering
        do {
            _ = try await LinkConnection.connect(transport: phoneEnd, identity: identity(), peer: wrong, timeout: 3)
            XCTFail("a different key must not be accepted")
        } catch {
            // refused: the handshake can't complete without the right key
        }
    }

    // MARK: the manager

    private struct Rig {
        let manager: LinkManager
        let brain: Brain
        let pc: FakePC
        let peer: LinkPeer
        let events: EventLog
    }

    private final class EventLog {
        var items: [LinkEvent] = []
        func has(_ test: (LinkEvent) -> Bool) -> Bool { items.contains(where: test) }
    }

    private func rig(role: String = "own") throws -> Rig {
        let (phoneEnd, pcEnd) = MemoryTransport.pair()
        let pc = FakePC(transport: pcEnd)
        pc.serve()
        let dir = temporaryDirectory("link")
        let brain = Brain(directory: dir)
        let engine = SyncEngine(deviceID: "aaaaaaaaaaaaaaaa", adapters: brain.adapters, storage: dir.appendingPathComponent("mirror.json"))
        let store = PeerStore(directory: dir)
        let found = peer(for: pc, role: role)
        store.save(found)
        let manager = LinkManager(identity: identity(), peerStore: store, engine: engine, feed: brain.feed,
                                  inboxDirectory: dir.appendingPathComponent("inbox")) { _, _ in phoneEnd }
        let log = EventLog()
        manager.onEvent = { log.items.append($0) }
        return Rig(manager: manager, brain: brain, pc: pc, peer: found, events: log)
    }

    func testTheBrainAsksThePCThroughTheManager() async throws {
        let r = try rig()
        r.pc.handlers["chat.ask"] = { _ in ["text": "Listo en el PC.", "expects_reply": false, "lang": "es"] }
        r.brain.pc = r.manager
        XCTAssertEqual(r.manager.peers.map { $0.online }, [false])
        let reply = await r.brain.handle("bloquea mi pc")
        XCTAssertEqual(reply.text, "Listo en el PC.")
        XCTAssertEqual(reply.source, "pc")
        XCTAssertEqual(r.manager.peers.map { $0.online }, [true])
        XCTAssertEqual(r.pc.received.first?.type, "chat.ask")
        XCTAssertEqual(r.pc.received.first?.data["text"] as? String, "bloquea mi pc")
        XCTAssertEqual(r.pc.received.first?.data["lang"] as? String, "es")
    }

    func testRunningAnAutomationOnAFriendsPC() async throws {
        let r = try rig(role: "collaborator")
        r.pc.handlers["automation.run"] = { data in ["text": "Sent to Claude.", "ok": true, "echo": data["name"] as? String ?? ""] }
        let text = try await r.manager.runAutomation(peerID: r.peer.id, name: "send_prompt",
                                                     args: ["target": "claude", "prompt": "hello"])
        XCTAssertEqual(text, "Sent to Claude.")
        let sent = r.pc.received.first { $0.type == "automation.run" }
        XCTAssertEqual((sent?.data["args"] as? JSONObject)?["target"] as? String, "claude")
    }

    func testSyncingWithThePC() async throws {
        let r = try rig()
        let pcDir = temporaryDirectory("pc")
        let pcBrain = Brain(directory: pcDir)
        let pcEngine = SyncEngine(deviceID: "bbbbbbbbbbbbbbbb", adapters: pcBrain.adapters, storage: nil)
        r.pc.handlers["sync.manifest"] = { data in
            pcEngine.scan()
            let d = pcEngine.diff(remote: (data["manifest"] as? JSONObject) ?? [:])
            return ["want": d.want, "items": d.offer.map { $0.toJSON() }]
        }
        r.pc.handlers["sync.push"] = { data in
            let items = ((data["items"] as? [Any]) ?? []).compactMap { SyncItem.from(json: $0) }
            return ["applied": pcEngine.apply(items)]
        }
        pcBrain.memory.remember(content: "your favorite band is Rammstein", key: "favorite band", value: "Rammstein")
        r.brain.memory.remember(content: "you live in Madrid", key: "home", value: "Madrid")
        let result = try await r.manager.syncWith(peerID: r.peer.id)
        XCTAssertGreaterThan(result.received, 0)
        XCTAssertGreaterThan(result.sent, 0)
        XCTAssertEqual(r.brain.memory.search("favorite band").first?.value, "Rammstein")
        XCTAssertEqual(pcBrain.memory.search("where do I live home").first?.value, "Madrid")
        // a second pass finds nothing left to say
        let again = try await r.manager.syncWith(peerID: r.peer.id)
        XCTAssertEqual(again.received, 0)
        XCTAssertEqual(again.sent, 0)
    }

    func testThePCCanPushChangesAndAskForTheManifest() async throws {
        let r = try rig()
        _ = try await r.manager.connect(peerID: r.peer.id)
        let item = SyncItem(kind: "memory", uid: "abc123", ts: Int64(Date().timeIntervalSince1970 * 1000) + 5, origin: "bbbbbbbbbbbbbbbb",
                            deleted: false, data: ["content": "your dog is Rex", "key": "dog", "value": "Rex", "category": "fact",
                                                   "how": "told", "tags": ["fact"], "confidence": 1.0])
        let pushed = try await r.pc.call("sync.push", ["items": [item.toJSON()]])
        XCTAssertEqual((pushed["d"] as? JSONObject)?["applied"] as? Int, 1)
        XCTAssertEqual(r.brain.memory.search("dog").first?.value, "Rex")
        let manifest = try await r.pc.call("sync.manifest", ["manifest": ["clock": 0, "items": [String: Any]()]])
        XCTAssertEqual(manifest["ok"] as? Bool, true)
        // the phone offers everything it has: the new memory, and its language settings
        let offered = (((manifest["d"] as? JSONObject)?["items"]) as? [JSONObject]) ?? []
        XCTAssertEqual(offered.filter { $0["k"] as? String == "memory" }.count, 1)
    }

    func testAFriendsSAINTCannotUseTheConnectionToGiveThePhoneOrders() async throws {
        let r = try rig(role: "collaborator")
        _ = try await r.manager.connect(peerID: r.peer.id)
        let push = try await r.pc.call("sync.push", ["items": [[String: Any]()]])
        XCTAssertEqual(push["ok"] as? Bool, false)
        XCTAssertEqual(((push["e"]) as? JSONObject)?["code"] as? String, "denied")
        let ask = try await r.pc.call("chat.ask", ["text": "open the camera"])
        XCTAssertEqual(ask["ok"] as? Bool, false)
        let file = try await r.pc.call("file.offer", ["id": Data(count: 16).hex, "name": "x.sh", "size": 1, "sha256": String(repeating: "0", count: 64)])
        XCTAssertEqual(file["ok"] as? Bool, false)
        r.pc.handlers["context.feed"] = nil
        XCTAssertTrue(r.brain.feed.recent().isEmpty)
    }

    func testWhatYouDidOnThePCReachesThePhone() async throws {
        let r = try rig()
        _ = try await r.manager.connect(peerID: r.peer.id)
        try await r.pc.sendJSON(["t": "context.feed", "d": ["source": "Home PC", "user": "remind me about the dentist",
                                                           "reply": "Okay.", "ts": Date().timeIntervalSince1970]])
        for _ in 0..<100 where r.brain.feed.recent().isEmpty { try await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertEqual(r.brain.feed.recent().first?.user, "remind me about the dentist")
        XCTAssertTrue(r.brain.feed.describe().contains("on Home PC"))
    }

    func testTheStatusRequest() async throws {
        let r = try rig()
        r.manager.languages = { ["es", "en"] }
        _ = try await r.manager.connect(peerID: r.peer.id)
        let status = try await r.pc.call("status.get")
        let body = try XCTUnwrap(status["d"] as? JSONObject)
        XCTAssertEqual((body["device"] as? JSONObject)?["platform"] as? String, "ios")
        XCTAssertEqual(body["language"] as? [String], ["es", "en"])
    }

    // MARK: files

    func testSendingAFileToThePC() async throws {
        let r = try rig()
        r.pc.handlers["file.offer"] = { _ in ["accept": true] }
        r.pc.handlers["file.done"] = { _ in ["ok": true, "name": "notes.txt"] }
        let bytes = Data((0..<150_000).map { UInt8($0 % 251) })
        let url = temporaryDirectory("file").appendingPathComponent("notes.txt")
        try bytes.write(to: url)
        var lastDone: Int64 = 0
        let name = try await r.manager.sendFile(peerID: r.peer.id, url: url) { done, _ in lastDone = done }
        XCTAssertEqual(name, "notes.txt")
        XCTAssertEqual(lastDone, Int64(bytes.count))
        let offer = try XCTUnwrap(r.pc.received.first { $0.type == "file.offer" }?.data)
        XCTAssertEqual(offer["size"] as? Int, bytes.count)
        XCTAssertEqual(offer["sha256"] as? String, SaintCrypto.sha256(bytes).hex)
        let id = try XCTUnwrap(Data(hex: offer["id"] as? String ?? ""))
        for _ in 0..<100 where r.pc.chunks[id]?.count != bytes.count { try await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertEqual(r.pc.chunks[id], bytes)
    }

    func testReceivingAFileFromThePC() async throws {
        let r = try rig()
        _ = try await r.manager.connect(peerID: r.peer.id)
        let bytes = Data("hello from the pc".utf8) + Data(repeating: 9, count: 70_000)
        let id = SaintCrypto.randomBytes(16)
        let offer = try await r.pc.call("file.offer", ["id": id.hex, "name": "../evil/report.pdf", "size": bytes.count,
                                                      "sha256": SaintCrypto.sha256(bytes).hex])
        XCTAssertEqual(offer["ok"] as? Bool, true)
        try await r.pc.sendChunk(id: id, offset: 0, data: bytes.prefix(40_000))
        try await r.pc.sendChunk(id: id, offset: 40_000, data: bytes.dropFirst(40_000))
        try await Task.sleep(nanoseconds: 200_000_000)
        let done = try await r.pc.call("file.done", ["id": id.hex])
        XCTAssertEqual(done["ok"] as? Bool, true)
        let saved = (done["d"] as? JSONObject)?["name"] as? String
        XCTAssertEqual(saved, "report.pdf", "path tricks in the name are flattened")
        guard let event = r.events.items.compactMap({ e -> URL? in if case .fileReceived(_, _, let url) = e { return url } else { return nil } }).first else {
            return XCTFail("no file event")
        }
        XCTAssertEqual(try Data(contentsOf: event), bytes)
    }

    func testADamagedFileIsThrownAway() async throws {
        let r = try rig()
        _ = try await r.manager.connect(peerID: r.peer.id)
        let bytes = Data(repeating: 1, count: 1000)
        let id = SaintCrypto.randomBytes(16)
        _ = try await r.pc.call("file.offer", ["id": id.hex, "name": "a.bin", "size": bytes.count, "sha256": String(repeating: "f", count: 64)])
        try await r.pc.sendChunk(id: id, offset: 0, data: bytes)
        try await Task.sleep(nanoseconds: 100_000_000)
        let done = try await r.pc.call("file.done", ["id": id.hex])
        XCTAssertEqual(done["ok"] as? Bool, false)
        XCTAssertEqual((done["e"] as? JSONObject)?["code"] as? String, "corrupt")
    }

    func testFileNamesAreMadeSafe() {
        XCTAssertEqual(LinkManager.sanitize("../../etc/passwd"), "passwd")
        XCTAssertEqual(LinkManager.sanitize("C:\\Users\\me\\notes.txt"), "notes.txt")
        XCTAssertEqual(LinkManager.sanitize("a:b?.txt"), "a_b_.txt")
        XCTAssertEqual(LinkManager.sanitize("   "), "file")
        XCTAssertEqual(LinkManager.sanitize(".hidden"), "hidden")
        XCTAssertLessThanOrEqual(LinkManager.sanitize(String(repeating: "x", count: 500)).count, 150)
    }
}

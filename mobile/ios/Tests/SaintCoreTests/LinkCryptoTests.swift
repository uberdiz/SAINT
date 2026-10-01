import XCTest
@testable import SaintCore

/// Known answers produced by the desktop's pure-Python implementation (and cross-checked there against the
/// `noiseprotocol` package): if these pass, the phone and the PC speak the same cryptography.
final class LinkCryptoTests: XCTestCase {
    func testChaChaPolyMatchesTheDesktop() throws {
        let vectors = try TestData.object("link_vectors")
        let cases = try XCTUnwrap(vectors["chacha20poly1305"] as? [[String: String]])
        XCTAssertFalse(cases.isEmpty)
        for c in cases {
            let key = hexData(c["key"] ?? ""), nonce = hexData(c["nonce"] ?? "")
            let plain = hexData(c["plaintext"] ?? ""), aad = hexData(c["aad"] ?? "")
            let sealed = try SaintCrypto.seal(key: key, nonce: nonce, plaintext: plain, aad: aad)
            XCTAssertEqual(sealed.hex, c["sealed"])
            XCTAssertEqual(try SaintCrypto.open(key: key, nonce: nonce, sealed: sealed, aad: aad), plain)
        }
    }

    func testTamperedMessagesAreRejected() throws {
        let key = SaintCrypto.randomBytes(32), nonce = Data(count: 12)
        var sealed = try SaintCrypto.seal(key: key, nonce: nonce, plaintext: Data("hello".utf8))
        sealed[0] ^= 1
        XCTAssertThrowsError(try SaintCrypto.open(key: key, nonce: nonce, sealed: sealed))
    }

    func testHKDFMatchesTheDesktop() throws {
        let vectors = try TestData.object("link_vectors")
        let cases = try XCTUnwrap(vectors["hkdf"] as? [[String: Any]])
        for c in cases {
            let okm = SaintCrypto.hkdf(ikm: hexData(c["ikm"] as? String ?? ""), salt: hexData(c["salt"] as? String ?? ""),
                                       info: hexData(c["info"] as? String ?? ""), length: c["length"] as? Int ?? 0)
            XCTAssertEqual(okm.hex, c["okm"] as? String)
        }
    }

    func testX25519MatchesTheDesktop() throws {
        let vectors = try TestData.object("link_vectors")
        let cases = try XCTUnwrap(vectors["x25519"] as? [[String: String]])
        for c in cases {
            let priv = hexData(c["private"] ?? ""), peerPriv = hexData(c["peer_private"] ?? "")
            let pub = try SaintCrypto.publicKey(forPrivate: priv)
            let peerPub = try SaintCrypto.publicKey(forPrivate: peerPriv)
            XCTAssertEqual(pub.hex, c["public"])
            XCTAssertEqual(peerPub.hex, c["peer_public"])
            XCTAssertEqual(try SaintCrypto.sharedSecret(privateKey: priv, peerPublic: peerPub).hex, c["shared"])
            XCTAssertEqual(try SaintCrypto.sharedSecret(privateKey: peerPriv, peerPublic: pub).hex, c["shared"])
        }
    }

    func testPairingCodes() throws {
        let vectors = try TestData.object("link_vectors")
        let c = try XCTUnwrap(vectors["pairing_code"] as? [String: String])
        let token = hexData(c["token"] ?? "")
        XCTAssertEqual(Pairing.encodeCode(token), c["code"])
        XCTAssertEqual(try Pairing.decodeCode(c["code"] ?? ""), token)
        XCTAssertEqual(Pairing.psk(forToken: token).hex, c["psk"])
        // forgiving input: lower case, spaces, 0/1 typed for O/I
        let sloppy = (c["code"] ?? "").lowercased().replacingOccurrences(of: "-", with: " ")
        XCTAssertEqual(try Pairing.decodeCode(sloppy), token)
        XCTAssertThrowsError(try Pairing.decodeCode("ABCD-EFGH"))
        XCTAssertThrowsError(try Pairing.decodeCode("not a code!"))
    }

    func testPairingLinksAndAddresses() throws {
        let token = Data(0..<16)
        let code = Pairing.encodeCode(token)
        let link = try Pairing.parse(link: "saint://pair?h=192.168.1.20&p=8765&t=\(code)&r=own&n=Home+PC")
        XCTAssertEqual(link.host, "192.168.1.20")
        XCTAssertEqual(link.port, 8765)
        XCTAssertEqual(link.token, token)
        XCTAssertEqual(link.role, "own")
        XCTAssertEqual(link.name, "Home PC")
        XCTAssertThrowsError(try Pairing.parse(link: "https://example.com"))
        XCTAssertThrowsError(try Pairing.parse(link: "saint://pair?h=1.2.3.4"))
        XCTAssertEqual(Pairing.parse(address: "192.168.1.20:9000").port, 9000)
        XCTAssertEqual(Pairing.parse(address: "192.168.1.20").port, 8765)
        XCTAssertEqual(Pairing.parse(address: "[fe80::1]:8800").host, "fe80::1")
    }

    // MARK: Noise

    func testNoiseHandshakesMatchTheDesktopByteForByte() throws {
        let vectors = try TestData.object("link_vectors")
        let handshakes = try XCTUnwrap(vectors["handshakes"] as? [[String: Any]])
        XCTAssertEqual(handshakes.count, 2)
        for h in handshakes {
            let name = h["pattern"] as? String ?? ""
            let pattern: NoisePattern = name == "IK" ? .ik : .xxpsk3
            let pskText = h["psk"] as? String ?? ""
            let psk: Data? = pskText.isEmpty ? nil : hexData(pskText)
            let responderPublic = try SaintCrypto.publicKey(forPrivate: hexData(h["responder_static"] as? String ?? ""))
            let state = try HandshakeState(pattern: pattern, initiator: true, staticPrivate: hexData(h["initiator_static"] as? String ?? ""),
                                           remoteStatic: pattern == .ik ? responderPublic : nil, psk: psk,
                                           prologue: hexData(h["prologue"] as? String ?? ""),
                                           ephemeralPrivate: hexData(h["initiator_ephemeral"] as? String ?? ""))
            let payloads = (h["payloads"] as? [String] ?? []).map { hexData($0) }
            let messages = (h["messages"] as? [String] ?? []).map { hexData($0) }
            XCTAssertEqual(try state.writeMessage(payload: payloads[0]), messages[0], "\(name): message 1")
            XCTAssertEqual(try state.readMessage(messages[1]), payloads[1], "\(name): message 2 payload")
            if messages.count == 3 {
                XCTAssertEqual(try state.writeMessage(payload: payloads[2]), messages[2], "\(name): message 3")
            }
            let (send, receive) = try state.split()
            let first = try send.encrypt(ad: Data(), plaintext: Data("first from initiator".utf8))
            XCTAssertEqual(first.hex, h["initiator_first"] as? String, "\(name): first transport message")
            let back = try receive.decrypt(ad: Data(), ciphertext: hexData(h["responder_first"] as? String ?? ""))
            XCTAssertEqual(String(data: back, encoding: .utf8), "first from responder")
        }
    }

    func testAWrongPairingCodeFailsTheHandshake() throws {
        let (initiatorSide, responderSide) = (SaintCrypto.generateKeyPair(), SaintCrypto.generateKeyPair())
        let prologue = LinkWire.prologue(mode: LinkWire.modePair)
        let initiator = try HandshakeState(pattern: .xxpsk3, initiator: true, staticPrivate: initiatorSide.privateKey,
                                           remoteStatic: nil, psk: Pairing.psk(forToken: Data(0..<16)), prologue: prologue,
                                           ephemeralPrivate: nil)
        let responder = try HandshakeState(pattern: .xxpsk3, initiator: false, staticPrivate: responderSide.privateKey,
                                           remoteStatic: nil, psk: Pairing.psk(forToken: Data(16..<32)), prologue: prologue,
                                           ephemeralPrivate: nil)
        _ = try responder.readMessage(try initiator.writeMessage())
        _ = try initiator.readMessage(try responder.writeMessage())
        // the pairing code is mixed in at the third message, so that is where a wrong code is caught
        let third = try initiator.writeMessage(payload: Data("hello".utf8))
        XCTAssertThrowsError(try responder.readMessage(third))
    }

    // MARK: the wire

    private func linkedChannels() throws -> (SecureChannel, SecureChannel) {
        let a = SaintCrypto.generateKeyPair(), b = SaintCrypto.generateKeyPair()
        let prologue = LinkWire.prologue(mode: LinkWire.modeReconnect)
        let initiator = try HandshakeState(pattern: .ik, initiator: true, staticPrivate: a.privateKey, remoteStatic: b.publicKey,
                                           psk: nil, prologue: prologue, ephemeralPrivate: nil)
        let responder = try HandshakeState(pattern: .ik, initiator: false, staticPrivate: b.privateKey, remoteStatic: nil,
                                           psk: nil, prologue: prologue, ephemeralPrivate: nil)
        _ = try responder.readMessage(try initiator.writeMessage())
        _ = try initiator.readMessage(try responder.writeMessage())
        let (is_, ir) = try initiator.split()
        let (rs, rr) = try responder.split()
        return (SecureChannel(send: is_, receive: ir, remoteStatic: b.publicKey),
                SecureChannel(send: rs, receive: rr, remoteStatic: a.publicKey))
    }

    func testMessagesTravelBothWays() throws {
        let (phone, pc) = try linkedChannels()
        let frames = try phone.encode(json: ["t": "chat.ask", "id": 1, "d": ["text": "hola"]])
        XCTAssertEqual(frames.count, 1)
        var decoder = FrameDecoder()
        let inbound = decoder.feed(frames[0])
        guard case .json(let object)? = try pc.decode(frame: inbound[0]) else { return XCTFail("not JSON") }
        XCTAssertEqual(object["t"] as? String, "chat.ask")
        XCTAssertEqual((object["d"] as? [String: Any])?["text"] as? String, "hola")
    }

    func testBigMessagesAreFragmentedAndPutBackTogether() throws {
        let (phone, pc) = try linkedChannels()
        let big = String(repeating: "á", count: 90_000)
        let frames = try phone.encode(json: ["t": "x", "d": ["blob": big]])
        XCTAssertGreaterThan(frames.count, 1)
        var decoder = FrameDecoder()
        var result: JSONObject?
        for wire in frames {
            for frame in decoder.feed(wire) {
                if case .json(let object)? = try pc.decode(frame: frame) { result = object }
            }
        }
        XCTAssertEqual(((result?["d"]) as? [String: Any])?["blob"] as? String, big)
    }

    func testFileChunksCarryTheirOffsets() throws {
        let (phone, pc) = try linkedChannels()
        let id = Data(0..<16)
        let wire = try phone.encodeChunk(transferID: id, offset: 70_000, data: Data("abc".utf8))
        var decoder = FrameDecoder()
        guard case .chunk(let tid, let offset, let data)? = try pc.decode(frame: decoder.feed(wire)[0]) else { return XCTFail("not a chunk") }
        XCTAssertEqual(tid, id)
        XCTAssertEqual(offset, 70_000)
        XCTAssertEqual(String(data: data, encoding: .utf8), "abc")
    }

    func testFramesSplitAcrossReadsAreReassembled() throws {
        let payload = Data(repeating: 7, count: 300)
        let framed = try LinkWire.frame(payload)
        var decoder = FrameDecoder()
        XCTAssertTrue(decoder.feed(framed.prefix(1)).isEmpty)
        XCTAssertTrue(decoder.feed(framed.dropFirst(1).prefix(100)).isEmpty)
        let done = decoder.feed(framed.dropFirst(101) + framed)         // the rest, plus a whole second frame
        XCTAssertEqual(done.count, 2)
        XCTAssertEqual(done[0], payload)
        XCTAssertEqual(decoder.pendingBytes, 0)
    }

    func testDeviceIDsComeFromTheKey() {
        let pair = SaintCrypto.generateKeyPair()
        let id = Pairing.deviceID(forPublicKey: pair.publicKey)
        XCTAssertEqual(id.count, 16)
        XCTAssertEqual(id, String(SaintCrypto.sha256(pair.publicKey).hex.prefix(16)))
    }
}

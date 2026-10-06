"""SAINT Link's cryptography: RFC vectors, Noise handshakes, known answers shared with the iOS app."""

import binascii
import json
import os

import pytest

from modules.link import crypto, noise, wire
from modules.link.identity import decode_code, encode_code, psk_for

h = binascii.unhexlify
VECTORS = os.path.join(os.path.dirname(__file__), "data", "link_vectors.json")


@pytest.fixture(params=["pure", "cryptography"])
def backend(request, monkeypatch):
    """Everything below runs on both the pure-Python path and (when installed) the `cryptography` one."""
    if request.param == "cryptography":
        if not crypto.HAVE_CRYPTOGRAPHY:
            pytest.skip("the cryptography package isn't installed")
    else:
        monkeypatch.setattr(crypto, "HAVE_CRYPTOGRAPHY", False)
    return request.param


def test_x25519_rfc7748(backend):
    a_priv = h("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    b_priv = h("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
    shared = h("4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742")
    assert crypto.public_key(a_priv) == h("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
    assert crypto.shared_secret(a_priv, crypto.public_key(b_priv)) == shared
    assert crypto.shared_secret(b_priv, crypto.public_key(a_priv)) == shared


def test_chacha20_poly1305_rfc8439(backend):
    key = h("808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f")
    nonce = h("070000004041424344454647")
    aad = h("50515253c0c1c2c3c4c5c6c7")
    pt = (b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, "
          b"sunscreen would be it.")
    sealed = crypto.seal(key, nonce, pt, aad)
    assert sealed[:16] == h("d31a8d34648e60db7b86afbc53ef7ec2")
    assert sealed[-16:] == h("1ae10b594f09e26a7e902ecbd0600691")
    assert crypto.open_sealed(key, nonce, sealed, aad) == pt


def test_tampering_is_detected(backend):
    key, nonce = os.urandom(32), os.urandom(12)
    sealed = crypto.seal(key, nonce, b"attack at dawn", b"ad")
    for bad in (sealed[:-1] + bytes([sealed[-1] ^ 1]), bytes([sealed[0] ^ 1]) + sealed[1:], sealed[:-3]):
        with pytest.raises(crypto.CryptoError):
            crypto.open_sealed(key, nonce, bad, b"ad")
    with pytest.raises(crypto.CryptoError):
        crypto.open_sealed(key, nonce, sealed, b"other ad")


def test_hkdf_rfc5869():
    okm = crypto.hkdf(h("0b" * 22), h("000102030405060708090a0b0c"), h("f0f1f2f3f4f5f6f7f8f9"), 42)
    assert okm == h("3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865")


def test_pure_and_fast_paths_agree():
    if not crypto.HAVE_CRYPTOGRAPHY:
        pytest.skip("the cryptography package isn't installed")
    key, nonce = os.urandom(32), os.urandom(12)
    data = os.urandom(100_000)
    fast = crypto.seal(key, nonce, data, b"x")
    crypto.HAVE_CRYPTOGRAPHY = False
    try:
        assert crypto.seal(key, nonce, data, b"x") == fast
        assert crypto.open_sealed(key, nonce, fast, b"x") == data
    finally:
        crypto.HAVE_CRYPTOGRAPHY = True


# ---------------------------------------------------------------------- #
def _handshake(pattern, psk=None, prologue=b"p", fixed=False):
    i_s, r_s = os.urandom(32), os.urandom(32)
    known = crypto.public_key(r_s) if pattern == "IK" else None
    ini = noise.HandshakeState(pattern, True, i_s, known, psk, prologue)
    rsp = noise.HandshakeState(pattern, False, r_s, None, psk, prologue)
    return ini, rsp, i_s, r_s


@pytest.mark.parametrize("pattern,psk", [("IK", None), ("XXpsk3", b"\x07" * 32)])
def test_handshake_completes_and_both_learn_the_other_key(backend, pattern, psk):
    ini, rsp, i_s, r_s = _handshake(pattern, psk)
    payloads = [b"one", b"two", b"three"]
    n = 0
    while not ini.finished:
        assert rsp.read_message(ini.write_message(payloads[n])) == payloads[n]
        n += 1
        if ini.finished:
            break
        assert ini.read_message(rsp.write_message(payloads[n])) == payloads[n]
        n += 1
    assert ini.rs == crypto.public_key(r_s) and rsp.rs == crypto.public_key(i_s)
    send, recv = ini.split()
    rsend, rrecv = rsp.split()
    assert rrecv.decrypt(b"", send.encrypt(b"", b"hello")) == b"hello"
    assert recv.decrypt(b"", rsend.encrypt(b"", b"hi")) == b"hi"
    # a replayed transport message doesn't decrypt again (the counter moved on)
    ct = send.encrypt(b"", b"once")
    assert rrecv.decrypt(b"", ct) == b"once"
    with pytest.raises(noise.NoiseError):
        rrecv.decrypt(b"", ct)


def test_wrong_pairing_code_fails_at_the_last_message(backend):
    i_s, r_s = os.urandom(32), os.urandom(32)
    ini = noise.HandshakeState("XXpsk3", True, i_s, None, b"A" * 32, b"p")
    rsp = noise.HandshakeState("XXpsk3", False, r_s, None, b"B" * 32, b"p")
    rsp.read_message(ini.write_message())
    ini.read_message(rsp.write_message())
    with pytest.raises(noise.NoiseError):
        rsp.read_message(ini.write_message(b"secret hello"))


def test_changed_prologue_or_tampered_message_fails(backend):
    ini, rsp, _, _ = _handshake("IK", prologue=b"mode-1")
    rsp2 = noise.HandshakeState("IK", False, os.urandom(32), None, None, b"mode-2")
    m1 = ini.write_message(b"x")
    with pytest.raises(noise.NoiseError):
        rsp2.read_message(m1)
    ini, rsp, _, _ = _handshake("IK")
    bad = bytearray(ini.write_message(b"x"))
    bad[40] ^= 1
    with pytest.raises(noise.NoiseError):
        rsp.read_message(bytes(bad))
    with pytest.raises(noise.NoiseError):
        _handshake("IK")[1].read_message(b"short")


def test_handshake_out_of_turn_is_refused(backend):
    ini, rsp, _, _ = _handshake("IK")
    with pytest.raises(noise.NoiseError):
        rsp.write_message(b"x")
    with pytest.raises(noise.NoiseError):
        ini.read_message(b"x" * 100)


def test_independent_noise_implementation_agrees(backend):
    """Byte-for-byte against the `noiseprotocol` package, when it's installed."""
    nc = pytest.importorskip("noise.connection")
    Keypair = nc.Keypair
    for pattern, name, psk in (("IK", b"Noise_IK_25519_ChaChaPoly_SHA256", None),
                               ("XXpsk3", b"Noise_XXpsk3_25519_ChaChaPoly_SHA256", os.urandom(32))):
        i_s, r_s, i_e, r_e = (os.urandom(32) for _ in range(4))
        theirs_i, theirs_r = nc.NoiseConnection.from_name(name), nc.NoiseConnection.from_name(name)
        theirs_i.set_as_initiator()
        theirs_r.set_as_responder()
        theirs_i.set_keypair_from_private_bytes(Keypair.STATIC, i_s)
        theirs_r.set_keypair_from_private_bytes(Keypair.STATIC, r_s)
        if pattern == "IK":
            theirs_i.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, crypto.public_key(r_s))
        theirs_i.set_keypair_from_private_bytes(Keypair.EPHEMERAL, i_e)
        theirs_r.set_keypair_from_private_bytes(Keypair.EPHEMERAL, r_e)
        if psk:
            theirs_i.set_psks(psk)
            theirs_r.set_psks(psk)
        theirs_i.set_prologue(b"pro")
        theirs_r.set_prologue(b"pro")
        theirs_i.start_handshake()
        theirs_r.start_handshake()
        ours_i = noise.HandshakeState(pattern, True, i_s, crypto.public_key(r_s) if pattern == "IK" else None, psk, b"pro", i_e)
        ours_r = noise.HandshakeState(pattern, False, r_s, None, psk, b"pro", r_e)
        for step in range(len(noise._PATTERNS[pattern])):
            sender_t, sender_o, recv_t, recv_o = ((theirs_i, ours_i, theirs_r, ours_r) if step % 2 == 0
                                                  else (theirs_r, ours_r, theirs_i, ours_i))
            a, b = sender_t.write_message(b"p%d" % step), sender_o.write_message(b"p%d" % step)
            assert a == b
            recv_t.read_message(a)
            recv_o.read_message(b)


# ---------------------------------------------------------------------- #
# Known answers shared with the iOS app (ios/Tests/SaintCoreTests)
# ---------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def vectors():
    with open(VECTORS, "r", encoding="utf-8") as f:
        return json.load(f)


def test_known_answers_primitives(vectors, monkeypatch):
    monkeypatch.setattr(crypto, "HAVE_CRYPTOGRAPHY", False)
    for v in vectors["chacha20poly1305"]:
        sealed = crypto.seal(h(v["key"]), h(v["nonce"]), h(v["plaintext"]), h(v["aad"]))
        assert sealed.hex() == v["sealed"]
        assert crypto.open_sealed(h(v["key"]), h(v["nonce"]), h(v["sealed"]), h(v["aad"])) == h(v["plaintext"])
    v = vectors["hkdf"][0]
    assert crypto.hkdf(h(v["ikm"]), h(v["salt"]), h(v["info"]), v["length"]).hex() == v["okm"]
    v = vectors["x25519"][0]
    assert crypto.public_key(h(v["private"])).hex() == v["public"]
    assert crypto.shared_secret(h(v["private"]), h(v["peer_public"])).hex() == v["shared"]
    p = vectors["pairing_code"]
    assert encode_code(h(p["token"])) == p["code"] and decode_code(p["code"]) == h(p["token"])
    assert psk_for(h(p["token"])).hex() == p["psk"]
    p = vectors["pairing_code_short"]
    assert encode_code(h(p["token"])) == p["code"] and decode_code(p["code"]) == h(p["token"])
    assert psk_for(h(p["token"])).hex() == p["psk"]


def test_known_answers_handshakes(vectors, backend):
    for v in vectors["handshakes"]:
        psk = h(v["psk"]) if v["psk"] else None
        pro = h(v["prologue"])
        known = crypto.public_key(h(v["responder_static"])) if v["pattern"] == "IK" else None
        ini = noise.HandshakeState(v["pattern"], True, h(v["initiator_static"]), known, psk, pro, h(v["initiator_ephemeral"]))
        rsp = noise.HandshakeState(v["pattern"], False, h(v["responder_static"]), None, psk, pro, h(v["responder_ephemeral"]))
        for i, hexmsg in enumerate(v["messages"]):
            sender, receiver = (ini, rsp) if i % 2 == 0 else (rsp, ini)
            msg = sender.write_message(h(v["payloads"][i]))
            assert msg.hex() == hexmsg, (v["pattern"], i)
            assert receiver.read_message(msg) == h(v["payloads"][i])
        send, _ = ini.split()
        rsend, _ = rsp.split()
        assert send.encrypt(b"", b"first from initiator").hex() == v["initiator_first"]
        assert rsend.encrypt(b"", b"first from responder").hex() == v["responder_first"]


def test_pairing_codes_are_forgiving_and_strict():
    token = os.urandom(16)
    code = encode_code(token)
    assert decode_code(code.lower()) == token
    assert decode_code(code.replace("-", " ")) == token
    with pytest.raises(ValueError):
        decode_code("AAAA-BBB")                      # 4 bytes: neither a short nor a long code
    with pytest.raises(ValueError):
        decode_code("!!!!")


def test_short_codes_are_eight_characters_and_stretched():
    from modules.link.identity import PairingManager, SHORT_CODE_ITERATIONS, looks_like_code
    import hashlib
    offer = PairingManager().create("own")
    assert len(offer.code) == 9 and offer.code.count("-") == 1          # ABCD-EFGH
    assert decode_code(offer.code.lower().replace("-", "")) == offer.token
    assert looks_like_code(offer.code) and not looks_like_code("192.168.1.20:8765")
    assert offer.psk == hashlib.pbkdf2_hmac("sha256", offer.token, b"SAINT-LINK-PAIRING-SHORT",
                                           SHORT_CODE_ITERATIONS, 32)
    assert PairingManager().create("own", short=False).code.count("-") == 6


def test_frames_reject_oversized_and_unknown_preamble():
    import socket
    a, b = socket.socketpair()
    try:
        with pytest.raises(wire.LinkError):
            wire.write_frame(a, b"x" * 70000)
        a.sendall(b"XX\x01\x01")
        hs = wire.ServerHandshake(b, os.urandom(32))
        with pytest.raises(wire.LinkError):
            hs.begin()
    finally:
        a.close()
        b.close()

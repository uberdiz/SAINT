"""Two SAINT Link nodes talking over real sockets on localhost: pairing, reconnecting, permissions, files."""

import hashlib
import os
import tempfile
import threading
import time

import pytest

from modules.link.approvals import ApprovalQueue
from modules.link.identity import (ALLOW, ASK, COLLABORATOR, DENY, OWN, Identity, PairingManager, Peer, PeerStore,
                                   parse_address, parse_pair_uri)
from modules.link.node import LinkNode
from modules.link.wire import LinkError


def make(name):
    d = tempfile.mkdtemp(prefix=f"link-{name}-")
    ident = Identity(os.path.join(d, "identity.json"))
    ident.set_name(name)
    peers = PeerStore(os.path.join(d, "peers.json"))
    events = []
    node = LinkNode(ident, peers, PairingManager(), ApprovalQueue(emit=lambda n, p: events.append((n, p))),
                    emit=lambda n, p: events.append((n, p)), inbox_dir=lambda: os.path.join(d, "inbox"),
                    max_file_mb=lambda: 5)
    node.events = events
    node.dir = d
    return node


@pytest.fixture()
def pair():
    a, b = make("Alpha PC"), make("Beta Phone")
    a.port_ = a.start("127.0.0.1", 0)
    b.port_ = b.start("127.0.0.1", 0)
    a.register("echo", lambda ctx, d: {"you": ctx.peer.name, "said": d.get("x")})

    def chat(ctx, d):
        ctx.require("chat")
        return {"ok": 1}
    a.register("chat", chat)

    def auto(ctx, d):
        ctx.require("auto.send_prompt", "type a prompt", timeout=3)
        return {"ran": True}
    a.register("auto", auto)
    yield a, b
    a.stop()
    b.stop()


def paired(a, b, role=OWN):
    offer = a.pairing.create(role)
    peer = b.pair("127.0.0.1", a.port_, offer.code, role)
    deadline = time.time() + 3
    while not a.connected_ids() and time.time() < deadline:
        time.sleep(0.02)
    return peer


def test_pairing_then_requests_both_ways(pair):
    a, b = pair
    peer = paired(a, b)
    assert peer.id == a.identity.device_id and peer.role == OWN
    assert [p.name for p in a.peers.all()] == ["Beta Phone"]
    assert a.pairing.current is None                              # the code is single-use
    assert b.request(peer.id, "echo", {"x": "hola"}) == {"you": "Beta Phone", "said": "hola"}
    b.register("whoami", lambda ctx, d: {"you": ctx.peer.name})
    assert a.request(b.identity.device_id, "whoami") == {"you": "Alpha PC"}


def test_wrong_code_is_refused_and_the_window_survives_a_few_tries(pair):
    a, b = pair
    offer = a.pairing.create(OWN)
    with pytest.raises(LinkError):
        b.pair("127.0.0.1", a.port_, "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-GG")
    assert a.peers.all() == []
    assert a.pairing.current is offer                              # still open for the real code
    assert b.pair("127.0.0.1", a.port_, offer.code).name == "Alpha PC"


def test_pairing_window_closes_after_too_many_bad_guesses(pair):
    a, b = pair
    offer = a.pairing.create(OWN)
    for _ in range(10):
        offer.failures += 1
    assert a.pairing.current is None
    with pytest.raises(LinkError):
        b.pair("127.0.0.1", a.port_, offer.code)


def test_nobody_can_pair_without_an_open_window(pair):
    a, b = pair
    with pytest.raises(LinkError):
        b.pair("127.0.0.1", a.port_, "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-GG")
    assert a.peers.all() == []


def test_role_mismatch_pairs_as_friends_on_both_sides(pair):
    """A friend picking "my own device" on a friend code (or the reverse) used to fail as a "wrong
    code" — the friend → me direction broke while me → friend worked. Now it pairs, and neither side
    is ever upgraded: both keep the stricter role."""
    a, b = pair
    offer = a.pairing.create(COLLABORATOR)
    peer = b.pair("127.0.0.1", a.port_, offer.code, OWN)
    assert peer.role == COLLABORATOR
    assert a.peers.get(b.identity.device_id).role == COLLABORATOR
    offer = a.pairing.create(OWN)
    b.peers.remove(peer.id)
    peer = b.pair("127.0.0.1", a.port_, offer.code, COLLABORATOR)
    assert peer.role == COLLABORATOR and a.peers.get(b.identity.device_id).role == COLLABORATOR


def test_dialling_tries_every_address_at_once(pair):
    """A dead home address no longer holds up the one that works (Tailscale away from home)."""
    from modules.link.node import dial_first
    a, b = pair
    t0 = time.time()
    sock, host = dial_first(["10.255.255.1", "127.0.0.1"], a.port_, 3.0)
    sock.close()
    assert host == "127.0.0.1" and time.time() - t0 < 2.0
    peer = paired(a, b)
    b.close_peer(peer.id)
    time.sleep(0.2)
    b.peers.update(peer.id, host="10.255.255.1", addrs=["127.0.0.1"])
    b.connect(b.peers.get(peer.id), timeout=3.0)
    assert b.peers.get(peer.id).host == "127.0.0.1"


def test_paired_devices_learn_each_others_addresses(pair):
    a, b = pair
    peer = paired(a, b)
    assert isinstance(b.peers.get(peer.id).addrs, list)
    assert isinstance(a.peers.get(b.identity.device_id).addrs, list)


def test_reconnect_and_revocation(pair):
    a, b = pair
    peer = paired(a, b)
    b.close_peer(peer.id)
    time.sleep(0.2)
    assert b.connected_ids() == []
    b.connect(b.peers.get(peer.id))
    assert b.request(peer.id, "echo", {"x": 1})["said"] == 1
    a.peers.remove(b.identity.device_id)                           # revoked on the PC
    b.close_peer(peer.id)
    time.sleep(0.2)
    with pytest.raises(LinkError):
        b.connect(b.peers.get(peer.id))


def test_permissions_by_role_and_override(pair):
    a, b = pair
    peer = paired(a, b, COLLABORATOR)
    assert a.peers.get(b.identity.device_id).role == COLLABORATOR
    with pytest.raises(LinkError) as e:
        b.request(peer.id, "chat")
    assert e.value.code == "denied"
    a.peers.update(b.identity.device_id, perms={"chat": ALLOW})
    assert b.request(peer.id, "chat") == {"ok": 1}


def test_ask_waits_for_the_owner_and_honours_no(pair):
    a, b = pair
    peer = paired(a, b, COLLABORATOR)
    assert a.peers.get(b.identity.device_id).permission("auto.send_prompt") == ASK
    result = {}

    def call():
        try:
            result["r"] = b.request(peer.id, "auto")
        except LinkError as err:
            result["e"] = err
    t = threading.Thread(target=call)
    t.start()
    deadline = time.time() + 3
    while not a.approvals.pending() and time.time() < deadline:
        time.sleep(0.02)
    pending = a.approvals.pending()
    assert pending and pending[0]["peer"] == "Beta Phone"
    assert a.approvals.resolve(pending[0]["id"], True)
    t.join(5)
    assert result.get("r") == {"ran": True}
    # and a "no"
    result.clear()
    t = threading.Thread(target=call)
    t.start()
    deadline = time.time() + 3
    while not a.approvals.pending() and time.time() < deadline:
        time.sleep(0.02)
    a.approvals.resolve(a.approvals.pending()[0]["id"], False)
    t.join(5)
    assert result["e"].code == "denied"


def test_ask_that_nobody_answers_is_a_no(pair):
    a, b = pair
    peer = paired(a, b, COLLABORATOR)
    a.approval_timeout = 0.5
    with pytest.raises(LinkError) as e:
        b.request(peer.id, "auto")                # the handler's own 3 s timeout; the owner never answers
    assert e.value.code == "denied"


def test_file_transfer_is_verified_sanitised_and_limited(pair):
    a, b = pair
    peer = paired(a, b)
    src = os.path.join(tempfile.mkdtemp(), "big file.bin")
    data = os.urandom(1024 * 1024 + 77)
    with open(src, "wb") as f:
        f.write(data)
    res = b.send_file(peer.id, src)
    got = [e for e in a.events if e[0] == "link.file"][-1][1]
    assert res["ok"] and hashlib.sha256(open(got["path"], "rb").read()).hexdigest() == hashlib.sha256(data).hexdigest()
    assert os.path.dirname(got["path"]).endswith("Beta Phone")
    # the same name again never overwrites
    b.send_file(peer.id, src)
    assert len({e[1]["path"] for e in a.events if e[0] == "link.file"}) == 2
    # too big for the 5 MB limit
    big = os.path.join(tempfile.mkdtemp(), "huge.bin")
    with open(big, "wb") as f:
        f.truncate(6 * 1024 * 1024)
    with pytest.raises(LinkError) as e:
        b.send_file(peer.id, big)
    assert e.value.code == "too_large"


def test_hostile_file_names_and_collaborator_executables():
    from modules.link.files import sanitize_filename, unique_path
    assert sanitize_filename("..\\..\\Windows\\system32\\evil.dll") == "evil.dll"
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("con.txt") == "_con.txt"
    assert sanitize_filename('a<b>:c"d|e?f*.txt') == "a_b__c_d_e_f_.txt"
    assert sanitize_filename("   ...   ") == "file"
    assert len(sanitize_filename("x" * 400 + ".pdf")) <= 150 and sanitize_filename("x" * 400 + ".pdf").endswith(".pdf")
    d = tempfile.mkdtemp()
    open(os.path.join(d, "a.txt"), "w").close()
    assert unique_path(d, "a.txt").endswith("a (2).txt")


def test_collaborator_executable_is_renamed_unsafe(pair):
    a, b = pair
    peer = paired(a, b, COLLABORATOR)
    src = os.path.join(tempfile.mkdtemp(), "setup.exe")
    with open(src, "wb") as f:
        f.write(b"MZ" + os.urandom(100))
    b.send_file(peer.id, src)
    got = [e for e in a.events if e[0] == "link.file"][-1][1]
    assert got["name"] == "setup.exe.unsafe" and got["unsafe"]


def test_large_message_is_fragmented_and_reassembled(pair):
    a, b = pair
    peer = paired(a, b)
    a.register("len", lambda ctx, d: {"n": len(d["s"])})
    assert b.request(peer.id, "len", {"s": "é" * 150_000}) == {"n": 150_000}


def test_unknown_request_and_a_crashing_handler_dont_kill_the_link(pair):
    a, b = pair
    peer = paired(a, b)
    with pytest.raises(LinkError) as e:
        b.request(peer.id, "nope")
    assert e.value.code == "unknown"
    a.register("boom", lambda ctx, d: 1 / 0)
    with pytest.raises(LinkError) as e:
        b.request(peer.id, "boom")
    assert e.value.code == "internal"
    assert b.request(peer.id, "echo", {"x": 1})["said"] == 1


def test_simultaneous_dial_settles_on_one_connection(pair):
    a, b = pair
    peer = paired(a, b)
    b.close_peer(peer.id)
    time.sleep(0.2)
    a.peers.update(b.identity.device_id, host="127.0.0.1", port=b.port_)
    errs = []

    def dial(node, pid):
        try:
            node.connect(node.peers.get(pid))
        except LinkError as e:
            errs.append(e)
    ts = [threading.Thread(target=dial, args=(a, b.identity.device_id)), threading.Thread(target=dial, args=(b, peer.id))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    time.sleep(0.5)
    assert b.request(peer.id, "echo", {"x": "still works"})["said"] == "still works"
    assert len([s for s in a.all_sessions()]) == 1 and len([s for s in b.all_sessions()]) == 1


def test_peer_store_names_and_permissions():
    store = PeerStore(os.path.join(tempfile.mkdtemp(), "peers.json"))
    store.save(Peer(id="g1", name="Gian", public_key="00" * 32, role=COLLABORATOR, platform="windows"))
    store.save(Peer(id="p1", name="Junior's iPhone", public_key="11" * 32, role=OWN, platform="ios"))
    assert store.find_by_name("Gian's PC").id == "g1"
    assert store.find_by_name("gian").id == "g1"
    assert store.find_by_name("my phone").id == "p1"
    assert store.find_by_name("somebody") is None
    g = store.get("g1")
    assert g.permission("auto.send_prompt") == ASK and g.permission("chat") == DENY and g.permission("nope") == DENY
    store.update("g1", perms={"chat": ALLOW, "bogus": ALLOW, "sync": "maybe"})
    assert store.get("g1").permission("chat") == ALLOW and "bogus" not in store.get("g1").perms


def test_addresses_and_pairing_links():
    assert parse_address("192.168.1.20:9000") == ("192.168.1.20", 9000)
    assert parse_address("192.168.1.20") == ("192.168.1.20", 8765)
    assert parse_address("[fe80::1]:9001") == ("fe80::1", 9001)
    offer = PairingManager().create(COLLABORATOR)
    info = parse_pair_uri(offer.uri("10.0.0.5", 8765, "My PC"))
    assert (info["host"], info["port"], info["role"], info["name"]) == ("10.0.0.5", 8765, "collaborator", "My PC")
    assert info["token"] == offer.token
    for bad in ("http://x", "saint://pair?h=1", "saint://other?h=1&p=2&t=x"):
        with pytest.raises(ValueError):
            parse_pair_uri(bad)


def test_identity_persists_and_ids_follow_the_key(monkeypatch):
    import keyring  # the test conftest swaps in an in-memory keyring
    d = tempfile.mkdtemp()
    path = os.path.join(d, "identity.json")
    one = Identity(path)
    pub, dev = one.public_key, one.device_id
    two = Identity(path)
    assert two.public_key == pub and two.device_id == dev
    assert two.private_key == one.private_key and len(dev) == 16

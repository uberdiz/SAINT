"""Voice commands for SAINT Link, the closed list of remote automations, approvals, and the service glue."""

import os
import tempfile
import threading
import time

import pytest

from core.config import config
from modules.agent import link_intents
from modules.agent.router import route
from modules.link import automations
from modules.link.approvals import ApprovalQueue
from modules.link.identity import ALLOW, ASK, COLLABORATOR, DENY, OWN, Identity, Peer, PeerStore
from modules.link.service import LinkService
from modules.link.sync import AliasAdapter, Mirror, SyncEngine
from modules.link.wire import LinkError


class FakeLink:
    def __init__(self):
        self.peers = PeerStore(os.path.join(tempfile.mkdtemp(), "peers.json"))
        self.peers.save(Peer(id="g1", name="Gian", public_key="00" * 32, role=COLLABORATOR, platform="windows"))
        self.peers.save(Peer(id="l1", name="Laptop", public_key="11" * 32, role=OWN, platform="windows"))
        self.calls = []
        self.running = True
        self.node = type("N", (), {"port": 8765})()

    def find_peer(self, spoken):
        return self.peers.find_by_name(spoken)

    def devices(self):
        return [dict(p.public(), connected=p.id == "g1") for p in self.peers.all()]

    def run_remote(self, peer, name, args, timeout=0):
        self.calls.append(("run_remote", peer.id, name, args))
        return {"text": f"did {name}"}

    def ask_peer(self, peer, text, timeout=0):
        self.calls.append(("ask_peer", peer.id, text))
        return {"text": "done over there", "expects_reply": False}

    def send_file(self, peer, path, progress=None):
        self.calls.append(("send_file", peer.id, path))
        return {"ok": True}

    def share(self, peer, kind, uid, data):
        self.calls.append(("share", peer.id, kind, uid, data))
        return {}

    def offer(self, role):
        self.calls.append(("offer", role))
        return {"addresses": ["192.168.1.20"], "port": 8765, "code": "X"}

    def sync_now(self):
        return 1

    def accept_shared(self, peer_id=None):
        self.calls.append(("accept", peer_id))
        return 2

    def unpair(self, peer_id):
        self.calls.append(("unpair", peer_id))

    def set_enabled(self, on):
        self.calls.append(("enable", on))
        return True


@pytest.fixture()
def link(monkeypatch):
    fake = FakeLink()
    monkeypatch.setattr(link_intents, "_link", lambda: fake)
    return fake


def run(text):
    intent = route(text)
    assert intent is not None and intent.domain == "link", (text, intent and intent.name)
    return intent, intent.run()


# ---------------------------------------------------------------------- #
# Voice commands
# ---------------------------------------------------------------------- #
def test_send_a_prompt_to_someones_pc(link):
    intent, reply = run("send this prompt to Gian's PC on Claude: summarize my notes and open the door")
    assert intent.name == "link.send_prompt"
    assert link.calls == [("run_remote", "g1", "send_prompt", {"target": "claude", "prompt": "summarize my notes and open the door"})]
    assert reply.ok and reply.text == "did send_prompt"


@pytest.mark.parametrize("text", [
    "send this prompt to gian on chatgpt write a haiku about rain",
    "type the following prompt into Gian's computer on ChatGPT: write a haiku about rain",
    "Hey SAINT, send a prompt to Gian's PC with ChatGPT, write a haiku about rain",
])
def test_prompt_phrasings(link, text):
    intent, _ = run(text)
    assert link.calls[-1][2:] == ("send_prompt", {"target": "chatgpt", "prompt": "write a haiku about rain"})


def test_prompt_to_unknown_device_is_declined_not_guessed(link):
    intent, reply = run("send this prompt to Zed's PC on Claude: hi")
    assert not reply.ok and "Zed" in reply.text and "Gian" in reply.text and link.calls == []


def test_messages_music_links_and_questions(link):
    run("send gian a message saying I'm running late")
    assert link.calls[-1] == ("run_remote", "g1", "message", {"text": "I'm running late"})
    run("tell Gian that dinner is ready")
    assert link.calls[-1][3] == {"text": "dinner is ready"}
    run("play Tití Me Preguntó on Gian's PC")
    assert link.calls[-1][2:] == ("play_music", {"query": "Tití Me Preguntó"})
    run("open https://example.com/x on Gian's PC")
    assert link.calls[-1][2:] == ("open_url", {"url": "https://example.com/x"})
    intent, reply = run("ask Gian's saint what the capital of France is")
    assert link.calls[-1][2:] == ("ask", {"question": "what the capital of France is"}) and "Gian's SAINT says" in reply.text


def test_ordinary_sentences_are_not_hijacked(link):
    for text in ("tell me that joke again", "tell the kids dinner is ready", "play Blinding Lights on Spotify",
                 "send an email to Gian", "what devices does this laptop support", "open https://example.com",
                 "list my audio devices", "send it to the printer"):
        assert link_intents.parse_link(text) is None, text
    assert link.calls == []


def test_commands_for_my_own_devices_run_there(link):
    intent, reply = run("ask my laptop to lock itself")
    assert intent.name == "link.remote_command" and link.calls[-1] == ("ask_peer", "l1", "lock itself")
    assert reply.text == "done over there"
    # a friend's PC is never handed a free-form command
    assert link_intents.parse_link("ask Gian to lock his pc") is None


def test_sending_a_file_uses_what_was_just_made(link, tmp_path):
    from modules.agent.recent import recent
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")
    recent.clear()
    recent.note("screenshot", "the screenshot of Claude", str(shot), "took")
    try:
        intent, reply = run("send that to Gian")
        assert link.calls[-1] == ("send_file", "g1", str(shot)) and "Sent the screenshot" in reply.text
        recent.clear()
        intent, reply = run("send the screenshot to my laptop")
        assert not reply.ok and "file to send" in reply.text
    finally:
        recent.clear()


def test_pairing_listing_syncing_sharing_and_housekeeping(link):
    assert run("pair my phone")[0].name == "link.pair" and link.calls[-1] == ("offer", "own")
    assert run("add a friend")[0].name == "link.pair_friend" and link.calls[-1] == ("offer", "collaborator")
    _, reply = run("what devices are connected")
    assert "Connected: Gian" in reply.text and "Not connected: Laptop" in reply.text
    assert run("sync my devices")[1].text == "Synced."
    assert "Kept 2" in run("accept what Gian shared")[1].text and link.calls[-1] == ("accept", "g1")
    run("turn on SAINT Link")
    assert link.calls[-1] == ("enable", True)
    _, reply = run("disconnect Gian")
    assert link.calls[-1] == ("unpair", "g1") and "Gian" in reply.text
    assert link_intents.parse_link("remove downloads") is None


def test_sharing_a_scene_with_a_friend(link):
    from modules.automation.scenes import Scene, scenes
    scenes.save(Scene("Link test scene", ["play lofi"], phrase="", id="lnk00001"))
    try:
        run("share my scene Link test scene with Gian")
        kind = link.calls[-1]
        assert kind[:4] == ("share", "g1", "scene", "lnk00001") and kind[4]["steps"] == ["play lofi"]
        _, reply = run("share my scene nothing here with Gian")
        assert not reply.ok
    finally:
        scenes.delete("lnk00001")


def test_spanish_reaches_the_same_intent(link):
    from modules import lang
    turn = lang.analyze("manda este prompt al PC de Gian en Claude: resume mis notas de hoy")
    assert turn.english == "send this prompt to Gian's pc on Claude: resume mis notas de hoy"
    run(turn.english)
    assert link.calls[-1][2:] == ("send_prompt", {"target": "claude", "prompt": "resume mis notas de hoy"})


def test_the_prompt_text_is_never_split_into_other_commands(link):
    """"... and lock it" inside a prompt must not become a second command on this PC."""
    intent = route("send this prompt to Gian's PC on Claude: open the door and then lock my pc")
    assert intent.name == "link.send_prompt"


# ---------------------------------------------------------------------- #
# The closed list of remote automations
# ---------------------------------------------------------------------- #
class Ctx:
    def __init__(self, peer, allow=True):
        self.peer, self.allow = peer, allow
        self.node = type("N", (), {"emit": lambda s, n, p: EVENTS.append((n, p))})()
        self.asked = []

    def require(self, perm, describe="", timeout=None):
        self.asked.append((perm, describe))
        if not self.allow:
            raise LinkError("said no", "denied")


EVENTS = []


@pytest.fixture()
def tools(monkeypatch):
    calls = []

    def fake(name, /, **kw):
        calls.append((name, kw))
        return {"name": "Song", "artist": "Artist"} if name == "spotify.play_query" else {}
    monkeypatch.setattr(automations, "_tool", fake)
    monkeypatch.setattr(automations.time, "sleep", lambda s: None)
    EVENTS.clear()
    return calls


def peer(role=COLLABORATOR):
    return Peer(id="g1", name="Gian", public_key="00" * 32, role=role)


def test_send_prompt_opens_the_app_types_line_by_line_and_sends(tools):
    ctx = Ctx(peer())
    res = automations.run(ctx, "send_prompt", {"target": "Claude", "prompt": "line one\nline two"})
    assert res["ok"] and res["text"] == "Sent to Claude."
    names = [c[0] for c in tools]
    assert names[0] == "desktop.open_app" and tools[0][1] == {"name": "Claude"}
    assert [c[1].get("keys") for c in tools if c[0] == "desktop.press_keys"] == ["shift+enter", "enter"]
    assert [c[1]["text"] for c in tools if c[0] == "desktop.type_text"] == ["line one", "line two"]
    assert ctx.asked[0][0] == "auto.send_prompt" and "line one" in ctx.asked[0][1]


def test_send_prompt_falls_back_to_the_website_and_chunks_long_text(tools, monkeypatch):
    def fake(name, /, **kw):
        tools.append((name, kw))
        if name == "desktop.open_app":
            raise LinkError("no such app", "not_found")
        return {}
    monkeypatch.setattr(automations, "_tool", fake)
    config.set("desktop.max_type_length", 10, persist=False)
    try:
        automations.run(Ctx(peer()), "send_prompt", {"target": "claude", "prompt": "x" * 25})
    finally:
        config.set("desktop.max_type_length", 500, persist=False)
    assert ("desktop.open_url", {"url": "https://claude.ai/new"}) in tools
    assert [len(c[1]["text"]) for c in tools if c[0] == "desktop.type_text"] == [10, 10, 5]


def test_nothing_runs_when_the_owner_says_no(tools):
    with pytest.raises(LinkError) as e:
        automations.run(Ctx(peer(), allow=False), "send_prompt", {"target": "claude", "prompt": "hi"})
    assert e.value.code == "denied" and tools == []


@pytest.mark.parametrize("name,args,code", [
    ("send_prompt", {"target": "notepad", "prompt": "hi"}, "bad_args"),
    ("send_prompt", {"target": "claude", "prompt": ""}, "bad_args"),
    ("send_prompt", {"target": "claude", "prompt": "x" * 5000}, "bad_args"),
    ("send_prompt", {"target": "claude", "prompt": "a\x00b"}, "bad_args"),
    ("open_url", {"url": "file:///C:/Windows/system32/cmd.exe"}, "bad_args"),
    ("open_url", {"url": "javascript:alert(1)"}, "bad_args"),
    ("open_url", {"url": "https://ok.example\nhttps://evil.example"}, "bad_args"),
    ("play_music", {"query": "x" * 500}, "bad_args"),
    ("run_scene", {"scene": "Not shared"}, "not_shared"),
    ("format_c", {}, "unknown_automation"),
    ("message", {"text": 5}, "bad_args"),
])
def test_bad_input_is_refused_before_anything_happens(tools, name, args, code):
    with pytest.raises(LinkError) as e:
        automations.run(Ctx(peer()), name, args)
    assert e.value.code == code and tools == []


def test_message_music_and_open_url(tools):
    automations.run(Ctx(peer()), "message", {"text": "dinner!"})
    assert EVENTS[-1] == ("link.message", {"peer_id": "g1", "peer": "Gian", "text": "dinner!"})
    assert automations.run(Ctx(peer()), "play_music", {"query": "daft punk"})["text"] == "Playing Song by Artist."
    assert automations.run(Ctx(peer()), "open_url", {"url": "https://example.com"})["text"] == "Opened."
    assert ("desktop.open_url", {"url": "https://example.com"}) in tools


def test_only_shared_scenes_can_be_run_remotely(tools):
    from modules.automation.scenes import Scene, scenes
    scenes.save(Scene("Remote scene", ["play lofi"], id="rem00001"))
    ran = []
    orig = scenes.run_in_background
    scenes.run_in_background = lambda scene, *a, **k: ran.append(scene.name)
    try:
        with pytest.raises(LinkError):
            automations.run(Ctx(peer(OWN)), "run_scene", {"scene": "Remote scene"})
        config.set("link.shared_scenes", ["Remote scene"], persist=False)
        assert automations.run(Ctx(peer()), "run_scene", {"scene": "remote scene"})["text"] == "Running Remote scene."
        assert ran == ["Remote scene"]
    finally:
        scenes.run_in_background = orig
        config.set("link.shared_scenes", [], persist=False)
        scenes.delete("rem00001")


def test_catalog_says_what_would_happen_for_a_peer():
    cat = {a["name"]: a for a in automations.catalog(peer())}
    assert cat["send_prompt"]["policy"] == ASK and cat["run_scene"]["policy"] == DENY and cat["message"]["policy"] == ALLOW
    assert {a["policy"] for a in automations.catalog(peer(OWN))} == {ALLOW}


# ---------------------------------------------------------------------- #
# Approvals by voice and on screen
# ---------------------------------------------------------------------- #
@pytest.fixture()
def queue():
    from modules.agent.confirm import confirmations
    confirmations.clear("test")
    spoken = []
    q = ApprovalQueue(emit=lambda n, p: None, announce=spoken.append)
    q.spoken = spoken
    yield q
    confirmations.clear("test")


def _ask_in_thread(q, out, voice=True):
    t = threading.Thread(target=lambda: out.append(q.request("g1", "Gian", "type a prompt into Claude", timeout=5)))
    t.start()
    deadline = time.time() + 3
    # the on-screen request is open a moment before the spoken question is asked: wait for both
    while (not q.pending() or (voice and not q.spoken)) and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.05)
    return t


def test_yes_said_out_loud_allows_it(queue):
    from modules.agent.confirm import confirmations
    out = []
    t = _ask_in_thread(queue, out)
    assert queue.spoken == ["Gian wants to type a prompt into Claude. Allow it?"]
    assert confirmations.pending is not None
    assert confirmations.resolve("yes, go ahead") == "Okay, allowing it."
    t.join(5)
    assert out == [True]


def test_no_said_out_loud_denies_it(queue):
    out = []
    t = _ask_in_thread(queue, out)
    from modules.agent.confirm import confirmations
    confirmations.resolve("no")
    t.join(5)
    assert out == [False]


def test_the_buttons_on_screen_work_and_a_late_answer_is_too_late(queue):
    out = []
    t = _ask_in_thread(queue, out)
    a = queue.pending()[0]
    assert queue.resolve(a["id"], True) and not queue.resolve(a["id"], False)
    t.join(5)
    assert out == [True] and queue.pending() == []
    assert not queue.resolve(9999, True)


def test_silence_is_a_no(queue):
    assert queue.request("g1", "Gian", "do something", timeout=0.4) is False


def test_a_second_request_while_a_question_is_open_still_works_on_screen(queue):
    from modules.agent.confirm import PendingAction, confirmations
    confirmations.ask(PendingAction("something else", lambda: "ok"))
    out = []
    t = _ask_in_thread(queue, out, voice=False)
    assert queue.spoken == []                               # the voice question is taken; the buttons remain
    queue.resolve(queue.pending()[0]["id"], True)
    t.join(5)
    assert out == [True]


# ---------------------------------------------------------------------- #
# The service
# ---------------------------------------------------------------------- #
@pytest.fixture()
def service():
    config.set("agent.enabled", True, persist=False)          # another test file can leave it off
    d = tempfile.mkdtemp()
    ident = Identity(os.path.join(d, "identity.json"))
    ident.set_name("Test PC")
    svc = LinkService(ident, PeerStore(os.path.join(d, "peers.json")),
                      SyncEngine(ident.device_id, [AliasAdapter()], Mirror(os.path.join(d, "sync.db"))))
    svc.shared_inbox = link_intents_shared(d)
    yield svc
    svc.stop()


def link_intents_shared(d):
    from modules.link.service import SharedInbox
    return SharedInbox(os.path.join(d, "shared.json"))


def test_a_phone_asking_this_pc_gets_the_agents_answer_in_its_language(service):
    out = service.process_text("what time is it")
    assert out["text"].startswith("It's ") and out["lang"] == "en" and not out["expects_reply"]
    out = service.process_text("¿qué hora es?")
    assert out["text"].startswith("Son las ") and out["lang"] == "es"
    out = service.process_text("what time is it", language="en", source="Phone")
    from modules.link.context_feed import context_feed
    assert any("Phone" in i["source"] for i in context_feed.recent())
    context_feed.clear()


def test_chat_handler_needs_the_chat_permission(service):
    p = Peer(id="c1", name="Friend", public_key="22" * 32, role=COLLABORATOR)
    service.peers.save(p)
    ctx = type("C", (), {"peer": p, "require": lambda s, perm, describe="", timeout=None: (_ for _ in ()).throw(LinkError("no", "denied"))})()
    with pytest.raises(LinkError):
        service._h_chat(ctx, {"text": "hi"})
    with pytest.raises(LinkError):
        service._h_chat(type("C", (), {"peer": p, "require": lambda *a, **k: None})(), {"text": ""})


def test_enable_disable_and_listening(service):
    config.set("link.port", 0, persist=False)
    config.set("link.bind", "127.0.0.1", persist=False)
    config.set("link.discoverable", False, persist=False)
    try:
        assert service.set_enabled(True) and service.running and service.node.port > 0
        info = service.offer("own")
        assert info["code"].count("-") == 1 and info["uri"].startswith("saint://pair?") and info["port"] == service.node.port
        assert service.status()["pairing"]["role"] == "own"
        assert service.set_enabled(False) is False and not service.running and service.pairing.current is None
    finally:
        config.set("link.enabled", False, persist=False)
        config.set("link.port", 8765, persist=False)
        config.set("link.bind", "0.0.0.0", persist=False)
        config.set("link.discoverable", True, persist=False)


def test_offer_refuses_when_link_is_off(service):
    config.set("link.enabled", False, persist=False)
    with pytest.raises(LinkError):
        service.offer("own")


def test_a_friends_shared_things_wait_for_a_yes(service):
    from modules.agent.aliases import aliases
    p = Peer(id="c1", name="Friend", public_key="22" * 32, role=COLLABORATOR)
    service.peers.save(p)
    ctx = type("C", (), {"peer": p, "require": lambda *a, **k: None})()
    service._h_share(ctx, {"items": [{"k": "alias", "u": "the shared thing", "d": {"target": "open shared thing"}},
                                     {"k": "files", "u": "x", "d": {}}, {"k": "alias", "d": "not a dict"}]})
    pending = service.shared_inbox.pending()
    assert len(pending) == 1 and "the shared thing" not in aliases.all()
    try:
        assert service.accept_shared() == 1
        assert aliases.all()["the shared thing"] == "open shared thing" and service.shared_inbox.pending() == []
    finally:
        aliases.forget("the shared thing")


def test_sync_handlers_are_for_your_own_devices_only(service):
    friend = Peer(id="c1", name="Friend", public_key="22" * 32, role=COLLABORATOR)
    ok = lambda *a, **k: None
    with pytest.raises(LinkError):
        service._h_sync_manifest(type("C", (), {"peer": friend, "require": ok})(), {"manifest": {}})
    with pytest.raises(LinkError):
        service._h_sync_push(type("C", (), {"peer": friend, "require": ok})(), {"items": []})
    own = Peer(id="o1", name="Phone", public_key="33" * 32, role=OWN)
    assert service._h_sync_manifest(type("C", (), {"peer": own, "require": ok})(), {"manifest": {}}) == {"want": [], "items": []}


def test_context_from_other_devices_reaches_the_language_model_prompt():
    from modules.link.context_feed import ContextFeed
    feed = ContextFeed()
    feed.add("Junior's iPhone", "remind me to call mom at 5", "Okay — reminder set.", ts=time.time() - 120)
    text = feed.describe()
    assert "2 min ago, on Junior's iPhone" in text and "call mom" in text
    feed.add("old", "x", ts=time.time() - 3600)
    assert "old" not in feed.describe()


def test_reminder_message_isnt_mangled_when_the_time_words_overlap():
    """"this evening" contains "evening": both matches used to be cut out of the text, one after the other."""
    from datetime import datetime
    from modules.automation.timeparse import extract_reminder
    rem = extract_reminder("remind me this evening to call mom", datetime(2026, 9, 30, 12, 0))
    assert rem["message"] == "Call mom" and rem["schedule"]["type"] == "once"
    assert extract_reminder("remind me on saturday morning to go running", datetime(2026, 9, 30, 12, 0))["message"] == "Go running"


def test_this_pcs_history_lesson_answers_and_journal_sync(tmp_path, monkeypatch):
    """2026-10-02: share logs, automations and what was learned between all your devices — the PC's own
    History now goes out (the phone only sent its Activity before), with lesson answers and the journal."""
    from modules.link.sync import ActionLogAdapter, AnswerAdapter, JournalAdapter, SyncEngine, Mirror
    import modules.learning.lesson as L
    import modules.learning.feedback as F
    t = time.time()
    hist = [{"ts": t - 60, "source": "voice", "user": "open spotify", "reply": "Opened Spotify.",
             "tools": [{"tool": "desktop.open_app", "ok": True}]},
            {"ts": t - 30, "source": "iphone", "user": "from the phone", "reply": "x", "tools": []}]
    a_log = ActionLogAdapter(str(tmp_path / "a_log.json"), history_source=lambda: hist, own=lambda: ("aaaa1111bbbb", "Desk"))
    b_log = ActionLogAdapter(str(tmp_path / "b_log.json"), history_source=lambda: [], own=lambda: ("cccc2222dddd", "Laptop"))
    snap = a_log.snapshot()
    assert len(snap) == 1 and next(iter(snap.values()))["device"] == "Desk"     # phone entries aren't re-offered
    monkeypatch.setattr(L, "answers", L.Answers(str(tmp_path / "answers_a.json")))
    L.answers.put("What's Mr Norton's email address?", "jnorton@essextech.net")
    monkeypatch.setattr(F, "journal", F.Journal(str(tmp_path / "journal_a.jsonl")))
    F.journal.add("complaint", "open my mail", did="opened Outlook")
    a = SyncEngine("aaaa1111bbbb", [a_log, AnswerAdapter(), JournalAdapter()], Mirror(str(tmp_path / "a.db")))
    a.scan()
    want, offer = a.diff({"clock": 0, "items": {}, "kinds": ["actionlog", "answer"]})
    assert {i["k"] for i in offer} == {"actionlog", "answer"}                     # no journal for a device without one
    appended = []
    monkeypatch.setattr("core.history.history.append", lambda rec: appended.append(rec))
    b = SyncEngine("cccc2222dddd", [b_log], Mirror(str(tmp_path / "b.db")))
    assert b.apply([i for i in offer if i["k"] == "actionlog"]) == 1
    assert appended and appended[0]["source"] == "remote" and appended[0]["device"] == "Desk"
    # coming back to the PC it came from, its own entry is ignored
    assert a_log.apply_batch([(next(iter(snap)), snap[next(iter(snap))])]) == [] and not a_log._load()


def test_typing_only_the_code_finds_the_pc_with_the_window_open(service, monkeypatch):
    """2026-10-02: the 26-character code plus an address was too much for pairing two PCs. Now the other
    PC's beacon says its window is open, and the 8-character code alone is enough."""
    from modules.link.node import LinkNode
    d = tempfile.mkdtemp()
    other_ident = Identity(os.path.join(d, "identity.json"))
    other_ident.set_name("Other PC")
    other = LinkNode(other_ident, PeerStore(os.path.join(d, "peers.json")))
    port = other.start("127.0.0.1", 0)
    try:
        offer = other.pairing.create("own")

        class FakeDiscovery:
            probed = 0

            def probe(self):
                self.probed += 1

            def nearby(self, pairing_only=False):
                return [{"id": other_ident.device_id, "host": "127.0.0.1", "port": port, "pairing": "own",
                         "name": "Other PC", "at": time.time()}]
        service._discovery = FakeDiscovery()
        service.node.start("127.0.0.1", 0)
        monkeypatch.setattr("modules.link.service.tailscale_status", lambda max_age=60.0: {})
        peer = service.pair(offer.code.lower().replace("-", " "))
        assert peer.name == "Other PC" and peer.role == "own" and service._discovery.probed == 1
        # a wrong code is reported plainly
        other.pairing.create("own")
        with pytest.raises(LinkError) as e:
            service.pair("ABCD-EFGH")
        assert "didn't work" in str(e.value)
    finally:
        service._discovery = None
        other.stop()

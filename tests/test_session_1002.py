"""2026-10-02: interrupting a long automation, learning long tasks by being walked
through them (modules/learning/lesson.py), and "write an email" no longer typing
the words "an email"."""

import threading
import time

import pytest

from core.config import config


# ------------------------------------------------------------------ interrupting
class _FakeAI:
    """A turn that runs a long automation (like run_plan: stops between steps
    when the user says stop), and turns that answer at once."""

    def __init__(self):
        self.started, self.stopped_early = [], []
        self.expects_reply = False
        self.context = None

    def stream_prompt(self, prompt, on_token, **_kw):
        from core.cancel import cancel
        self.started.append(prompt)
        if prompt == "do the long thing":
            tok = cancel.token()
            for _ in range(100):                   # ~10 s of steps
                if tok.cancelled:
                    self.stopped_early.append(prompt)
                    return
                time.sleep(0.1)
        on_token("Done.")

    def cancel(self):
        pass


class _FakeVoice:
    def set_saint_speaking(self, _on):
        pass

    def set_tts_playback_active(self, _on):
        pass


@pytest.fixture
def controller():
    from core.conversation import ConversationController
    ai = _FakeAI()
    ctrl = ConversationController(ai, _FakeVoice(), None)
    yield ctrl, ai
    ctrl.shutdown()


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_a_new_request_stops_a_long_automation_instead_of_waiting_behind_it(controller):
    ctrl, ai = controller
    ctrl._handle_user_speech("do the long thing", session_id=1, addressed=True)
    assert _wait(lambda: ai.started == ["do the long thing"])
    t0 = time.time()
    ctrl._handle_user_speech("open spotify", session_id=2, addressed=True)
    # The automation stops at its next step and the new request runs — it used to
    # wait 5 s for the lock and then be dropped ("Previous request is still running").
    assert _wait(lambda: "open spotify" in ai.started, timeout=3.0)
    assert ai.stopped_early == ["do the long thing"] and time.time() - t0 < 3.0


def test_answering_while_saint_speaks_a_question_does_not_cancel_anything(controller, monkeypatch):
    from core.cancel import cancel
    from core.conversation import ConvState
    ctrl, _ai = controller
    gen = cancel.generation
    ctrl._thinking_lock.acquire()               # the question's turn is still finishing its speech
    try:
        ctrl._set_state(ConvState.SPEAKING)
        monkeypatch.setattr(ctrl, "_start_thinking", lambda *a, **k: None)
        ctrl._handle_user_speech("school", session_id=3, addressed=True)
    finally:
        ctrl._thinking_lock.release()
    assert cancel.generation == gen


def test_stop_said_over_saints_own_sentence_is_heard():
    from modules.voice.module import VoiceModule
    v = VoiceModule.__new__(VoiceModule)
    v._tts_text = "Here's what I found about the weather in Boston today."
    heard = "Here's what I found about the weather stop"
    m = v._STOP_PHRASE.search(heard)
    assert m and not v._saint_said(m.group(0)) and v._is_own_echo(heard)   # mostly echo, but "stop" is the user's
    v._tts_text = "Stopped the timer. Anything else?"
    assert v._saint_said("stopped") and not v._saint_said("stop")


# ------------------------------------------------------------------ "write an email"
def test_write_an_email_is_a_task_not_text_to_type():
    from modules.agent.dictate import dictation
    from modules.agent.router import route
    for said in ("write an email", "write an email to my teacher", "write the email", "write me a message to bob"):
        assert route(said) is None, said
    assert route("write hello world").name == "desktop.type_text"
    assert dictation.START_RE.match("write an email about the trip") is None
    assert dictation.START_RE.match("help me write a prompt about chess")


# ------------------------------------------------------------------ lessons
@pytest.fixture
def lesson_env(monkeypatch, tmp_path):
    """Real routing and lesson code; the desktop, the browser and the model are fakes."""
    from modules.automation.tools import ToolResult, get_tool_registry
    import modules.learning.lesson as L
    import modules.learning.planner as planner
    import modules.learning.skills as sk
    from modules.learning import intents as learning_intents
    from modules.learning.skills import SkillStore
    store = SkillStore(str(tmp_path / "skills.json"))
    for mod in (sk, L, planner, learning_intents):
        monkeypatch.setattr(mod, "skills", store)
    monkeypatch.setattr(L, "answers", L.Answers(str(tmp_path / "answers.json")))
    monkeypatch.setattr(planner, "attempt", lambda *a, **k: None)
    monkeypatch.setitem(config._data.setdefault("learning", {}), "email_skills_retired", True)
    tools = []

    class Result(dict):
        def __missing__(self, key):
            return ""

    def execute(_tool_name, **kw):
        tools.append((_tool_name, {k: v for k, v in kw.items() if not k.startswith("_")}))
        if "missing" in str(kw.get("name", "")):
            return ToolResult(success=False, error=f"I couldn't find {kw['name']} on the screen.")
        return ToolResult(success=True, result=Result(clicked=kw.get("name", ""), changed=True, title="Gmail"))
    monkeypatch.setattr(get_tool_registry(), "execute", execute)
    drafts, systems = [], []

    def complete(prompt, system="", timeout=45.0, max_tokens=0):
        drafts.append(prompt)
        systems.append(system)
        if prompt.startswith("Write a short subject"):
            return "Lunch tomorrow"
        return "Hi Bob, short." if "Change it like this" in prompt else "Hi Bob,\nStill on for lunch?\nSeb"
    monkeypatch.setattr("modules.agent.llm.complete", complete)
    L.lessons.stop()
    yield {"tools": tools, "skills": store, "drafts": drafts, "systems": systems, "lessons": L.lessons}
    L.lessons.stop()


def _say(*lines):
    from modules.agent.agent import agent
    out = None
    for line in lines:
        out = agent.handle(line)
    return out


def _clicked(tools):
    return [kw.get("name") for name, kw in tools if name == "desktop.click_element"]


EMAIL_LESSON = [
    "write an email",
    "go to https://mail.google.com/mail/u/0/#inbox",
    "ask me which email to send from, personal or school", "school",
    "click me@school.edu", "yes", "me@gmail.com",
    "click compose",
    "ask me who it's to", "sam@example.com",
    "type it in the to box",
    "ask me what to write about", "the trip",
    "write a short subject line", "click the subject box", "type the subject",
    "write the email", "click the message body", "type the email",
    "ask me before you send it", "click send",
]


def test_an_unknown_task_asks_how_instead_of_guessing(lesson_env):
    res = _say("write an email")
    assert not res.ok and res.expects_reply and "Walk me through" in res.text
    assert lesson_env["tools"] == [] and lesson_env["lessons"].teaching
    assert "Okay" in _say("no").text and not lesson_env["lessons"].active


def test_an_email_lesson_is_taught_once_then_run_with_its_questions(lesson_env):
    tools = lesson_env["tools"]
    replies = {}
    for line in EMAIL_LESSON:
        res = _say(line)
        assert res.expects_reply, (line, res.text)
        replies[line] = res.text
    assert replies["click me@school.edu"].endswith(
        "Is “me@school.edu” the one for “school”?")
    assert replies["yes"] == "And what should I use for “personal”?"
    saved = _say("done")
    assert "Saved" in saved.text and "Which email should I send from, personal or school?" in saved.text
    steps = lesson_env["skills"].all()[0].steps
    assert steps[0] == "go to https://mail.google.com/mail/u/0/#inbox"
    assert steps[1].startswith("ask: Which email should I send from, personal or school? -> account (")
    assert "school = me@school.edu" in steps[1] and "personal = me@gmail.com" in steps[1]
    assert "click {account}" in steps and "type {recipient} into the to box" in steps
    assert "write: a short subject line -> subject" in steps and "type {subject}" in steps
    assert "confirm: Should I send it?" in steps and steps[-1] == "click send"

    # Next time: what the request already says isn't asked again.
    tools.clear()
    res = _say("send an email to bob@x.com about lunch tomorrow")
    assert res.text == "Which email should I send from, personal or school?"
    res = _say("personal")
    assert "Here's the subject: Lunch tomorrow." in res.text
    assert _clicked(tools)[:2] == ["me@gmail.com", "compose"]
    assert ("desktop.type_text", {"text": "bob@x.com", "target": "to box"}) in tools
    res = _say("yes")
    assert "Here's the message" in res.text
    assert "topic: lunch tomorrow" in lesson_env["drafts"][-1]
    res = _say("make it shorter")
    assert "Hi Bob, short." in res.text
    assert _say("sure").text == "Should I send it?"
    assert ("desktop.type_text", {"text": "Hi Bob, short."}) in tools
    res = _say("no")                                    # nothing after the question runs without a yes
    assert "stopped before “click send”" in res.text and "send" not in _clicked(tools)
    assert not lesson_env["lessons"].active


def test_a_step_that_stops_working_is_asked_about_and_replaced(lesson_env):
    store = lesson_env["skills"]
    store.learn("post on reddit", ["go to reddit.com", "click missing button", "ask: What's the title? -> title",
                                   "type {title}"], "lesson")
    res = _say("post on reddit")
    assert not res.ok and res.expects_reply and "step 2" in res.text and "How should I do it now?" in res.text
    res = _say("click create post")
    assert "that's how I'll do it from now on" in res.text and res.text.endswith("What's the title?")
    assert store.all()[0].steps[1] == "click create post"
    _say("My cat")
    assert ("desktop.type_text", {"text": "My cat"}) in lesson_env["tools"]


def test_stop_ends_a_lesson_and_what_the_user_does_is_waited_for(lesson_env):
    lessons = lesson_env["lessons"]
    res = _say("teach you how to post on reddit", "go to reddit.com", "hover over the missing profile")
    assert not res.ok and "do it yourself" in res.text
    assert "that part's yours" in _say("I did it").text
    res = _say("click create post")
    assert res.expects_reply
    _say("done")
    assert lesson_env["skills"].all()[0].steps == ["go to reddit.com", "you: hover over the missing profile",
                                                   "click create post"]
    res = _say("post on reddit")
    assert res.text == "Your turn: hover over the missing profile. Say “next” when it's done."
    assert _say("stop").text == "Okay, I stopped." and not lessons.active


def test_an_ask_once_answer_is_remembered_for_that_question(lesson_env):
    store = lesson_env["skills"]
    store.learn("message my teacher", ["ask: Who's it to? -> recipient",
                                       "ask once: What's {recipient}'s email? -> recipient_email",
                                       "type {recipient_email}"], "lesson")
    assert _say("message my teacher").text == "Who's it to?"
    assert _say("Mr Smith").text == "What's Mr Smith's email?"
    _say("smith@school.org")
    assert _say("message my teacher").text == "Who's it to?"
    _say("Mr Smith")                                    # remembered: not asked again
    assert lesson_env["tools"][-1] == ("desktop.type_text", {"text": "smith@school.org"})


def test_teaching_phrases_become_the_right_kind_of_step():
    from modules.learning import lesson as L
    assert L.direct_question("which account to send from, personal or school") == \
        "Which account should I send from, personal or school?"
    assert L.direct_question("who it's to") == "Who's it to?"
    assert L.direct_question("what to write about") == "What should I write about?"
    assert L.direct_question("for their email", owner="{recipient}") == "What's {recipient}'s email?"
    assert L.parse("ask: Which one, personal or school? -> account").options == ["personal", "school"]
    assert L.start_phrase("let me teach you how to send an email") == "send an email"
    assert L.start_phrase("I want to learn how to cook") is None
    assert L.is_task("send an email to Sam") and L.is_task("fill out the form") and not L.is_task("open discord")
    assert L.canon("compose an e-mail to Sam") == L.canon("write an email to Sam") == "send a email to sam"
    assert L.split_request("Write an email to Sam about the Trip") == ("write an email",
                                                                       {"to": "Sam", "about": "the Trip"})
    line = "ask once: What's {recipient}'s email? -> recipient_email (a = b)"
    assert L.parse(line).line() == line and L.is_lesson([line]) and not L.is_lesson(["open discord"])


def test_an_email_skill_learned_the_old_way_is_forgotten_once(lesson_env, monkeypatch):
    from modules.learning import lesson as L
    store = lesson_env["skills"]
    store.learn("write an email", ["open gmail"], "planned")
    store.learn("open my mail", ["open gmail"], "planned")
    monkeypatch.setattr(config, "save", lambda *a, **k: None)
    monkeypatch.setitem(config._data["learning"], "email_skills_retired", False)
    L.retire_guessed_email_skills()
    assert [s.phrase for s in store.all()] == ["open my mail"]
    assert config.get("learning.email_skills_retired") is True
    res = _say("write an email")
    assert "Walk me through" in res.text


# ------------------------------------------------------------------ 2026-10-02 evening: the Norton email
def test_the_norton_email_lesson_is_summarized_and_parameterized(lesson_env):
    """The lesson as it was taught: the address typed (not an email composed), the correction not kept as a
    step, "type out a summary of what SAINT is" written by the model (not the words typed), and the saved
    lesson asks who it's to and what it's about next time."""
    tools, drafts = lesson_env["tools"], lesson_env["drafts"]
    _say("write an email")
    _say("go to https://mail.google.com/mail/u/0/#inbox")
    _say("Click Compose")
    res = _say("write mr norton's email (jnorton@school.example.org)")
    assert res.ok and "Mr Norton's address" in res.text, res.text
    assert ("desktop.type_text", {"text": "jnorton@school.example.org"}) in tools
    res = _say("no only write the email")
    assert "just the address" in res.text
    _say("Click Subject")
    res = _say("type out a summary of what SAINT is")
    assert res.ok and "typed it" in res.text, res.text
    assert "summary of what SAINT is" in drafts[-1]
    assert not any(kw.get("text") == "a summary of what SAINT is" for _n, kw in tools)
    saved = _say("done")
    assert "Saved" in saved.text and "Who's it to?" in saved.text and "What's it about?" in saved.text
    steps = lesson_env["skills"].all()[0].steps
    assert steps[:3] == ["ask: Who's it to? -> recipient", "ask: What's it about? -> topic",
                         "ask: Anything it has to say? (or “no”) -> points"]
    assert "ask once: What's {recipient}'s email address? -> email" in steps and "type {email}" in steps
    assert steps.count("type {email}") == 1 and not any("no only" in s for s in steps)
    assert "write: the email's text about {topic} -> message" in steps and "type {message}" in steps

    # Next time, for the same person: their address is remembered and the topic comes from the request.
    tools.clear()
    res = _say("write an email to Mr Norton about the meeting")
    assert ("desktop.type_text", {"text": "jnorton@school.example.org"}) in tools
    assert "Here's the message" in res.text and "topic: the meeting" in drafts[-1]
    assert "recipient: Mr Norton" in drafts[-1]
    lesson_env["lessons"].stop()

    # Someone new: who, their address (once), what it's about, and what it has to say.
    tools.clear()
    assert _say("write an email").text == "Who's it to?"
    assert _say("Sam").text == "What's it about?"
    assert "Anything it has to say" in _say("the trip").text
    assert _say("tell him to bring snacks").text == "What's Sam's email address?"
    res = _say("sam@example.com")
    assert ("desktop.type_text", {"text": "sam@example.com"}) in tools
    assert "points: tell him to bring snacks" in drafts[-1]


def test_type_out_a_description_writes_it_then_types_it(lesson_env):
    tools, drafts = lesson_env["tools"], lesson_env["drafts"]
    res = _say("type out a summary of what SAINT is")
    assert res.ok and "typed it" in res.text
    assert "SAINT is the user's own AI desktop assistant" in lesson_env["systems"][-1]
    typed = [kw["text"] for n, kw in tools if n == "desktop.type_text"]
    assert typed and typed[-1] != "a summary of what SAINT is"

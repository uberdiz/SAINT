"""
modules/agent/output.py

Keeps internal representations out of what the user sees and hears.

Small local models often *write* a tool call as text instead of using the
structured ``tool_calls`` field — ``{"name": "screen__context", "parameters":
{}}`` — or wrap a plain answer in a pseudo call like ``{"name": "prompt",
"parameters": {"result": "The Burj Khalifa is 828 meters tall."}}``. Streaming
those tokens straight to the chat and TTS is how raw JSON reached the user.

* ``ReplyGuard`` sits between the model stream and the UI/TTS. It holds text
  back while it could still be a tool call / JSON / code block and only
  releases prose.
* ``extract_tool_calls`` turns textual tool calls into real ones so they get
  *executed* instead of shown.
* ``clean_reply`` is the last line of defence for any final reply.
"""

import json
import re
from typing import Callable, Dict, List, Optional, Tuple

# Text that starts like one of these is held until we know what it is.
_SUSPECT_START = re.compile(r"^\s*(?:\{|\[|```|`?[a-z_]+__[a-z_]+|<\|?(?:tool|function|python)|functions?\.|"
                            r"\b(?:tool_call|function_call)\b|[a-z_]+__[a-z_]+\s*\(|here'?s how you (?:could|can) "
                            r"ask|here(?:'s| is) (?:the |some |an? )?(?:example )?(?:python |json )?(?:code|function "
                            r"call|json|snippet|implementation)|(?:python|json)\s*\n)", re.I)
# "Here is the code that corresponds to the provided specification: python { "type": "function", ...}"
# (2026-09-28/29): the model narrating a tool call as code. Nothing in it is for the user.
_CODE_DUMP = re.compile(r"^\s*here(?:'s| is) (?:the |some |an? )?(?:example )?(?:python |json )?(?:code|function call|"
                        r"json|snippet|implementation)\b|\"type\"\s*:\s*\"function\"|\"parameters\"\s*:", re.I)
# A tool call written like code: desktop__click_element(name="top left video", action="click")
_CODE_CALL = re.compile(r"\b([a-z][a-z0-9_]*__[a-z0-9_]+)\s*\(([^()]*)\)", re.I)
_KWARG = re.compile(r"(\w+)\s*=\s*(\"[^\"]*\"|'[^']*'|-?\d+(?:\.\d+)?|true|false|none)", re.I)
_TOOLISH_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:__|\.)[a-z0-9_]+$")
_CODE_REQUEST = re.compile(r"\b(code|script|program|python|javascript|json|function|regex|snippet|"
                           r"class|html|css|sql|powershell|bash|command line)\b", re.I)


def _json_objects(text: str) -> List[Tuple[int, int, object]]:
    """All top-level JSON values embedded in ``text`` as (start, end, value)."""
    out, dec, i = [], json.JSONDecoder(), 0
    while i < len(text):
        j = min([k for k in (text.find("{", i), text.find("[", i)) if k >= 0], default=-1)
        if j < 0:
            break
        try:
            val, end = dec.raw_decode(text, j)
            out.append((j, end, val))
            i = end
        except ValueError:
            i = j + 1
    return out


def _as_call(val) -> Optional[Tuple[str, Dict]]:
    if not isinstance(val, dict):
        return None
    if "function" in val and isinstance(val["function"], dict):      # {"function": {"name", "arguments"}}
        val = val["function"]
    name = val.get("name") or val.get("tool") or val.get("tool_name")
    if not isinstance(name, str) or not name:
        return None
    args = val.get("parameters", val.get("arguments", val.get("args", {})))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"value": args}
    return (name.strip(), args if isinstance(args, dict) else {})


def _literal(v: str):
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low == "none":
        return None
    if v[:1] in "\"'":
        return v[1:-1]
    return float(v) if "." in v else int(v)


def extract_tool_calls(text: str) -> List[Tuple[str, Dict]]:
    """Tool calls written as text: JSON objects/lists with name + parameters,
    or code like ``spotify__play_query(query="jazz")``."""
    calls = []
    plain = _strip_fences(text)
    for _s, _e, val in _json_objects(plain):
        for item in (val if isinstance(val, list) else [val]):
            c = _as_call(item)
            if c:
                calls.append(c)
    if not calls:
        for m in _CODE_CALL.finditer(plain.replace("`", " ")):
            calls.append((m.group(1), {k: _literal(v) for k, v in _KWARG.findall(m.group(2))}))
    return calls


def pseudo_answer(name: str, args: Dict) -> Optional[str]:
    """``{"name": "prompt", "parameters": {"result": "..."}}`` is an answer, not a call."""
    if _TOOLISH_NAME.match(name or ""):
        return None
    strings = [v for v in (args or {}).values() if isinstance(v, str) and v.strip()]
    return strings[0].strip() if len(strings) == 1 else None


def _strip_fences(text: str) -> str:
    return re.sub(r"```[a-zA-Z]*\n?|```", "", text or "")


def looks_internal(text: str) -> bool:
    return bool(_SUSPECT_START.match(text or ""))


def clean_reply(text: str, user_text: str = "") -> str:
    """Remove tool-call JSON, internal tool names and (unrequested) code.

    Voice-first: even when the model rambles about "I'm going to inspect your
    screen and determine which window is currently active before...", we want
    the *result* to reach the user, not the meta-narration. Prompt hygiene is
    enforced upstream; this is the last line of defence."""
    t = (text or "").strip()
    if not t:
        return t
    wants_code = bool(_CODE_REQUEST.search(user_text or ""))
    if not wants_code and _CODE_DUMP.search(t) and not pseudo_answer_in(t):
        return ""
    # A reply that is only a pseudo call wrapping an answer -> the answer.
    objs = _json_objects(_strip_fences(t))
    if objs and not wants_code:
        stripped = _strip_fences(t)
        for s, e, val in reversed(objs):
            call = _as_call(val)
            if call is not None or isinstance(val, (dict, list)):
                ans = pseudo_answer(*call) if call else None
                stripped = stripped[:s] + (f" {ans} " if ans else " ") + stripped[e:]
        t = stripped
    if not wants_code:
        t = re.sub(r"```.*?```", " ", t, flags=re.S)                 # fenced code
        t = re.sub(r"<\|?/?(?:tool_call|function|python_tag|eom_id|eot_id)[^>]*\|?>", " ", t)
        # Chat-template scaffolding some local models leak: <|start|>…<|end|>, <think>…</think>
        t = re.sub(r"<\|[^|>]+\|>", " ", t)
        t = re.sub(r"<think>.*?</think>", " ", t, flags=re.S | re.I)
        t = re.sub(r"</?think>", " ", t, flags=re.I)
        # Any remaining `{...}` or `[...]` that isn't a real JSON object we can
        # unwrap is almost always a leaked tool schema fragment. Drop it.
        t = re.sub(r"\{[^{}]{0,400}?\}", " ", t)
        t = re.sub(r"^\s*\[[^\[\]]{0,400}?\]\s*", " ", t)
    # Internal tool identifiers ("screen__context", "desktop.open_app", "composite:...").
    if not wants_code:
        t = re.sub(r"^\s*here'?s how you (?:could|can) ask (?:that|it|me)[:.]?\s*", " ", t, flags=re.I)
        t = _CODE_CALL.sub(" ", t)
        t = re.sub(r"`+\s*`*", " ", t)
        if re.fullmatch(r"[\s,.;:]*(?:(?:followed by|then|and|or|after that)[\s,.;:]*)*", t, flags=re.I):
            t = ""                               # nothing but the glue between removed calls
    t = re.sub(r"\bcomposite[:.][\w.+:]+", " ", t)
    t = re.sub(r"\b(?:spotify|desktop|screen|memory|automation|system|browser|window|vision|voice)(?:__|\.)[a-z_]+\b",
               " ", t)
    # Talk about its own tools ("Based on the provided functions, it seems like you
    # want to open YouTube. However, there is no direct function to..." — 2026-09-28).
    t = re.sub(r"(?:^|(?<=[.!?])\s+)(?:based on (?:the )?(?:provided|available|given|listed) "
               r"(?:functions?|tools?|information)|(?:however,?\s+)?(?:there (?:is|are) no|i (?:don'?t|do not) "
               r"have (?:a|any)) (?:direct |specific |available )?(?:functions?|tools?))[^.!?]*[.!?]?", " ", t,
               flags=re.I)
    # Meta-narration voice-first users don't want to hear.
    t = re.sub(r"^\s*(?:sure|okay|ok|alright|got it),?\s+(?:i'?ll|i will|let me|i'?m going to|i am going to)\s+"
               r"[^.!?\n]{0,120}[.!?]\s*", "", t, flags=re.I)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s+([.,!?])", r"\1", t).strip()
    # What's left of a broken JSON call ('{"type":"function","name":" ","parameters }') is not speech.
    if not wants_code and (t[:1] in "{[" or re.search(r"\"\w+\"\s*:", t) or
                           (t and sum(ch in "{}[]\":" for ch in t) / len(t) > 0.12)):
        return ""
    return t


def pseudo_answer_in(text: str) -> bool:
    """A wrapped answer ({"name": "prompt", "parameters": {"result": "..."}}) is worth keeping."""
    for _s, _e, val in _json_objects(_strip_fences(text)):
        call = _as_call(val)
        if call and pseudo_answer(*call):
            return True
    return False


_ACTION_CLAIM = re.compile(
    r"^\W*(?:(?:ok(?:ay)?|sure|done|alright|got it)[,.!]?\s*)?(?:i(?:'ve| have| just)?\s+)?"
    r"(closed|opened|moved|played|paused|skipped|clicked|launched|started|stopped|turned|set|searched|typed|"
    r"minimi[sz]ed|maximi[sz]ed|deleted|sent|scrolled|switched|muted|resumed|queued|created|scheduled|"
    r"navigated|pressed|increased|decreased|raised|lowered|adjusted|hid|unmuted|enabled|disabled|"
    r"removed|added|restored|shuffled|renamed|copied|extracted|unzipped|downloaded|installed|uninstalled|"
    r"emptied|freed|recycled|trashed)\b|"
    r"^\W*(?:(?:ok(?:ay)?|sure|done|alright)[,.!]?\s*)?(?:i(?:'ve| have| just)?\s+)?made\s+(?:you\s+)?(?:a|an|the|your)\s+"
    r"(?:new\s+)?(?:folder|file|playlist|shortcut|automation|copy|backup|reminder)\b", re.I)


# "Playing your 'moe' playlist", "Disk Cleanup is open.", "Spotify volume increased."
_ACTION_CLAIM_MORE = re.compile(
    r"^\W*(?:(?:ok(?:ay)?|sure|done|alright|got it)[,.!]?\s*)?(?:i'?m\s+)?(?:now\s+)?"
    r"(?:playing|opening|closing|launching|starting|switching|skipping|pausing|muting|unmuting|turning|setting|"
    r"moving|clicking|typing|searching|minimi[sz]ing|maximi[sz]ing|stopping)\b|"
    r"^[\w'’ .-]{1,40}?\s+(?:is|are|was|were|has been|have been)\s+(?:now\s+)?(?:open(?:ed)?|closed|playing|paused|"
    r"muted|unmuted|on|off|launched|started|switched|minimi[sz]ed|maximi[sz]ed|skipped|banned|deleted|removed|"
    r"created|made|moved|renamed|saved|extracted|recycled|emptied|cleared|installed)\b|"
    r"^[\w'’ .-]{1,40}?\s+(?:volume\s+)?(?:increased|decreased|raised|lowered|switched|turned (?:up|down|on|off)|"
    r"muted|unmuted|opened|closed|launched)\b", re.I)


def unverified_action_claim(text: str) -> bool:
    """A reply that says an action happened. Only allowed when a tool ran."""
    t = (text or "").strip()
    if _ACTION_CLAIM.match(t):
        return True
    # "Nothing is playing" / "Spotify isn't open" describe a state, they don't claim an action.
    if re.search(r"\b(?:not|nothing|no|never|isn'?t|aren'?t|wasn'?t|can'?t|couldn'?t|won'?t)\b", t, re.I):
        return False
    return bool(_ACTION_CLAIM_MORE.match(t))


HONEST_NO_ACTION = "I haven't done anything yet — tell me what you'd like me to do."


def honest(text: str, tool_succeeded: bool) -> str:
    """Replace an action claim that no successful tool call backs up."""
    if not tool_succeeded and unverified_action_claim(text):
        return HONEST_NO_ACTION
    return text


# Once a reply is prose it streams sentence by sentence, and each sentence is
# still checked: "You can use the `automation__schedule_command` function.
# Here's an example: automation__schedule_command(command=...)" was spoken
# aloud on 2026-09-25 because everything after the first words passed through.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
_INTERNAL_BITS = re.compile(r"\b[a-z][a-z0-9]*__[a-z0-9_]+|\b[a-z_][a-z0-9_]*\s*\(\s*[a-z_]+\s*=", re.I)


class ReplyGuard:
    """Streams prose through; holds anything that might be internal.

    ``feed`` receives model tokens. While the reply so far could still be a
    tool call or code block nothing is emitted; once it is clearly prose it is
    released a sentence at a time, and a sentence that carries a tool name or
    a code-style call is dropped (unless the user asked for code). ``held`` is
    the text that was never shown.
    """

    DECIDE_CHARS = 24
    MAX_PENDING = 240              # release a long sentence without an end at a word boundary

    def __init__(self, emit: Callable[[str], None], user_text: str = ""):
        self._emit = emit
        self._buf = ""
        self._pending = ""
        self._wants_code = bool(_CODE_REQUEST.search(user_text or ""))
        self._user_text = user_text
        self._mode = "undecided"     # undecided | pass | hold

    def feed(self, tok: str):
        if self._mode == "pass":
            self._pending += tok
            self._flush()
            return
        self._buf += tok
        if self._mode == "hold":
            return
        head = self._buf.lstrip()
        if not head:
            return
        if looks_internal(head) or unverified_action_claim(head):
            self._mode = "hold"
        elif len(head) >= self.DECIDE_CHARS or len(head.split()) >= 5 or re.search(r"[.!?](\s|$)", head):
            # Decide only once a few words are in: "Okay, I opened..." must be
            # recognisable as a claim before anything is released.
            if unverified_action_claim(head):
                self._mode = "hold"
                return
            self._mode = "pass"
            self._pending, self._buf = self._buf, ""
            self._flush()

    def _flush(self, final: bool = False):
        while True:
            m = _SENTENCE_END.search(self._pending)
            if not m:
                break
            sentence, gap = self._pending[:m.start()], m.group(0)
            self._pending = self._pending[m.end():]
            self._release(sentence, gap)
        if len(self._pending) > self.MAX_PENDING and " " in self._pending:
            cut = self._pending.rfind(" ")
            sentence, self._pending = self._pending[:cut], self._pending[cut + 1:]
            self._release(sentence, " ")
        if final and self._pending.strip():
            sentence, self._pending = self._pending, ""
            self._release(sentence, "")

    def _release(self, sentence: str, gap: str):
        if self._wants_code:
            self._emit(sentence + gap)
            return
        if _INTERNAL_BITS.search(sentence):
            return                         # a tool name / call spelled out: never spoken
        out = clean_reply(sentence, self._user_text)
        if out:
            lead = sentence[:len(sentence) - len(sentence.lstrip())]
            self._emit(lead + out + gap)

    def finish(self) -> str:
        """End of a model message. Returns text still held back (not shown)."""
        if self._mode == "pass":
            self._flush(final=True)
        held, self._buf = self._buf, ""
        if self._mode == "undecided" and held.strip() and not looks_internal(held) \
                and not unverified_action_claim(held):
            self._emit(held)
            held = ""
        self._mode = "undecided"
        return held

    @property
    def holding(self) -> bool:
        return self._mode == "hold"

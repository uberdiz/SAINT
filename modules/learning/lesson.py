"""
modules/learning/lesson.py

Learning a long task by being walked through it, one step at a time.

SAINT doesn't guess at a task it has never done ("write an email" used to type
the words "an email"). It asks how, does each step as it's told, and keeps the
steps:

    you:   write an email
    SAINT: I haven't learned how to write an email yet. Walk me through it — where do I start?
    you:   open my browser and go to mail.google.com         (done now, kept as steps)
    you:   ask me which account to send from, personal or school
    SAINT: Okay, I'll ask that every time. Which account should I send from, personal or school?
    you:   school
    you:   click me@school.edu
    SAINT: Is "me@school.edu" the one for "school"?   (so the other answer works too)
    you:   click compose / ask me who it's to / type it in the to box / click the subject box
    you:   write a subject line about it                     (drafted by the model each time)
    you:   type the subject / ... / ask me before you send it / click send / done

Next time "write an email to Sam about the trip" runs the steps, asks only what
it doesn't already know (Sam and the trip come from the request), reads the
draft out for changes, and stops before "click send" until you say yes. A step
that stops working asks how to do it now and replaces only that step.

Steps are plain lines in an ordinary skill (data/skills.json), so they sync,
show on the Learned page and can be edited there:

    open my browser                            a command, run like anything said aloud
    ask: Which account, personal or school? -> account (school = x@y.edu; personal = x@gmail.com)
    ask once: What's {recipient}'s email? -> email     the answer is remembered for that question
    write: a short subject line -> subject     the only kind of step that uses the language model
    type {subject}                             answers and drafts fill {names}
    confirm: Should I send it?                 nothing after it runs without a yes
    you: hover over the profile picture        a step only the user can do; SAINT waits for "next"
"""

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.config import config
from core.paths import data_path
from modules.learning.skills import norm, skills

log = logging.getLogger("saint.learning")

IDLE_SEC = 600            # a lesson nobody has answered for 10 minutes is dropped


# ---------------------------------------------------------------------- #
# Steps
# ---------------------------------------------------------------------- #
_LINE = re.compile(r"^(ask once|ask|write|confirm|you)\s*:\s*(.+?)\s*$", re.I)
_ARROW = re.compile(r"^(.*?)\s*->\s*([a-z_][a-z0-9_]*)\s*(?:\((.*)\))?\s*$", re.I)
_VAR = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


@dataclass
class Step:
    kind: str                     # do | ask | write | confirm | you
    text: str                     # the command / question / what to write / what the user does
    var: str = ""                 # where the answer or draft goes
    once: bool = False            # ask: remember the answer to this exact (filled-in) question
    values: Dict[str, str] = field(default_factory=dict)   # ask: answer -> what to use for it

    @property
    def options(self) -> List[str]:
        """'Which account, personal or school?' -> ['personal', 'school']."""
        m = re.search(r"(?:^|[,:])\s*([^,:?]+?\s+or\s+[^,:?]+?)\s*\??$", self.text)
        if not m:
            return list(self.values)
        opts = [o.strip().lower() for o in re.split(r"\s*(?:,|\bor\b)\s*", m.group(1)) if o.strip()]
        return opts if all(len(o.split()) <= 3 for o in opts) else list(self.values)

    def line(self) -> str:
        if self.kind == "do":
            return self.text
        tail = f" -> {self.var}" if self.var else ""
        if self.values:
            tail += " (" + "; ".join(f"{k} = {v}" for k, v in self.values.items()) + ")"
        return f"{'ask once' if self.once else self.kind}: {self.text}{tail}"


def parse(line: str) -> Step:
    m = _LINE.match((line or "").strip())
    if not m:
        return Step("do", (line or "").strip())
    kind, rest = m.group(1).lower(), m.group(2)
    var, values = "", {}
    a = _ARROW.match(rest)
    if a:
        rest, var = a.group(1).strip(), a.group(2).lower()
        for pair in (a.group(3) or "").split(";"):
            k, _, v = pair.partition("=")
            if k.strip() and v.strip():
                values[k.strip().lower()] = v.strip()
    return Step("ask" if kind.startswith("ask") else kind, rest, var, once=kind == "ask once", values=values)


def is_lesson(steps: List[str]) -> bool:
    return any(parse(s).kind != "do" or _VAR.search(s) for s in steps or [])


def fill(text: str, values: Dict[str, str]) -> str:
    return _VAR.sub(lambda m: values.get(m.group(1).lower(), m.group(0)), text or "")


# ---------------------------------------------------------------------- #
# Understanding what the user says while teaching
# ---------------------------------------------------------------------- #
_LEAD = r"^(?:(?:ok(?:ay)?|so|and|then|now|next|after that|first|first of all|alright)[,\s]+)*" \
        r"(?:you (?:should|need to|have to|can|will) |i want you to |please )?"
_CANCEL = re.compile(r"^(?:cancel(?: (?:it|that|the lesson))?|never ?mind|forget (?:it|this|that|about it)|"
                     r"stop teaching|don'?t (?:save|learn) (?:it|this|that)|not now|no thanks|nah|no)$")
_UNDO = re.compile(r"^(?:undo(?: that| it| the last (?:step|one))?|scratch that|take that back|"
                   r"remove (?:that|the last) step|that step was wrong|that was wrong)$")
_CONFIRM = re.compile(
    _LEAD + r"(?:ask (?:me )?(?:first )?(?:before (?:you )?(?P<a>.+)|if i (?:really )?want (?:you )?to (?P<b>.+)|"
            r"whether (?:i want )?(?:you )?(?:to |should )?(?P<c>.+)|to confirm(?: (?:before )?(?P<d>.+))?)|"
            r"(?:check|confirm) with me(?: first)?(?: before (?:you )?(?P<e>.+))?|"
            r"make sure i want (?:you )?to (?P<f>.+)|"
            r"wait for my (?:ok|okay|go ahead|yes)(?: before (?:you )?(?P<g>.+))?)$")
_ASK = re.compile(_LEAD + r"ask (?:me )?(?P<q>.+)$")
_ONCE = re.compile(r",?\s+(?:if|unless|when) (?:you )?(?:don'?t|do not|didn'?t|haven'?t|never) "
                   r"(?:know|have|remember|already have|saved?)(?: (?:it|that|them))?(?: (?:yet|saved|already))?$|"
                   r",?\s+if (?:it'?s|it is|that'?s|they'?re) not (?:saved|known)(?: yet)?$|"
                   r",?\s+unless (?:you (?:already )?(?:know|have|remember) (?:it|that)|it'?s saved)$|"
                   r",?\s+(?:just )?the first time$")
_CONTENT = (r"title|subject(?: line)?|email|e-mail|message|body|text|reply|response|letter|post|tweet|caption|"
            r"description|summary|paragraph|note|content|contents|answer|comment|bio|intro|draft")
_WRITE = re.compile(_LEAD + r"(?:write|draft|compose|make|create|come up with|generate|think of|think up) "
                    r"(?:me |up )?(?:(?:a|an|the|my) )?(?P<what>(?:(?:short|quick|good|nice|polite|formal|casual|"
                    r"catchy|fitting|proper|simple|friendly)\s+)*(?P<noun>" + _CONTENT + r")\b.*)$")
_TYPE = re.compile(_LEAD + r"(?:type|put|paste|enter|fill in|write|add)(?: in| out)? "
                   r"(?:(?P<it>it|that|this)|(?P<det>the|my|their|his|her) (?P<what>[a-z][a-z' ]{1,30}?))"
                   r"(?: (?:in|into|on|to) (?:the )?(?P<where>.+?))?$")
_USER_DOES = re.compile(r"^(?:(?:i'?ll|i will|let me) (?:do|handle) (?:it|that|this)(?: (?:part|one|step|bit))?"
                        r"(?: myself)?(?: then)?|i did it(?: myself)?|(?:i )?did that|i (?:just )?did that one)$")
_SKIP = re.compile(r"^(?:skip|leave out|forget about)(?: (?:it|that|this)(?: (?:part|step|one))?)?$")
_NEXT = re.compile(r"^(?:next|ok(?:ay)?(?: next)?|go on|continue|keep going|i did it|done(?: that)?|it'?s done|"
                   r"all set|ready|go ahead|got it|finished|i'?m done|there|yes|yeah|yep)$")
_USE_IT = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|perfect|great|good|looks good|sounds good|that'?s good|"
                     r"that works|use it|use that|keep it|go with it|go with that|that'?s fine|fine|nice|"
                     r"(?:yes|ok(?:ay)?),? (?:use|keep) (?:it|that)|do it|go ahead|send it)$")
_EMAILISH = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|https?://\S+")
# "write mr norton's email (jnorton@essextech.net)" / "type jnorton@x.com in the to box": the recipient's
# address, typed — not an email to compose (2026-10-02).
_ADDRESS = re.compile(_LEAD + r"(?:write|type|put|enter|fill in|add|paste|use)(?: in| out| down)?\s+"
                      r"(?:(?P<who>[a-z][\w.' -]{0,40}?)(?:'s|s')\s+)?(?:(?:the |their |his |her )?(?:e-?mail|address|"
                      r"email address)\s*)?[(\[]?\s*(?P<addr>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)\s*[)\]]?"
                      r"(?:\s+(?:in|into|to|on) (?:the )?(?P<where>.+?))?$")
# "no, only write the email" / "no I meant click Send" / "not that, the subject box": replace the last step.
_CORRECTION = re.compile(r"^(?:no|nope|not that|wrong|that'?s wrong|that'?s not (?:it|right))[,.!]?\s+"
                         r"(?:i (?:meant|mean|said)\s+|instead\s+|rather\s+)?(?P<rest>\S.*)$")
_ONLY_ADDRESS = re.compile(r"^(?:only |just )?(?:write|type|put|enter|use|do)?\s*(?:in )?(?:the |their |his |her )?"
                           r"(?:e-?mail|address|email address)(?: address)?(?: only| part)?$")
# "type out a summary of what SAINT is": write it with the model, then type it.
_TYPE_DESCRIBED = re.compile(_LEAD + r"(?:type|put|enter|fill in|add)(?: in| out| up)?\s+(?P<what>.+?)"
                             r"(?:\s+(?:in|into|to) (?:the )?(?P<where>[\w ]{1,30}?\s*(?:box|field|bar|line|body|area)))?$")
_NOTHING = re.compile(r"^(?:no|nope|nah|nothing|none|no thanks|that'?s it|just that|nothing (?:else|special|in "
                      r"particular)|no,? (?:that'?s|it'?s) (?:it|fine)|skip(?: it)?)$")
_MESSAGE_WORDS = {"message", "email", "e-mail", "body", "text", "letter", "content", "contents", "draft",
                  "reply", "response", "it", "that", "this"}
_SUBJECT_WORDS = {"subject", "subject line", "title", "heading", "headline"}
_GERUND = {"sending": "send", "posting": "post", "submitting": "submit", "deleting": "delete", "buying": "buy",
           "paying": "pay", "ordering": "order", "saving": "save", "closing": "close", "clicking": "click",
           "uploading": "upload", "publishing": "publish", "replying": "reply", "booking": "book"}


_POLITE = re.compile(r"^(?:(?:hey\s+)?saint[,\s]+)?(?:(?:please|can you|could you|would you|will you|"
                     r"go ahead and)\s+)*", re.I)


def _tidy(text: str) -> str:
    """As said, minus "hey SAINT, please" and end punctuation. Case, URLs and
    addresses are kept ("go to https://mail.google.com/mail/u/0/#inbox")."""
    t = re.sub(r"\s+", " ", (text or "").strip().strip('"“”'))
    return _POLITE.sub("", t).strip(" .!?,")


def _clean(text: str) -> str:
    return _tidy(text).lower()


def _end(text: str) -> str:
    """A draft read out before a question needs its own full stop."""
    text = (text or "").rstrip()
    return text if text[-1:] in ".!?\"”')" else text + "."


def _plain(question: str) -> str:
    """A question with its {names} said in words, for SAINT's summary."""
    return _VAR.sub("it", re.sub(r"\{\w+\}'s", "their", question))


def _sentence(q: str) -> str:
    q = q.strip(" ?.")
    return (q[:1].upper() + q[1:] + "?") if q else ""


def direct_question(indirect: str, owner: str = "") -> str:
    """'which account to send from, personal or school' -> 'Which account should I send from, personal or school?'
    ``owner``: the earlier answer "their" / "his" / "her" refers to ("{recipient}")."""
    q = indirect.strip(" ?.")
    m = re.match(r"^(which|what|who|where|when|how)\s+(.*?)\bto\s+([a-z]+)\b(.*)$", q, re.I)
    if m and m.group(3).lower() not in ("me", "you", "it", "them", "him", "her"):
        return _sentence(f"{m.group(1)} {m.group(2)}should I {m.group(3)}{m.group(4)}")
    m = re.match(r"^(who|what|where)\s+(?:it|this|that)(?:'s| is)\s+(.+)$", q, re.I)
    if m:
        return _sentence(f"{m.group(1)}'s it {m.group(2)}")
    m = re.match(r"^(?:for|about)\s+(?:the|an?|their|his|her|your|my)\s+(.+)$", q, re.I)
    if m:
        whose = re.match(r"^(?:for|about)\s+(their|his|her)\b", q, re.I)
        thing = m.group(1)
        if whose and owner:
            return _sentence(f"what's {owner}'s {thing}")
        return _sentence(f"what's the {thing}")
    return _sentence(q)


def _var_name(question: str, used) -> str:
    q = question.lower()
    if re.match(r"^who\b|.*\b(?:to whom|recipient|send (?:it|this) to)\b", q):
        base = "recipient"
    elif re.search(r"\babout\b|\btopic\b|what should (?:it|i|the \w+) say|what (?:should i|to) write", q):
        base = "topic"
    elif re.search(r"\bwhich (?:email|address|account|profile)\b.*\bfrom\b", q):
        base = "account"
    else:
        m = re.search(r"\bwhich (\w+)", q) or re.search(r"what'?s (?:the |\{\w+\}'s )?([a-z]+(?: [a-z]+)?)\??$", q)
        base = re.sub(r"\W+", "_", m.group(1)).strip("_") if m else "answer"
        if re.search(r"\{(\w+)\}'s", q):
            base = re.search(r"\{(\w+)\}'s", q).group(1) + "_" + base
    name, n = base, 2
    while name in used:
        name, n = f"{base}{n}", n + 1
    return name


def _write_var(noun: str, used) -> str:
    noun = noun.lower()
    base = "subject" if noun.startswith(("subject", "title")) else \
        "message" if noun in _MESSAGE_WORDS else re.sub(r"\W+", "_", noun)
    name, n = base, 2
    while name in used:
        name, n = f"{base}{n}", n + 1
    return name


def _is_who(step: Step) -> bool:
    # "What's {recipient}'s email address?" mentions the recipient but asks for an address, not who.
    return step.var == "recipient" or bool(re.search(r"^who\b|\bto whom\b|\brecipient\b", _VAR.sub("", step.text),
                                                     re.I))


def _is_about(step: Step) -> bool:
    return step.var == "topic" or bool(re.search(r"\babout\b|\btopic\b|what should (?:it|i) (?:say|write)",
                                                 _VAR.sub("", step.text), re.I))


# "write an email" / "compose a message" / "email Sam": one task however it's said.
def canon(text: str) -> str:
    t = re.sub(r"\be-mail\b", "email", norm(text))
    t = re.sub(r"^(?:email|mail)\s+(?!(?:a|an|the)\b)", "send an email to ", t)
    t = re.sub(r"^(?:write|compose|draft|send|make|create|start|type up|do)\s+(?:me\s+|up\s+)?"
               r"(?:a\s+|an\s+|the\s+|another\s+)?(?:new\s+)?(email|message|text|reply|letter)\b", r"send a \1", t)
    return t


_MESSAGE_TASK = re.compile(r"^send a (?:email|message|text|reply|letter)\b")


def split_request(text: str):
    """'write an email to Sam about the trip' -> ('write an email', {'to': 'Sam', 'about': 'the trip'})."""
    said = norm(text)
    if not _MESSAGE_TASK.match(canon(said)):
        return said, {}
    m = re.match(r"^(.+?)\s+((?:to|about|saying|that says|regarding|telling)\b.*)$", _tidy(text), re.I)
    if not m:
        return said, {}
    return norm(m.group(1)), _details(m.group(2))


def _details(rest: str) -> Dict[str, str]:
    """'... to Sam about the trip' -> {'to': 'Sam', 'about': 'the trip'} (as said, case kept)."""
    out = {}
    m = re.search(r"\bto\s+(.+?)(?=\s+(?:about|saying|that says|regarding|telling (?:them|him|her))\b|$)", rest, re.I)
    if m:
        out["to"] = m.group(1).strip()
    m = re.search(r"\b(?:about|regarding|saying|that says|telling (?:them|him|her))\s+(.+)$", rest, re.I)
    if m:
        out["about"] = m.group(1).strip()
    return out


_TASK = re.compile(r"^(?:send|write|compose|draft|reply to|respond to|forward|post|tweet|upload|submit|order|"
                   r"book|buy|purchase|pay|sign up|register|apply|fill (?:out|in)|email|e-mail|renew|"
                   r"check in|log ?in to|sign in to|unsubscribe)\b\s*\S", re.I)


def is_task(text: str) -> bool:
    """A job with several steps SAINT should be taught rather than guess at."""
    t = _clean(text)
    return bool(t) and len(t.split()) <= 25 and bool(_TASK.match(t))


_START = re.compile(r"^(?:(?:i want to|i'?d like to|let me|i'?ll|i will|i'?m (?:going|gonna) to|can i|could i)\s+)?"
                    r"(?:teach you|walk you through|show you step by step)(?: how)?(?: to)? (?P<task>.+)$|"
                    r"^(?:(?:can|could|will) you\s+)?learn (?:how )?to (?P<task2>.+)$")


def start_phrase(text: str) -> Optional[str]:
    """'let me teach you how to send an email' -> 'send an email'."""
    m = _START.match(_clean(text))
    if m and not m.group("task"):
        task = m.group("task2").strip()
        return None if task in ("it", "this", "that") else task
    if not m or m.group("task").strip() in ("it", "this", "that", "how"):
        return None
    return m.group("task").strip()


def retire_guessed_email_skills():
    """Once: forget email / message skills learned the old way (a guess, or "write
    an email" typing the words), so the next "write an email" asks to be walked
    through it (asked for on 2026-10-02). Lessons are kept."""
    if config.get("learning.email_skills_retired", False):
        return
    gone = []
    for s in skills.all():
        if _MESSAGE_TASK.match(canon(s.phrase)) and not is_lesson(s.steps):
            skills.forget(s.id)
            gone.append(s.phrase)
    if gone:
        log.info("lesson.retired_email_skills %r", gone)
    config.set("learning.email_skills_retired", True)


# ---------------------------------------------------------------------- #
# Tidying a lesson before it's saved
# ---------------------------------------------------------------------- #
def ensure_message_asks(steps: List[Step]) -> None:
    """An email / message lesson asks who it's to, what it's about and what it has to say — unless the
    request already said ("write an email to Sam about the trip") — so the drafts are about *this* email."""
    if not any(s.kind == "write" for s in steps):
        return
    used = {s.var for s in steps if s.var}
    at = 0
    if not any(s.kind == "ask" and _is_who(s) for s in steps):
        steps.insert(at, Step("ask", "Who's it to?", "recipient" if "recipient" not in used else "recipient2"))
    at = next((i for i, s in enumerate(steps) if s.kind == "ask" and _is_who(s)), -1) + 1
    if not any(s.kind == "ask" and _is_about(s) for s in steps):
        steps.insert(at, Step("ask", "What's it about?", "topic" if "topic" not in used else "topic2"))
    at = next((i for i, s in enumerate(steps) if s.kind == "ask" and _is_about(s)), at) + 1
    if not any(s.var.startswith("points") for s in steps):
        steps.insert(at, Step("ask", "Anything it has to say? (or “no”)", "points"))


def compact(steps: List[Step]) -> List[Step]:
    """Drop what doesn't need repeating: the same step twice in a row, and switching to an app the
    step before just opened."""
    out: List[Step] = []
    for s in steps:
        if out and s.kind == out[-1].kind and s.line().lower() == out[-1].line().lower():
            continue
        if out and s.kind == "do" and out[-1].kind == "do" and s.text.lower().startswith("switch to ") and \
                s.text.lower()[10:].strip() in out[-1].text.lower() and out[-1].text.lower().startswith("open "):
            continue
        out.append(s)
    return out


def describe(steps: List[Step]) -> str:
    """The steps in plain words, for reading a lesson back."""
    words = []
    for s in steps:
        if s.kind == "ask":
            continue                                  # listed separately ("Each time I'll ask: ...")
        if s.kind == "write":
            words.append("write " + _VAR.sub(lambda m: "what it's " + ("about" if m.group(1).startswith("topic")
                                                                      else m.group(1)), s.text))
        elif s.kind == "confirm":
            words.append("check with you")
        elif s.kind == "you":
            words.append("wait while you " + s.text)
        else:
            t = re.sub(r"\{(\w*email\w*)\}", "their address", s.text)
            t = re.sub(r"\{(subject\w*)\}", "the subject", t)
            t = re.sub(r"\{(message\w*)\}", "the message", t)
            words.append(_VAR.sub(lambda m: "the " + m.group(1).replace("_", " "), t))
    return ", then ".join(words) if words else "nothing to do on screen"


# ---------------------------------------------------------------------- #
# Answers kept for "ask once" questions
# ---------------------------------------------------------------------- #
class Answers:
    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return self._path or str(data_path("lesson_answers.json"))

    def _load(self) -> Dict[str, str]:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, question: str) -> str:
        with self._lock:
            return self._load().get(_clean(question), "")

    def put(self, question: str, answer: str):
        with self._lock:
            d = self._load()
            d[_clean(question)] = answer
            self._write(d)

    def all(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._load())

    def remove(self, question: str):
        with self._lock:
            d = self._load()
            if d.pop(_clean(question), None) is not None:
                self._write(d)

    def _write(self, d: Dict[str, str]):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.path)


answers = Answers()


# ---------------------------------------------------------------------- #
# The lesson itself
# ---------------------------------------------------------------------- #
@dataclass
class _Teaching:
    task: str
    details: Dict[str, str]
    steps: List[Step] = field(default_factory=list)
    vals: Dict[str, str] = field(default_factory=dict)      # var -> what it is this time
    said: Dict[str, str] = field(default_factory=dict)      # var -> the answer as said ("school")
    waiting: tuple = ()                                     # ("answer", var) | ("map", ...) | ("mapval", ...)
    last_failed: str = ""
    touched: float = field(default_factory=time.time)


@dataclass
class _Run:
    skill: object
    steps: List[Step]
    vals: Dict[str, str]
    tok: object
    i: int = 0
    waiting: str = ""            # ask | write | confirm | you | repair | step
    last: str = ""               # the last thing a step said
    touched: float = field(default_factory=time.time)


class Reply:
    """The router's Reply shape (kept local so this module imports nothing heavy)."""

    def __init__(self, text: str, ok: bool = True, expects_reply: bool = False):
        self.text, self.ok, self.expects_reply = text, ok, expects_reply


class LessonManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._teach: Optional[_Teaching] = None
        self._run: Optional[_Run] = None

    # ------------------------------------------------------------------ #
    @property
    def teaching(self) -> bool:
        with self._lock:
            return self._teach is not None and time.time() - self._teach.touched < IDLE_SEC

    @property
    def running(self) -> bool:
        with self._lock:
            r = self._run
            if r is not None and (r.tok.cancelled or time.time() - r.touched > IDLE_SEC):
                self._run = None
                return False
            return r is not None

    @property
    def active(self) -> bool:
        return self.teaching or self.running

    def stop(self) -> bool:
        with self._lock:
            was = self._teach is not None or self._run is not None
            self._teach = self._run = None
        if was:
            log.info("lesson.stopped")
        return was

    # ------------------------------------------------------------------ #
    # Starting
    # ------------------------------------------------------------------ #
    @staticmethod
    def start_phrase(text: str) -> Optional[str]:
        return start_phrase(text)

    @staticmethod
    def should_offer(text: str) -> bool:
        """Ask to be walked through ``text`` rather than guess at it?"""
        return bool(config.get("learning.guided_lessons", True)) and is_task(text)

    def offer(self, text: str) -> Reply:
        """SAINT was asked for a task it has never done: ask to be walked through it."""
        task, details = split_request(text)
        with self._lock:
            self._run = None
            self._teach = _Teaching(task, details)
        log.info("lesson.offer task=%r details=%r", task, details)
        return Reply(f"I haven't learned how to {task} yet, and I'd rather not guess. Walk me through it one "
                     f"step at a time and I'll do each one — where do I start? (Say “done” at the end, or "
                     f"“cancel”.)", ok=False, expects_reply=True)

    def start(self, task: str) -> Reply:
        task, details = split_request(task)
        with self._lock:
            self._run = None
            self._teach = _Teaching(task, details)
        log.info("lesson.start task=%r", task)
        return Reply(f"Okay, teach me how to {task}. Tell me one step at a time and I'll do each one as we go. "
                     f"Where do I start?", expects_reply=True)

    def match(self, text: str):
        """A learned lesson for ``text`` ("write an email to Sam" -> the "write an email" lesson)."""
        said = canon(text)
        if not said:
            return None
        retire_guessed_email_skills()
        best = None
        for s in skills.all():
            if not is_lesson(s.steps):
                continue
            mine = canon(s.phrase)
            if said == mine or (said.startswith(mine + " ") and _MESSAGE_TASK.match(mine)):
                if best is None or len(mine) > len(canon(best.phrase)):
                    best = s
        return best

    # ------------------------------------------------------------------ #
    # Talking during a lesson
    # ------------------------------------------------------------------ #
    def feed(self, text: str) -> Optional[Reply]:
        """The user's next utterance while a lesson is open. None: not for the
        lesson (a question, say) — the agent handles it as usual."""
        if self.running:
            return self._feed_run(text)
        if self.teaching:
            return self._feed_teach(text)
        return None

    def resume(self, said: str) -> Optional[Reply]:
        """A step asked its own question ("Which window?") and it was answered
        through the normal confirmation path: carry on from there."""
        if self.running and self._run.waiting == "step":
            self._run.waiting = ""
            return self._advance(lead=said)
        if self.teaching:
            self._teach.touched = time.time()
            return Reply(f"{said.rstrip()} What's next?", expects_reply=True)
        return None

    # ---- teaching -------------------------------------------------------- #
    def _feed_teach(self, text: str) -> Optional[Reply]:
        from modules.agent.confirm import classify_reply
        from modules.learning import intents as learning
        from modules.learning.planner import _CHATTY
        t = self._teach
        t.touched = time.time()
        said = _clean(text)
        if t.waiting:
            return self._answer_teach(text, said)
        if _CANCEL.match(said) and (not t.steps or said not in ("no", "nah", "not now", "no thanks")):
            self.stop()
            return Reply("Okay, forgotten — I didn't save anything." if t.steps else "Okay.")
        if _SKIP.match(said) and t.last_failed:
            t.last_failed = ""
            return Reply("Okay, I'll leave that out. What's next?", expects_reply=True)
        if _USER_DOES.match(said) and t.last_failed:
            t.steps.append(Step("you", self._generalize(t.last_failed)))
            t.last_failed = ""
            return Reply("Okay, that part's yours — each time I'll wait for you to do it and say “next”. "
                         "What's next?", expects_reply=True)
        if learning.is_done(text) or re.match(r"^(?:that'?s (?:everything|the whole thing)|save it|we'?re done)$",
                                               said):
            return self._save()
        if _UNDO.match(said):
            if not t.steps:
                return Reply("There's nothing to undo yet. Where do I start?", expects_reply=True)
            gone = t.steps.pop()
            return Reply(f"Okay, I took out “{gone.line() if gone.kind != 'do' else gone.text}”. What's next?",
                         expects_reply=True)
        if re.match(r"^(?:next|what'?s next|ok(?:ay)?|go on)$", said):
            return Reply("Tell me the next step, or say “done” if that's everything.", expects_reply=True)
        if not t.steps and classify_reply(said) is True:
            return Reply("Great — what's the first step? Where do I go for that?", expects_reply=True)

        m = _CORRECTION.match(said)
        if m and t.steps and not _CANCEL.match(m.group("rest")):
            return self._correct(text, m.group("rest"))
        m = _ADDRESS.match(said)
        if m:
            return self._teach_address(m.group("who") or "", m.group("addr"), m.group("where") or "")
        m = _TYPE_DESCRIBED.match(said)
        if m and not _TYPE.match(said):
            from modules.agent.compose import describes_text
            if describes_text(m.group("what")):
                what = _tidy(text)[m.start("what"):m.end("what")]          # the user's own capitals
                return self._teach_type_described(what or m.group("what"), m.group("where") or "")

        m = _CONFIRM.match(said)
        if m:
            act = next((g for g in m.groups() if g), "") or ""
            act = re.sub(r"^(\w+ing)\b", lambda g: _GERUND.get(g.group(1), g.group(1)), act.strip())
            act = re.sub(r"^(?:you |i )", "", act)
            q = f"Should I {act}?" if act else "Should I go ahead?"
            t.steps.append(Step("confirm", q))
            return Reply(f"Okay — I'll stop and ask “{q}” there. What's next?", expects_reply=True)
        m = _ASK.match(said)
        if m:
            return self._teach_ask(m.group("q"))
        m = _WRITE.match(said)
        if m:
            what = m.group("what")
            return self._teach_write(("an " if what[:1] in "aeiou" else "a ") + what, m.group("noun"))
        m = _TYPE.match(said)
        var = self._var_for(m) if m else ""
        if var:
            where = m.group("where") or ""
            line = f"type {{{var}}}" + (f" into the {where}" if where else "")
            t.steps.append(Step("do", line))
            value = t.vals.get(var, "")
            if not value:
                return Reply("Okay. What's next?", expects_reply=True)
            r = self._type(value, where)
            if not r.ok:
                t.steps.pop()
                t.last_failed = line
                return Reply(f"{r.text} Click where it should go and tell me again, or say “I'll do it”.",
                             ok=False, expects_reply=True)
            return Reply(f"{r.text} What's next?", expects_reply=True)
        m = re.match(r"^(?:for |when i say |if i say )?(?P<a>[\w'.@ -]{1,30}?),? (?:means|is|use|=) (?P<b>\S.{0,120})$",
                     said)
        if m:
            ask = next((s for s in reversed(t.steps) if s.kind == "ask" and m.group("a") in s.options), None)
            if ask is not None:
                ask.values[m.group("a")] = m.group("b")
                return Reply(f"Got it — “{m.group('a')}” means {m.group('b')}. What's next?", expects_reply=True)
        if _CHATTY.match(said) and not said.startswith(("go ", "do ")):
            return None                          # a question in passing: answered as usual, lesson stays open

        # A command: do it now, keep it if it works.
        reply, steps = self._do_new(text)
        if reply is None:
            t.last_failed = said
            return Reply(f"I don't know how to “{said}” yet. Say it another way — like “click Compose” or “go "
                         f"to mail.google.com” — or do it yourself and say “I did it”.", ok=False, expects_reply=True)
        if not reply.ok and not reply.expects_reply:
            t.last_failed = said
            return Reply(f"{reply.text.rstrip()} Tell me another way to do that, or do it yourself and say "
                         f"“I did it”.", ok=False, expects_reply=True)
        t.last_failed = ""
        t.steps.extend(Step("do", self._generalize(s)) for s in steps)
        if reply.expects_reply:
            return Reply(reply.text, ok=True, expects_reply=True)     # the step's own question; resume() follows
        follow = self._maybe_ask_mapping(steps)
        if follow:
            return Reply(f"{reply.text.rstrip()} {follow}", expects_reply=True)
        return Reply(f"{reply.text.rstrip()} What's next?", expects_reply=True)

    # ---- corrections, addresses and written text while teaching ---------------- #
    def _correct(self, text: str, rest: str) -> Reply:
        """'no, only write the email': the last step was wrong — take it out and do this instead."""
        t = self._teach
        gone = t.steps.pop()
        gone_text = gone.text if gone.kind == "do" else gone.line()
        email_var = next((s.var for s in t.steps if s.kind == "ask" and s.var.endswith("email")), "")
        if _ONLY_ADDRESS.match(rest) and email_var:
            line = "type {" + email_var + "}"
            if gone.kind == "do" and gone.text.startswith(line):
                t.steps.append(gone)                    # that already was just the address
                return Reply("Got it — just the address. What's next?", expects_reply=True)
            t.steps.append(Step("do", line))
            r = self._type(t.vals.get(email_var, ""))
            return Reply(f"Okay — only the address, not “{gone_text}”. {r.text} What's next?", ok=r.ok,
                         expects_reply=True)
        tidy = _tidy(text)
        replacement = tidy[len(tidy) - len(rest):] if len(rest) <= len(tidy) else rest
        replacement = re.sub(r"^(?:only|just)\s+", "", replacement, flags=re.I)
        reply = self._feed_teach(replacement)
        lead = f"Okay, I took out “{gone_text}”."
        if reply is None:
            return Reply(f"{lead} What should I do instead?", expects_reply=True)
        return Reply(f"{lead} {reply.text}", ok=reply.ok, expects_reply=reply.expects_reply)

    def _teach_address(self, who: str, addr: str, where: str) -> Reply:
        """Type the recipient's address, and next time ask who it's to (and their address, once per person)."""
        t = self._teach
        rec = next((s for s in t.steps if s.kind == "ask" and _is_who(s)), None)
        if rec is None:
            used = {s.var for s in t.steps if s.var}
            rec = Step("ask", "Who's it to?", "recipient" if "recipient" not in used else _var_name("Who's it to?", used))
            t.steps.insert(0, rec)
        who = " ".join(w[:1].upper() + w[1:] for w in who.split()) if who and who == who.lower() else who
        if who and not t.vals.get(rec.var):
            t.vals[rec.var] = t.said[rec.var] = who
        elif not t.vals.get(rec.var) and t.details.get("to"):
            t.vals[rec.var] = t.said[rec.var] = t.details["to"]
        em = next((s for s in t.steps if s.kind == "ask" and s.var.endswith("email")), None)
        if em is None:
            em = Step("ask", "What's {" + rec.var + "}'s email address?", "email", once=True)
            t.steps.append(em)
        t.vals[em.var] = t.said[em.var] = addr
        if t.vals.get(rec.var):
            answers.put(fill(em.text, t.vals), addr)        # Mr Norton's address is known from now on
        line = "type {" + em.var + "}" + (f" into the {where}" if where else "")
        t.steps.append(Step("do", line))
        r = self._type(addr, where)
        if not r.ok:
            t.steps.pop()
            t.last_failed = line
            return Reply(f"{r.text} Click the To box and tell me again, or say “I'll do it”.", ok=False,
                         expects_reply=True)
        whose = f"{t.vals[rec.var]}'s" if t.vals.get(rec.var) else "their"
        return Reply(f"{r.text.rstrip('.')} — {whose} address. Next time I'll ask who it's to and fill in their "
                     f"address. What's next?", expects_reply=True)

    def _teach_type_described(self, what: str, where: str) -> Reply:
        """'type out a summary of what SAINT is': a write step (the model drafts it each time) and a type step.
        In an email lesson, what it's about becomes the question “What's it about?”."""
        t = self._teach
        used = {s.var for s in t.steps if s.var}
        message = bool(_MESSAGE_TASK.match(canon(t.task)))
        subject = bool(re.search(r"\bsubject|title\b", where or ""))
        if message:
            about = next((s for s in t.steps if s.kind == "ask" and _is_about(s)), None)
            if about is None:
                about = Step("ask", "What's it about?", "topic" if "topic" not in used else _var_name("about", used))
                t.steps.insert(1 if t.steps and t.steps[0].kind == "ask" else 0, about)
            if not t.vals.get(about.var):
                t.vals[about.var] = t.said[about.var] = t.details.get("about") or what
            noun = (re.search(r"\b(email|message|text|reply|letter)\b", canon(t.task)) or [None, "message"])[1]
            desc = f"a subject line for the {noun} about {{{about.var}}}" if subject else \
                f"the {noun}'s text about {{{about.var}}}"
            var = _write_var("subject" if subject else "message", used)
        else:
            desc = self._generalize(what)
            m = re.search(r"\b(" + _CONTENT + r")\b", what, re.I)
            var = _write_var(m.group(1) if m else "text", used)
        step = Step("write", desc, var)
        t.steps.append(step)
        draft = self._draft(step, t.vals)
        if not draft:
            t.steps.pop()
            return Reply(f"I couldn't write {what} — the language model didn't answer. Try again in a moment, or "
                         f"type it yourself and say “I did it”.", ok=False, expects_reply=True)
        t.vals[var] = draft
        line = "type {" + var + "}" + (f" into the {where}" if where else "")
        t.steps.append(Step("do", line))
        r = self._type(draft, where)
        if not r.ok:
            t.steps.pop()
            t.last_failed = line
            return Reply(f"I wrote it, but {r.text[:1].lower() + r.text[1:]} Click where it should go and say “type "
                         f"it”, or say “I'll do it”.", ok=False, expects_reply=True)
        return Reply(f"Wrote {what} and typed it. Each time I'll write a fresh one. What's next?", expects_reply=True)

    def _teach_ask(self, indirect: str) -> Reply:
        t = self._teach
        once = bool(_ONCE.search(indirect))
        indirect = _ONCE.sub("", indirect).strip()
        last = next((s.var for s in reversed(t.steps) if s.kind == "ask"), "")
        q = direct_question(indirect, owner="{" + last + "}" if last else "")
        used = {s.var for s in t.steps if s.var}
        step = Step("ask", q, _var_name(q, used), once=once)
        t.steps.append(step)
        how = "the first time and remember the answer" if once else "every time"
        value = self._known_answer(step, t.details, t.vals)
        if value:
            t.vals[step.var] = t.said[step.var] = value
            return Reply(f"Okay, I'll ask that {how}. This time it's “{value}”. What's next?", expects_reply=True)
        t.waiting = ("answer", step.var)
        return Reply(f"Okay, I'll ask that {how}. So — {_plain(fill(q, t.vals))}", expects_reply=True)

    def _teach_write(self, what: str, noun: str) -> Reply:
        t = self._teach
        used = {s.var for s in t.steps if s.var}
        step = Step("write", self._generalize(what), _write_var(noun, used))
        t.steps.append(step)
        draft = self._draft(step, t.vals)
        if not draft:
            return Reply(f"Okay, I'll write {what} there each time — I couldn't reach the language model just "
                         f"now, so there's nothing to show yet. What's next?", expects_reply=True)
        t.vals[step.var] = draft
        return Reply(f"Okay, I'll write that each time. This time: {_end(draft)} What's next?", expects_reply=True)

    def _answer_teach(self, text: str, said: str) -> Reply:
        from modules.agent.confirm import classify_reply
        t = self._teach
        kind = t.waiting[0]
        if kind == "answer":
            var = t.waiting[1]
            t.waiting = ()
            if _CANCEL.match(said) and said not in ("no", "nah"):
                self.stop()
                return Reply("Okay, forgotten — I didn't save anything.")
            answer = _tidy(text)
            step = next(s for s in t.steps if s.var == var)
            t.said[var] = answer
            t.vals[var] = self._value_for(step, answer)
            if step.once:
                answers.put(fill(step.text, t.vals), t.vals[var])
            return Reply(f"Got it — {answer}. What's next?", expects_reply=True)
        if kind == "map":                         # ("map", var, literal)
            _, var, literal = t.waiting
            ask = next(s for s in t.steps if s.var == var)
            if classify_reply(said) is not True:
                t.waiting = ()
                return Reply("Okay, I'll always use that one. What's next?", expects_reply=True)
            ask.values[t.said[var].lower()] = literal
            t.vals[var] = literal
            return self._next_mapping(var, literal)
        if kind == "mapval":                      # ("mapval", var, option, literal)
            _, var, option, literal = t.waiting
            ask = next(s for s in t.steps if s.var == var)
            ask.values[option] = _tidy(text)
            return self._next_mapping(var, literal)
        t.waiting = ()
        return Reply("What's next?", expects_reply=True)

    def _maybe_ask_mapping(self, done_steps: List[str]) -> str:
        """After 'school' was the answer, "click me@school.edu" probably
        *is* school. Ask, so the other answer can pick its own address next time."""
        t = self._teach
        ask = next((s for s in reversed(t.steps) if s.kind == "ask"), None)
        if ask is None or not ask.options or ask.values or ask.var not in t.said:
            return ""
        said = re.compile(rf"(?<![\w@.]){re.escape(t.said[ask.var])}(?![\w@])", re.I)
        for s in done_steps:
            m = _EMAILISH.search(s)
            if m and not said.search(s):             # "school" inside "me@school.edu" isn't the answer
                t.waiting = ("map", ask.var, m.group(0))
                return f"Is “{m.group(0)}” the one for “{t.said[ask.var]}”?"
        return ""

    def _next_mapping(self, var: str, literal: str) -> Reply:
        t = self._teach
        ask = next(s for s in t.steps if s.var == var)
        left = [o for o in ask.options if o not in ask.values]
        if left:
            t.waiting = ("mapval", var, left[0], literal)
            return Reply(f"And what should I use for “{left[0]}”?", expects_reply=True)
        t.waiting = ()
        for st in t.steps:
            if st.kind == "do" and literal.lower() in st.text.lower():
                st.text = re.sub(re.escape(literal), "{" + var + "}", st.text, flags=re.I)
        return Reply("Got it — I'll pick the right one from your answer. What's next?", expects_reply=True)

    def _save(self) -> Reply:
        t = self._teach
        self.stop()
        if not t.steps:
            return Reply("Okay — we didn't get to any steps, so there's nothing to save.")
        if t.steps[-1].kind == "confirm":
            t.steps.pop()                         # a question with nothing after it guards nothing
        if _MESSAGE_TASK.match(canon(t.task)):
            ensure_message_asks(t.steps)
        t.steps = compact(t.steps)
        lines = [s.line() for s in t.steps]
        sk = skills.learn(t.task, lines, "lesson")
        if sk is None:
            return Reply(f"I couldn't save that under “{t.task}” — try a more specific name.", ok=False)
        log.info("lesson.saved task=%r steps=%r", t.task, lines)
        asks = [_plain(s.text) for s in t.steps if s.kind == "ask" and not s.once]
        extra = (" Each time I'll ask: " + " / ".join(asks)) if asks else ""
        if any(s.kind == "confirm" for s in t.steps):
            extra += (" — and" if asks else " I'll") + " check with you before the last step."
        return Reply(f"Saved “{t.task}”: {describe(t.steps)}.{extra} Next time just say “{t.task}” — add who it's "
                     f"for or what it's about and I won't ask — and if a step stops working I'll ask how to do it "
                     f"then." if _MESSAGE_TASK.match(canon(t.task)) else
                     f"Saved “{t.task}”: {describe(t.steps)}.{extra} Next time just say “{t.task}”; if a step stops "
                     f"working I'll ask how to do it then.")

    # ---- running a learned lesson ---------------------------------------- #
    def run(self, skill, text: str = "") -> Reply:
        from core.cancel import cancel
        steps = [parse(s) for s in skill.steps]
        details = _details(_tidy(text)) if text and _MESSAGE_TASK.match(canon(text)) else {}
        vals: Dict[str, str] = {}
        for s in steps:
            v = self._known_answer(s, details, vals) if s.kind == "ask" else ""
            if v:
                vals[s.var] = v
            elif s.kind == "ask" and s.var.startswith("points") and details.get("about"):
                vals[s.var] = ""              # "... about lunch tomorrow" said enough: don't ask what it must say
        with self._lock:
            self._teach = None
            self._run = _Run(skill, steps, vals, cancel.token())
        log.info("lesson.run %r prefilled=%r", skill.phrase, list(vals))
        return self._advance()

    def _advance(self, lead: str = "") -> Reply:
        from core.activity import activity
        r = self._run
        if r is None:
            return Reply(lead or "Okay.")
        r.touched = time.time()
        lead = lead.strip()
        label = r.skill.phrase
        activity.begin(label, [self._label(s) for s in r.steps])
        try:
            while r.i < len(r.steps):
                if r.tok.cancelled:
                    self.stop()
                    return Reply(" ".join(x for x in (lead, "Stopped.") if x), ok=False)
                st = r.steps[r.i]
                activity.step(r.i)
                if st.kind == "ask":
                    if st.var in r.vals:
                        r.i += 1
                        continue
                    q = fill(st.text, r.vals)
                    kept = answers.get(q) if st.once else ""
                    if kept:
                        r.vals[st.var] = kept
                        r.i += 1
                        continue
                    r.waiting = "ask"
                    return Reply(" ".join(x for x in (lead, _plain(q)) if x), expects_reply=True)
                if st.kind == "write":
                    draft = self._draft(st, r.vals)
                    if not draft:
                        self.stop()
                        return Reply(" ".join(x for x in (lead, f"I couldn't write {fill(st.text, r.vals)} — the "
                                     f"language model didn't answer, so I stopped at step {r.i + 1}.") if x), ok=False)
                    r.vals[st.var] = draft
                    r.waiting = "write"
                    return Reply(" ".join(x for x in (lead, f"Here's the {st.var.replace('_', ' ')}: {_end(draft)} "
                                                            f"Should I use it, or what should I change?") if x),
                                 expects_reply=True)
                if st.kind == "confirm":
                    r.waiting = "confirm"
                    return Reply(" ".join(x for x in (lead, fill(st.text, r.vals)) if x), expects_reply=True)
                if st.kind == "you":
                    r.waiting = "you"
                    return Reply(" ".join(x for x in (lead, f"Your turn: {fill(st.text, r.vals)}. Say “next” when "
                                                            f"it's done.") if x), expects_reply=True)
                reply = self._do_step(fill(st.text, r.vals))
                if reply is None or (not reply.ok and not reply.expects_reply):
                    why = reply.text.rstrip() if reply is not None else f"I don't know how to “{fill(st.text, r.vals)}” any more."
                    r.waiting = "repair"
                    return Reply(" ".join(x for x in (lead, f"{why} That was step {r.i + 1}, “{fill(st.text, r.vals)}”. "
                                                            f"How should I do it now? (Or say “skip” or “stop”.)") if x),
                                 ok=False, expects_reply=True)
                r.i += 1
                r.last = reply.text
                if reply.expects_reply:
                    r.waiting = "step"
                    return Reply(" ".join(x for x in (lead, reply.text) if x), expects_reply=True)
                lead = ""
        finally:
            activity.end()
        last = r.last
        self.stop()
        try:
            skills.note_result(r.skill, True)
        except Exception:
            log.debug("lesson.note_result_failed", exc_info=True)
        return Reply(" ".join(x for x in (lead, last or "Done.") if x))

    def _feed_run(self, text: str) -> Optional[Reply]:
        from modules.agent.confirm import classify_reply
        r = self._run
        r.touched = time.time()
        said = _clean(text)
        st = r.steps[r.i] if r.i < len(r.steps) else None
        if r.waiting == "ask" and st is not None:
            answer = _tidy(text)
            r.vals[st.var] = self._value_for(st, answer)
            if st.once:
                answers.put(fill(st.text, r.vals), r.vals[st.var])
            r.i += 1
            r.waiting = ""
            return self._advance()
        if r.waiting == "write" and st is not None:
            if _USE_IT.match(said):
                r.i += 1
                r.waiting = ""
                return self._advance()
            if _CANCEL.match(said) or said in ("stop", "cancel"):
                self.stop()
                return Reply("Okay, I stopped there.")
            draft = self._draft(st, r.vals, change=text.strip())
            if not draft:
                return Reply("I couldn't rewrite it just now. Should I use it as it is?", expects_reply=True)
            r.vals[st.var] = draft
            return Reply(f"How's this: {_end(draft)} Should I use it?", expects_reply=True)
        if r.waiting == "confirm" and st is not None:
            if classify_reply(said) is True or _USE_IT.match(said):
                r.i += 1
                r.waiting = ""
                return self._advance()
            nxt = r.steps[r.i + 1] if r.i + 1 < len(r.steps) else None
            self.stop()
            where = f" before “{fill(nxt.text, r.vals)}”" if nxt is not None else ""
            return Reply(f"Okay, I won't — I stopped{where}. Everything up to there is done, so you can finish "
                         f"it yourself.")
        if r.waiting == "you":
            if _CANCEL.match(said) or said in ("stop", "cancel"):
                self.stop()
                return Reply("Okay, I stopped there.")
            if not _NEXT.match(said):
                return None                       # something else in the meantime; still waiting for "next"
            r.i += 1
            r.waiting = ""
            return self._advance()
        if r.waiting == "repair" and st is not None:
            if _SKIP.match(said):
                r.i += 1
                r.waiting = ""
                return self._advance(lead="Skipped.")
            if _USER_DOES.match(said):
                st.kind, r.waiting = "you", ""
                self._save_steps()
                r.i += 1
                return self._advance(lead="Okay, that step's yours from now on.")
            reply, steps = self._do_new(text)
            if reply is None or (not reply.ok and not reply.expects_reply):
                why = reply.text.rstrip() if reply is not None else f"I don't know how to “{said}” either."
                return Reply(f"{why} Try another way, or say “skip” or “stop”.", ok=False, expects_reply=True)
            new = [Step("do", self._generalize_run(s)) for s in steps]
            r.steps[r.i:r.i + 1] = new
            r.i += len(new)
            r.waiting = "step" if reply.expects_reply else ""
            self._save_steps()
            if reply.expects_reply:
                return Reply(reply.text, expects_reply=True)
            return self._advance(lead=f"{reply.text.rstrip()} Got it — that's how I'll do it from now on.")
        if r.waiting == "step":
            r.waiting = ""
            return self._advance()
        return None

    def _save_steps(self):
        r = self._run
        try:
            skills.update(r.skill.id, r.skill.phrase, [s.line() for s in r.steps])
            log.info("lesson.repaired %r", r.skill.phrase)
        except Exception:
            log.exception("lesson.repair_save_failed")

    # ---- helpers ----------------------------------------------------------- #
    @staticmethod
    def _label(step: Step) -> str:
        return {"ask": "asking you", "write": "writing", "confirm": "checking with you",
                "you": "waiting for you"}.get(step.kind, step.text[:40])

    @staticmethod
    def _known_answer(step: Step, details: Dict[str, str], vals: Dict[str, str]) -> str:
        if step.kind != "ask":
            return ""
        if details.get("to") and _is_who(step):
            return details["to"]
        if details.get("about") and _is_about(step):
            return details["about"]
        return ""

    @staticmethod
    def _value_for(step: Step, answer: str) -> str:
        """The answer, or what it stands for ("school" -> the school address)."""
        a = answer.lower()
        if step.var.startswith("points") and _NOTHING.match(a.strip(" .!")):
            return ""                                   # "anything it has to say?" — "no"
        for opt, value in step.values.items():
            if re.search(rf"\b{re.escape(opt)}\b", a):
                return value
        return answer

    def _var_for(self, m) -> str:
        """'type the subject' / 'type it' -> the variable meant, or '' (literal text to type)."""
        t = self._teach
        names = [s.var for s in t.steps if s.var]
        if not names:
            return ""
        if m.group("it"):
            return names[-1]
        what = re.sub(r"'s$", "", (m.group("what") or "").strip())
        writes = [s.var for s in t.steps if s.kind == "write"]
        asks = [s.var for s in t.steps if s.kind == "ask"]
        if m.group("det") in ("their", "his", "her") or re.search(r"\b(?:address|recipient|name)\b", what):
            # "type their email" is the address; "type the email" is the message.
            return next((v for v in reversed(asks) if v.endswith(("email", "address")) or v == "recipient"), "")
        if what in _SUBJECT_WORDS:
            return next((v for v in writes if v.startswith("subject")), "")
        if what in _MESSAGE_WORDS and writes:
            return next((v for v in reversed(writes) if v.startswith("message")), writes[-1])
        if what in names:
            return what
        return next((v for v in names if what.replace(" ", "_") == v or v.startswith(what.replace(" ", "_"))), "")

    def _generalize(self, step: str) -> str:
        """Put {names} where this time's answers appear, so the next run uses its own."""
        t = self._teach
        out = step
        for var, value in sorted(t.vals.items(), key=lambda kv: -len(kv[1] or "")):
            for v in {value, t.said.get(var, "")}:
                if v and len(v) >= 3 and len(v) <= 120 and "\n" not in v:
                    out = re.sub(rf"(?<![\w@.]){re.escape(v)}(?![\w@])", "{" + var + "}", out, flags=re.I)
        return out

    def _generalize_run(self, step: str) -> str:
        r = self._run
        out = step
        for var, value in sorted(r.vals.items(), key=lambda kv: -len(kv[1] or "")):
            if value and 3 <= len(value) <= 120 and "\n" not in value:
                out = re.sub(rf"(?<![\w@.]){re.escape(value)}(?![\w@])", "{" + var + "}", out, flags=re.I)
        return out

    @staticmethod
    def _type(value: str, where: str = ""):
        """Typed straight through the tool: the router would read "type I'll be in class"
        as text for a box called "class"."""
        from modules.agent.router import run_tool
        kwargs = {"text": value, **({"target": where} if where else {})}
        into = f" into the {where}" if where else ""
        return run_tool("desktop.type_text", f"type that{into}", lambda _r: f"Typed it{into}.", **kwargs)

    def _do_step(self, command: str):
        """Run one learned command. None when it means nothing any more."""
        m = re.match(r"^type (.+?)(?: into (?:the )?(.+))?$", command, re.I | re.S)
        if m and self._run is not None:
            raw = next((s.text for s in self._run.steps[self._run.i:self._run.i + 1]), "")
            if raw.startswith("type {"):
                return self._type(m.group(1), m.group(2) or "")
        sk = skills.match(command)
        if sk is not None and not is_lesson(sk.steps):
            from modules.learning.skills import run_steps
            return run_steps(sk.steps)
        from modules.agent.router import route, run_plan
        it = route(command)
        if it is None:
            return None
        try:
            return run_plan([it])
        except Exception:
            log.exception("lesson.step_crashed %r", command)
            return Reply(f"Something went wrong doing “{command}”.", ok=False)

    def _do_new(self, text: str):
        """A step said aloud while teaching or repairing: (reply, steps that did it)."""
        reply = self._do_step(text)
        if reply is not None:
            return reply, [_tidy(text)]
        from modules.learning import planner
        try:
            out = planner.attempt(text)
        except Exception:
            log.exception("lesson.plan_failed")
            out = None
        if out is None:
            return None, []
        return out.reply, list(out.steps)

    @staticmethod
    def _draft(step: Step, vals: Dict[str, str], change: str = "") -> str:
        from modules.agent.compose import write
        known = {k: v for k, v in vals.items() if k != step.var and v and k != "email"}
        try:
            return write(fill(step.text, vals), details=known, change=change, current=vals.get(step.var, ""),
                         one_line=step.var.startswith("subject"))
        except Exception as e:
            log.warning("lesson.draft_failed %s", e)
            return ""


lessons = LessonManager()

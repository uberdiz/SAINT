"""
modules/automation/scene_plan.py

A scene as the user wrote it -> steps SAINT can run *with* the user.

Scene steps are plain sentences. Most are commands ("open my browser", "set
volume to 70") and run as they are. Some are instructions about the
conversation — and those used to be run like commands too, so "write me an
email" opened Gmail, sent "ask which email to send from" to the chat model
(which answered and carried on), and clicked Compose before anyone had been
asked anything (2026-10-05). Now they become lesson steps
(modules/learning/lesson.py), whose runner asks, waits for the answer, drafts
text for review and stops before anything is sent:

    ask which email to send from, personal or school
        -> ask: Which email should I send from, personal or school? -> account (personal = …; school = …)
    ask for email if not saved
        -> ask: Who's it to? -> recipient
           ask once: What's {recipient}'s email address? -> email      (remembered per person)
           type {email}
    ask what to write about                    -> ask: What should I write about? -> topic
    make title based on desc and create contents of the email and ask to send or edit
        -> write: a short subject line for the email about {topic} -> subject
           type {subject} into the subject
           write: the email's text about {topic} -> message
           type {message} into the message body
           confirm: Should I send it?
    Click send if yes.                         -> Click send          (the confirm above guards it)
    hover over the profile …, if @gmail.com = personal else school
    if need change click on the profile and click the other email (a@gmail.com or b@school.edu)
        -> the answers map to addresses, and Gmail is opened *as* that account
           (mail.google.com/mail/?authuser=…) instead of clicking through the profile menu

Every question is asked before the first action, so nothing is left half done
while SAINT waits. A step it can't do by itself (a condition it can't check)
becomes the user's ("you: …" — SAINT waits for "next"), never a guess.
Deterministic: no model call.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from modules.learning import lesson as L
from modules.learning.lesson import Step

_EMAILISH = re.compile(r"\b(?:e-?mail|gmail|outlook|inbox|compose)\b|mail\.google|@", re.I)
_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_GOOGLE_URL = re.compile(r"https?://(?:mail|docs|drive|calendar|meet)\.google\.com\S*", re.I)
_CONSUMER = re.compile(r"@(?:gmail|googlemail|outlook|hotmail|live|yahoo|icloud|me|proton(?:mail)?|aol)\.", re.I)
_PERSONAL = re.compile(r"\b(?:personal|home|private|my own|main|gmail)\b", re.I)
_WORK = re.compile(r"\b(?:school|work|uni|university|college|office|job|business|class)\b", re.I)

# Clauses within one step: "make the title and create the contents and ask to send"
_VERBS = (r"ask|make|create|write|draft|compose|generate|click|press|hit|tap|type|enter|fill|open|go|navigate|"
          r"send|select|choose|pick|hover|scroll|wait|close|switch|play|search|paste|copy|save|then")
_SPLIT = re.compile(rf"\s*(?:,\s*)?\b(?:and(?:\s+then)?|then)\s+(?=(?:{_VERBS})\b)", re.I)

_IF = re.compile(r"\b(?:if|else|otherwise|unless|depending)\b|\s=\s|=", re.I)
_IF_YES_AFTER = re.compile(r"^(?P<act>.+?),?\s+(?:only\s+)?if\s+(?:i\s+say\s+|i\s+said\s+|the\s+answer\s+is\s+|"
                           r"they\s+say\s+|it'?s\s+)?(?:yes|ok(?:ay)?|so|confirmed|approved|good)\W*$", re.I)
_IF_YES_BEFORE = re.compile(r"^(?:only\s+)?if\s+(?:i\s+say\s+|the\s+answer\s+is\s+)?(?:yes|ok(?:ay)?|so),?\s+"
                            r"(?:then\s+)?(?P<act>.+)$", re.I)
_ASK_TO = re.compile(r"^ask(?:\s+me)?\s+(?:to|if\s+i\s+want\s+(?:you\s+)?to|whether\s+(?:to|i\s+want\s+to)|"
                     r"if\s+(?:i|you)\s+should)\s+(?P<verb>[a-z]+)(?:\s+(?:it|this|that))?"
                     r"(?:\s+or\s+(?P<alt>[a-z]+)(?:\s+(?:it|this|that))?)?$", re.I)
_ONCE_EXTRA = re.compile(r",?\s+(?:if|when)\s+(?:not|it'?s\s+not|it\s+isn'?t|isn'?t)\s+(?:saved|known|stored|"
                         r"remembered)(?:\s+(?:yet|already))?$|,?\s+if\s+(?:you\s+)?(?:don'?t|do\s+not)\s+"
                         r"(?:have|know)\s+(?:it|that|one)(?:\s+yet)?$", re.I)
_FOR_ADDRESS = re.compile(r"^(?:for\s+)?(?:(?:an?|the|their|his|her|the\s+recipient'?s)\s+)?(?:e-?mail(?:\s+address)?|"
                          r"address|recipient(?:'?s)?\s+(?:e-?mail|address))$|^(?:what|which)\s+(?:e-?mail|address)\s+"
                          r"to\s+send\s+(?:it\s+)?to$|^who\s+to\s+send\s+(?:it\s+)?to$", re.I)
_MAKE = re.compile(r"^(?:make|create|write|draft|compose|generate|come\s+up\s+with)\s+(?:me\s+)?(?:(?:a|an|the)\s+)?"
                   r"(?:(?:short|good|nice|fitting|catchy|proper)\s+)*(?P<noun>title|subject(?:\s+line)?|contents?|"
                   r"body|message|email|e-mail|text|reply|draft)\b(?P<rest>.*)$", re.I)
_SUBJECTISH = re.compile(r"^(?:title|subject)", re.I)
_ACCOUNTISH = re.compile(r"\b(?:profile|account|signed\s+in|logged\s+in|other\s+e-?mail|switch|change)\b", re.I)


@dataclass
class Plan:
    lines: List[str]                                  # lesson step lines, in run order
    notes: List[str] = field(default_factory=list)    # what SAINT changed / hands over, in plain words

    @property
    def interactive(self) -> bool:
        return L.is_lesson(self.lines)


def _conditional(text: str) -> bool:
    """'if … else …' / 'x = y' — not counting a "?a=b" inside a link or an address."""
    return bool(_IF.search(re.sub(r"https?://\S+|" + _ADDRESS.pattern, " ", text or "")))


def _clauses(step: str) -> List[str]:
    """One written step -> its parts. Conditions are kept whole ("if …, click X and click Y")."""
    s = re.sub(r"\s+", " ", (step or "").strip()).rstrip(".")
    if not s:
        return []
    if L.parse(s).kind != "do" or _conditional(s):
        return [s]
    parts = [p.strip(" ,") for p in _SPLIT.split(s)]
    return [p for p in parts if p]


def _mapping(options: List[str], text: str) -> Dict[str, str]:
    """Which address each answer means, from what the scene says about them:
    "if @gmail.com = personal else school" and "(a@gmail.com or b@school.example.edu)"."""
    addrs = list(dict.fromkeys(_ADDRESS.findall(text)))
    if len(options) < 2 or len(addrs) < 2:
        return {}
    out: Dict[str, str] = {}
    low = text.lower()
    for opt in options:
        # "@gmail.com = personal" / "personal = @gmail.com" / "personal is gmail"
        m = re.search(rf"(@?[\w.-]+\.[a-z]{{2,}})\s*(?:=|is|means)\s*{re.escape(opt)}\b", low) or \
            re.search(rf"\b{re.escape(opt)}\s*(?:=|is|means)\s*(@?[\w.-]+\.[a-z]{{2,}})", low)
        if m:
            frag = m.group(1).lstrip("@")
            hit = next((a for a in addrs if a.lower().endswith(frag) or frag in a.lower()), None)
            if hit:
                out[opt] = hit
    left = [a for a in addrs if a not in out.values()]
    rest = [o for o in options if o not in out]
    if len(rest) == 1 and len(left) == 1:
        out[rest[0]] = left[0]                          # "… else school": the other address
    elif rest and len(left) >= len(rest):
        # No rule written down: personal ~ a consumer address, school / work ~ the other one.
        for opt in rest:
            want_consumer = bool(_PERSONAL.search(opt)) and not _WORK.search(opt)
            hit = next((a for a in left if bool(_CONSUMER.search(a)) == want_consumer), None) \
                if (_PERSONAL.search(opt) or _WORK.search(opt)) else None
            if hit:
                out[opt] = hit
                left.remove(hit)
    return out if len(out) == len(options) else {}


class _Builder:
    def __init__(self, name: str, steps: List[str]):
        self.name = name
        self.raw = steps
        self.text = " ".join([name] + steps)
        self.emailish = bool(_EMAILISH.search(self.text))
        self.steps: List[Step] = []
        self.notes: List[str] = []

    # ---- helpers -------------------------------------------------------- #
    def used(self):
        return {s.var for s in self.steps if s.var}

    def find_ask(self, pred) -> Optional[Step]:
        return next((s for s in self.steps if s.kind == "ask" and pred(s)), None)

    def add_ask(self, question: str, var: str = "", once: bool = False) -> Step:
        st = Step("ask", question, var or L._var_name(question, self.used()), once=once)
        self.steps.append(st)
        return st

    def recipient(self) -> Step:
        return self.find_ask(L._is_who) or self.add_ask("Who's it to?", "recipient")

    def topic(self) -> Step:
        return self.find_ask(L._is_about) or self.add_ask("What's it about?", "topic")

    def confirm(self, question: str):
        if not self.steps or self.steps[-1].kind != "confirm":
            self.steps.append(Step("confirm", question))

    # ---- one clause ------------------------------------------------------ #
    def clause(self, raw: str, index: int):
        said = L._clean(raw)
        lesson_line = L.parse(raw)
        if lesson_line.kind != "do":
            self.steps.append(lesson_line)
            return
        m = _IF_YES_AFTER.match(raw) or _IF_YES_BEFORE.match(raw)
        if m:
            act = m.group("act").strip()
            self.confirm(self._should(act))
            self.steps.append(Step("do", act))
            return
        m = _ASK_TO.match(said)
        if m:
            self.confirm(self._should(m.group("verb")))
            return
        m = L._CONFIRM.match(said)
        if m:
            act = next((g for g in m.groups() if g), "") or ""
            act = re.sub(r"^(\w+ing)\b", lambda g: L._GERUND.get(g.group(1), g.group(1)), act.strip())
            self.confirm(f"Should I {re.sub(r'^(?:you |i )', '', act)}?" if act else "Should I go ahead?")
            return
        m = L._ASK.match(said)
        if m:
            self.ask(m.group("q"))
            return
        m = _MAKE.match(said)
        if m:
            self.write(m.group("noun"))
            return
        if _conditional(raw):
            self.conditional(raw, index)
            return
        self.steps.append(Step("do", raw))           # as written: quotes, paths and links intact

    @staticmethod
    def _should(act: str) -> str:
        act = act.strip().rstrip(".")
        m = re.match(r"^(?:click|press|hit|tap)\s+(?:the\s+|on\s+)?(\w+)(?:\s+button)?$", act, re.I)
        verb = m.group(1).lower() if m else act[:1].lower() + act[1:]
        return f"Should I {verb} it?" if m or re.fullmatch(r"[a-z]+", verb) else f"Should I {verb}?"

    def ask(self, q: str):
        once = bool(L._ONCE.search(q) or _ONCE_EXTRA.search(q))
        q = _ONCE_EXTRA.sub("", L._ONCE.sub("", q)).strip(" ,")
        if _FOR_ADDRESS.match(q) and self.emailish:
            rec = self.recipient()
            if not self.find_ask(lambda s: s.var.endswith("email")):
                self.add_ask("What's {" + rec.var + "}'s email address?", "email", once=True)
            self.steps.append(Step("do", "type {email}"))       # the To box has focus after Compose
            return
        last = next((s.var for s in reversed(self.steps) if s.kind == "ask"), "")
        question = L.direct_question(q, owner="{" + last + "}" if last else "")
        st = self.add_ask(question, once=once)
        if L._is_about(st) and st.var != "topic" and "topic" not in self.used():
            st.var = "topic"

    def write(self, noun: str):
        topic = self.topic() if self.emailish else self.find_ask(L._is_about)
        about = f" about {{{topic.var}}}" if topic is not None else ""
        kind = "email" if self.emailish else "text"
        used = self.used()
        if _SUBJECTISH.match(noun):
            st = Step("write", f"a short subject line for the {kind}{about}", L._write_var("subject", used))
            where = "subject"
        else:
            st = Step("write", f"the {kind}'s text{about}", L._write_var("message", used))
            where = "message body"
        self.steps.append(st)
        if self.emailish:
            self.steps.append(Step("do", f"type {{{st.var}}} into the {where}"))

    def conditional(self, raw: str, index: int):
        ask = self.find_ask(lambda s: len(s.options) >= 2)
        if ask is not None and _ACCOUNTISH.search(raw):
            if not ask.values:
                ask.values = _mapping(ask.options, self.text)
            if ask.values:
                return                  # handled by opening the app as that account (finish())
        self.steps.append(Step("you", L._tidy(raw)))
        self.notes.append(f"I can't check “{L._tidy(raw)}” myself, so at step {index + 1} I'll ask you to do "
                          f"that part and wait for “next”.")

    # ---- whole scene ------------------------------------------------------ #
    def finish(self) -> Plan:
        ask = self.find_ask(lambda s: bool(s.values) and all(_ADDRESS.fullmatch(v) for v in s.values.values()))
        if ask is not None:
            for st in self.steps:
                m = _GOOGLE_URL.search(st.text) if st.kind == "do" else None
                if m:
                    base = re.match(r"https?://[^/]+", m.group(0)).group(0)
                    path = "/mail/" if "mail.google" in base else "/"
                    st.text = f'go to "{base}{path}?authuser={{{ask.var}}}"'
                    self.notes.append(f"I'll open {base.split('//')[1]} as the account you pick instead of "
                                      f"switching it in the profile menu.")
                    break
            else:
                self.steps.append(Step("you", f"make sure you're signed in as {{{ask.var}}}"))
        # Every question first: answers are known before anything happens on screen. A question that
        # needs something a write step makes stays where it is.
        written = {s.var for s in self.steps if s.kind == "write"}
        asks = [s for s in self.steps if s.kind == "ask" and not (set(L._VAR.findall(s.text)) & written)]
        rest = [s for s in self.steps if s not in asks]
        steps = L.compact(asks + rest)
        if steps and steps[-1].kind == "confirm":
            steps.pop()                                  # a question with nothing after it guards nothing
        return Plan([s.line() for s in steps], self.notes)


def plan(name: str, steps: List[str]) -> Plan:
    """The scene ``steps`` as lesson lines (unchanged commands when there's nothing to ask)."""
    b = _Builder(name, steps or [])
    for i, step in enumerate(steps or []):
        for part in _clauses(step):
            b.clause(part, i)
    return b.finish()

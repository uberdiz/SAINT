"""
modules/agent/recent.py

Short-term working memory: what SAINT just did, made, found or said.

    "take a screenshot of Claude" ... "delete that screenshot"
    "extract it to my games folder" ... "open that folder" ... "move it to C"
    "clean up my D drive" ... "what did you find?" ... "delete it"

Every successful tool call and every finished background task leaves a Thing
here (a file, folder, cleanup result, app, playlist...), newest first. The
router resolves "it" / "that folder" / "the junk" against them, and the model
gets the same list as real facts, so it answers "what did you find?" from
what actually happened instead of inventing a path.

Things older than ``MAX_AGE`` are forgotten; files that no longer exist are
skipped.
"""

import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

MAX_AGE = 30 * 60          # seconds a Thing stays referable
_KEEP = 40

# What a spoken noun can refer to.
_NOUN_KINDS = [
    (r"screen ?shots?|screen captures?|snips?", {"screenshot"}),
    (r"pictures?|images?|photos?|pics?", {"screenshot", "file"}),
    (r"folders?|director(?:y|ies)", {"folder"}),
    (r"archives?|zips?|rars?|7z", {"archive"}),
    (r"downloads?", {"download", "archive", "file"}),
    (r"files?|documents?", {"file", "screenshot", "archive", "download"}),
    (r"junk|installers?|duplicates?|copies|leftovers|caches?|temp(?:orary)?(?: files)?|stuff|"
     r"(?:giga|mega)bytes?|gigs?|[gm]b", {"cleanup"}),
    (r"playlists?", {"playlist"}),
    (r"songs?|tracks?", {"track"}),
    (r"windows?|apps?|programs?|games?", {"app"}),
]
_VERB_HINT = re.compile(r"\b(?:you|we|i)\s+(?:just\s+)?(made|created|extracted|unzipped|unpacked|moved|took|saved|"
                        r"downloaded|found|opened|played|started|launched|renamed)\b")
_VERB_GROUP = {"made": "created", "created": "created", "extracted": "extracted", "unzipped": "extracted",
               "unpacked": "extracted", "moved": "moved", "took": "took", "saved": "took",
               "downloaded": "downloaded", "found": "found", "opened": "opened", "played": "played",
               "started": "opened", "launched": "opened", "renamed": "renamed"}
_FILLER = {"the", "a", "an", "my", "your", "that", "this", "those", "these", "it", "them", "one", "you", "we",
           "just", "other", "too", "also", "again", "of", "all", "and", "i", "me", "for", "to", "in", "on",
           "made", "created", "extracted", "unzipped", "moved", "took", "saved", "downloaded", "found",
           "opened", "renamed", "new", "old", "last", "latest", "recent", "most"}


@dataclass
class Thing:
    kind: str                 # file | screenshot | folder | archive | download | cleanup | app | playlist | track | result
    label: str                # how SAINT would say it: "the screenshot of Claude"
    path: str = ""
    verb: str = ""            # created | extracted | moved | took | found | opened | played | downloaded
    data: Dict = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def alive(self, now: Optional[float] = None) -> bool:
        if (now or time.time()) - self.at > MAX_AGE:
            return False
        if self.path and self.kind in ("file", "screenshot", "folder", "archive", "download"):
            return os.path.exists(self.path)
        return True

    def words(self) -> Set[str]:
        text = f"{self.label} {os.path.basename(self.path) if self.path else ''} {self.data.get('name', '')}"
        return set(re.findall(r"[a-z0-9]+", _split_camel(text).lower()))


def _split_camel(s: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s or "")


class Recent:
    def __init__(self):
        self._lock = threading.Lock()
        self._things: List[Thing] = []

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    def note(self, kind: str, label: str, path: str = "", verb: str = "", **data) -> Thing:
        th = Thing(kind, label, os.path.normpath(path) if path else "", verb, data)
        with self._lock:
            if th.path:      # the same file noted again moves to the front
                self._things = [t for t in self._things
                                if not (t.path and os.path.normcase(t.path) == os.path.normcase(th.path)
                                        and t.kind == kind)]
            self._things.insert(0, th)
            del self._things[_KEEP:]
        return th

    def forget_paths(self, paths: Iterable[str]):
        gone = {os.path.normcase(os.path.normpath(p)) for p in paths if p}
        with self._lock:
            self._things = [t for t in self._things if not (t.path and os.path.normcase(t.path) in gone)]

    def clear(self):
        with self._lock:
            self._things.clear()

    def things(self, kinds: Optional[Set[str]] = None, max_age: float = MAX_AGE) -> List[Thing]:
        now = time.time()
        with self._lock:
            items = list(self._things)
        return [t for t in items if (not kinds or t.kind in kinds) and now - t.at <= max_age and t.alive(now)]

    def latest(self, kinds: Optional[Set[str]] = None, max_age: float = MAX_AGE) -> Optional[Thing]:
        items = self.things(kinds, max_age)
        return items[0] if items else None

    # ------------------------------------------------------------------ #
    # Resolving "it" / "that folder" / "the screenshot you took"
    # ------------------------------------------------------------------ #
    def find(self, phrase: str, want: Optional[Set[str]] = None, max_age: float = MAX_AGE,
             verbs: Optional[Set[str]] = None) -> Optional[Thing]:
        """The Thing a spoken reference means, or None. ``want`` is what the
        verb can act on (delete -> files/folders/cleanup, play -> playlists)."""
        p = re.sub(r"[^\w\s.'-]", " ", (phrase or "").lower()).strip()
        p = re.sub(r"\s+", " ", p)
        if not p:
            return None
        kinds: Set[str] = set()
        for pat, ks in _NOUN_KINDS:
            if re.search(rf"\b(?:{pat})\b", p):
                kinds |= ks
                break                     # the first (most specific) noun decides
        if want:
            kinds = (kinds & want) if kinds else set(want)
            if not kinds:
                return None
        verb = None
        vm = _VERB_HINT.search(p)
        if vm:
            verb = _VERB_GROUP.get(vm.group(1))
        # Content words that must match the thing's name ("the loop folder").
        content = [w for w in re.findall(r"[a-z0-9]+", p) if w not in _FILLER and
                   not any(re.fullmatch(pat, w) for pat, _ in _NOUN_KINDS) and not re.fullmatch(r"\d+(?:\.\d+)?", w)]
        candidates = self.things(kinds or None, max_age)
        if verbs:
            candidates = [t for t in candidates if t.verb in verbs or t.kind == "download"]
        if verb:
            with_verb = [t for t in candidates if t.verb == verb]
            candidates = with_verb or candidates
        if content and kinds != {"cleanup"}:
            # A name was said ("the loop folder"): only a thing with that name.
            named = [t for t in candidates if _names_match(content, t)]
            return named[0] if named else None
        return candidates[0] if candidates else None

    # ------------------------------------------------------------------ #
    # For the language model
    # ------------------------------------------------------------------ #
    def describe(self, n: int = 8) -> str:
        items = self.things(max_age=MAX_AGE)[:n]
        if not items:
            return ""
        lines = []
        for t in items:
            when = time.strftime("%H:%M", time.localtime(t.at))
            extra = f" ({t.path})" if t.path else ""
            detail = t.data.get("summary") or ""
            lines.append(f"- {when} {t.verb + ' ' if t.verb else ''}{t.label}{extra}"
                         + (f": {detail}" if detail and detail not in t.label else ""))
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Hooks
    # ------------------------------------------------------------------ #
    def note_tool(self, name: str, args: Dict, result):
        """A tool just succeeded: remember what it made, found or opened."""
        try:
            _from_tool(self, name, args or {}, result if isinstance(result, dict) else {})
        except Exception:
            pass

    def note_task(self, payload: Dict):
        """A background task finished (scan, extraction, move)."""
        try:
            _from_task(self, payload or {})
        except Exception:
            pass


def _names_match(content: List[str], t: Thing) -> bool:
    import difflib
    words = t.words()
    if not words:
        return False
    hits = 0
    for w in content:
        if w in words or any(difflib.SequenceMatcher(None, w, x).ratio() >= 0.8 for x in words if len(x) > 2):
            hits += 1
    return hits >= max(1, (len(content) + 1) // 2)


def _kind_for_path(path: str) -> str:
    from modules.files.archives import is_archive
    if os.path.isdir(path):
        return "folder"
    if is_archive(path):
        return "archive"
    return "file"


def _from_tool(r: "Recent", name: str, args: Dict, res: Dict):
    if name == "system.screenshot" and res.get("path"):
        what = res.get("what") or "your screen"
        r.note("screenshot", f"the screenshot of {what}", res["path"], "took")
    elif name == "files.open_folder" and res.get("opened"):
        r.note("folder", os.path.basename(res["opened"].rstrip("\\/")) or res["opened"], res["opened"], "opened")
    elif name in ("files.make_folder",) and res.get("path"):
        r.note("folder", os.path.basename(res["path"]), res["path"], "created")
    elif name == "files.rename" and res.get("path"):
        r.forget_paths([res.get("old", "")])
        r.note(_kind_for_path(res["path"]), os.path.basename(res["path"]), res["path"], "renamed")
    elif name == "files.recycle":
        r.forget_paths(x.get("path", "") if isinstance(x, dict) else x for x in res.get("recycled", []))
    elif name in ("files.cleanup_plan", "files.junk_report") and res.get("findings") is not None:
        _note_cleanup(r, res)
    elif name == "files.open_path" and res.get("opened"):
        p = res["opened"]
        r.note(_kind_for_path(p), os.path.basename(p), p, "opened")
    elif name in ("desktop.open_app", "desktop.focus_window") and (res.get("app") or res.get("window")):
        label = res.get("app") or res.get("window")
        r.note("app", str(label), "", "opened" if name == "desktop.open_app" else "switched to",
               window=res.get("window", ""))
    elif name == "steam.launch" and (res.get("name") or res.get("game")):
        r.note("app", res.get("name") or res.get("game"), "", "opened")
    elif name.startswith("spotify.play") and (res.get("name") or res.get("playlist") or res.get("track")):
        if res.get("playlist") or res.get("type") == "playlist" or name == "spotify.play_playlist":
            r.note("playlist", res.get("playlist") or res.get("name"), "", "played", uri=res.get("uri", ""))
        else:
            r.note("track", res.get("track") or res.get("name"), "", "played", uri=res.get("uri", ""))


def _note_cleanup(r: "Recent", rep: Dict):
    from modules.files.scan import human
    findings = rep.get("findings") or []
    scope = rep.get("scope") or "your files"
    total = sum(f.get("size", 0) for f in findings)
    summary = rep.get("summary") or ""
    place = "Downloads" if str(scope).lower() == "downloads" else str(scope).rstrip("\\")
    r.note("cleanup", f"{human(total)} of clutter in {place}", "", "found",
           report=rep, summary=summary, scope=scope)


def _from_task(r: "Recent", p: Dict):
    if p.get("status") not in (None, "completed"):
        return
    res = p.get("result") if isinstance(p.get("result"), dict) else {}
    if res.get("archive") and res.get("dest") and not res.get("cancelled"):
        folder = res.get("folder") or res["dest"]
        r.note("archive", os.path.basename(res["archive"]), res["archive"], "extracted")
        r.note("folder", os.path.basename(folder.rstrip("\\/")), folder, "extracted",
               summary=f"extracted from {os.path.basename(res['archive'])}")
    elif res.get("moved") and res.get("to"):
        r.forget_paths([res["moved"]])
        r.note(_kind_for_path(res["to"]), os.path.basename(res["to"]), res["to"], "moved")
    elif res.get("findings") is not None:
        _note_cleanup(r, dict(res, summary=p.get("summary") or res.get("summary", "")))
    elif p.get("summary"):
        r.note("result", p.get("description") or "a background task", "", "finished", summary=p["summary"])


recent = Recent()


def _on_event(ev):
    from core.events import EventType
    if ev.type == EventType.TASK_DONE:
        recent.note_task(ev.payload or {})


try:
    from core.events import event_bus
    event_bus.subscribe(_on_event)
except Exception:
    pass

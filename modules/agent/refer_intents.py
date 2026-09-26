"""
modules/agent/refer_intents.py

Commands about something SAINT just did, made or found — resolved through
the short-term working memory (modules/agent/recent.py):

    "delete that screenshot" / "delete it" / "get rid of the folder you made"
    "open that folder" / "show me the screenshot" / "show it in Explorer"
    "move it to my C: games folder" / "move the loop folder to the D drive"
    "rename it to The Loop" / "copy the path"
    "delete the junk" / "the other old installers too"   (after a cleanup check)
    "go to my most recent download and delete it"
    "make a new folder in my games folder called The Loop"

Anything that removes or moves files still asks first (ALWAYS_CONFIRM).
These fire only when the reference resolves; otherwise the other parsers
get the sentence.
"""

import os
import re
from typing import List, Optional

from modules.agent.recent import Thing, recent
from modules.agent.router import Intent, Reply, _clean, call, run_tool

_DELETE = r"(?:delete|remove|trash|recycle|bin|throw (?:away|out)|get rid of|erase)"
# Things "delete it" may mean: what SAINT made or found, never a folder it merely opened.
_MADE = {"created", "extracted", "took", "moved", "downloaded", "found", "renamed"}
_OPEN = r"(?:open(?: up)?|show(?: me)?|pull up|bring up|go to|take me to|view|look at|let me see)"
_PATHLIKE = {"file", "screenshot", "folder", "archive", "download"}
# A bare "it"/"that" only reaches back this far (a named "that screenshot" reaches further).
PRONOUN_AGE = 5 * 60
_LATEST_DL = re.compile(r"^(?:my\s+|the\s+)?(?:most recent|latest|newest|last|recent)\s+download(?:ed file)?$|"
                        r"^(?:the\s+)?(?:file|thing) i (?:just )?downloaded$|^what i (?:just )?downloaded$")
_SELECTION = re.compile(r"^(?:this|these)\b|\bselected\b")


def _tidy(t: str) -> str:
    t = re.sub(r"\s+(?:too|as well|also|again|right now|now|for me|please|then)$", "", t.strip(" .!?,"))
    t = re.sub(r"\s+(?:too|as well|also|for me)$", "", t)
    return t.strip()


def _resolve(ref: str, want: set, verbs: Optional[set] = None) -> Optional[Thing]:
    """What ``ref`` points at, or None. Explorer selections ("this file") are
    left to parse_files, which reads what's selected."""
    ref = re.sub(r"^(?:all of|all)\s+", "", ref.strip()).strip()
    if not ref or _SELECTION.search(ref):
        return None
    if re.match(r"^my\s+", ref) and not _LATEST_DL.match(ref):
        return None                      # "my downloads" is the user's folder, not something SAINT did
    if _LATEST_DL.match(ref):
        from modules.files.ops import newest_download
        p = newest_download()
        if not p:
            return None
        return Thing("download", os.path.basename(p), p, "downloaded")
    bare = bool(re.fullmatch(r"(?:it|that|them|those|that one|the one|all of (?:it|them))", ref))
    return recent.find(ref, want, max_age=PRONOUN_AGE if bare else 30 * 60, verbs=verbs)


def parse_refer(text: str) -> Optional[Intent]:
    t = _tidy(_clean(text).lower())
    if not t:
        return None

    # "go to my most recent download and delete it" / "... and open it"
    m = re.match(rf"^(?:go to|open|find|select|click(?: on)?|pick)\s+(?P<ref>.+?)\s+and\s+(?:then\s+)?"
                 rf"(?P<verb>{_DELETE}|{_OPEN}|rename|move)\s+(?:it|that)(?P<rest>\s+.+)?$", t)
    if m and _resolve(m.group("ref"), _PATHLIKE):
        t = f"{m.group('verb')} {m.group('ref')}{m.group('rest') or ''}"

    # ---- make a folder -------------------------------------------------------------------------
    m = re.match(r"^(?:make|create|add)\s+(?:a\s+)?(?:new\s+)?folder(?:\s+(?:in|inside|on)\s+(?P<parent>.+?))?"
                 r"(?:\s+(?:called|named|for|with the name)\s+(?P<name>.+))?$", t)
    m2 = re.match(r"^(?:make|create|add)\s+(?:a\s+)?(?:new\s+)?folder\s+(?:called|named)\s+(?P<name>.+?)\s+"
                  r"(?:in|inside|on)\s+(?P<parent>.+)$", t)
    m = m2 or m
    if m and (m.group("name") or m.group("parent")):
        name = m.group("name") or ""
        return _make_folder_intent(m.group("parent") or "", _original_case(text, name) if name else "")

    # ---- delete -----------------------------------------------------------------------------
    m = re.match(rf"^{_DELETE}\s+(?P<ref>.+)$", t)
    if m:
        ref = m.group("ref")
        th = _resolve(ref, _PATHLIKE | {"cleanup"}, verbs=_MADE)
        if th is not None and th.kind == "cleanup":
            return Intent("files.cleanup_followup", lambda: _cleanup_followup(th, ref), "files")
        if th is not None:
            return Intent("files.recycle", lambda: _recycle_thing(th), "files")

    # "the other old installers too" as a whole follow-up
    if re.match(r"^(?:and\s+)?(?:the\s+)?(?:other|rest of the|remaining)\s+.+$|^what about the\s+.+$", t):
        th = recent.find(t, {"cleanup"}, max_age=15 * 60)
        if th is not None and _categories(t):
            return Intent("files.cleanup_followup", lambda: _cleanup_followup(th, t), "files")

    # ---- open / show ----------------------------------------------------------------------------
    m = re.match(rf"^(?:{_OPEN})\s+(?P<ref>.+?)(?P<ex>\s+in (?:file )?explorer)?$|"
                 rf"^where(?:'s| is| did you put)\s+(?P<ref2>.+?)(?:\s+saved)?$", t)
    if m:
        ref = m.group("ref") or m.group("ref2")
        th = _resolve(ref, _PATHLIKE)
        if th is not None:
            show = bool(m.group("ex") or m.group("ref2"))
            return Intent("files.open_recent", lambda: _open_thing(th, show), "files")

    # ---- move -------------------------------------------------------------------------------------
    m = re.match(r"^(?:move|put|transfer|send)\s+(?P<ref>.+?)\s+(?:back\s+)?(?:to|into|in|onto|over to)\s+"
                 r"(?:my\s+|the\s+)?(?P<dst>.+)$", t)
    if m and not re.search(r"\b(screen|monitor|display|side|workspace|playlist|queue|window|tab|recycle bin)\b",
                           m.group("dst")):
        th = _resolve(m.group("ref"), _PATHLIKE)
        dst = _destination(m.group("dst"))
        if th is not None and dst:
            return Intent("files.move", lambda: _move_thing(th, dst), "files")

    # ---- rename -----------------------------------------------------------------------------------
    m = re.match(r"^(?:rename|call)\s+(?P<ref>.+?)\s+(?:to|as)\s+(?P<new>.+)$|^name\s+(?P<ref2>it|that)\s+(?P<new2>.+)$", t)
    if m:
        th = _resolve(m.group("ref") or m.group("ref2"), _PATHLIKE)
        new = (m.group("new") or m.group("new2") or "").strip()
        if th is not None and new:
            raw_new = _original_case(text, new)
            return Intent("files.rename", lambda: _rename_thing(th, raw_new), "files")

    # ---- copy the path -----------------------------------------------------------------------------
    m = re.match(r"^copy\s+(?:the|its|that|the full)\s+(?:file\s+|folder\s+)?(?:path|location|address)"
                 r"(?:\s+(?:of|to|for)\s+(?P<ref>.+))?$", t)
    if m:
        th = _resolve(m.group("ref") or "it", _PATHLIKE)
        if th is not None:
            return Intent("files.copy_path", lambda: _say(call("files.copy_path", path=th.path), "Copied it."),
                          "files")

    # "open that folder" with nothing to point at: ask, don't guess an app called "that folder".
    m = re.match(rf"^(?:{_DELETE}|{_OPEN}|move|rename)\s+(?:that|the|those)\s+(?P<noun>folder|file|screenshot|picture|"
                 r"download|archive)s?(?:\s+(?:that\s+)?you\s+(?:just\s+)?(?:made|created|extracted|took|moved|"
                 r"downloaded|saved|opened))?$", t)
    if m:
        noun = m.group("noun")
        return Intent("recent.unknown", lambda: Reply(
            f"I haven't made or opened a {noun} in the last few minutes, so I'm not sure which one you mean. Say its "
            f"name, or select it in File Explorer and say “this {noun}”.", ok=False), "files")
    return None


# ---------------------------------------------------------------------- #
def _say(res, fallback: str) -> Reply:
    if res.success:
        return Reply((res.result or {}).get("summary") or fallback)
    return Reply(res.error or fallback, ok=False)


def _original_case(text: str, lowered: str) -> str:
    """The user's own capitals for a name ("TheLoop" typed stays TheLoop)."""
    i = text.lower().rfind(lowered)
    return text[i:i + len(lowered)].strip(" .!?\"'") if i >= 0 else lowered


def _destination(spoken: str) -> Optional[str]:
    from modules.files.paths import resolve_folder
    s = re.sub(r"\s+(?:folder|directory)$", "", spoken.strip())
    return resolve_folder(spoken) or resolve_folder(s) or resolve_folder(s + " folder")


def _make_folder_intent(parent: str, name: str) -> Optional[Intent]:
    from modules.files.paths import resolve_folder
    parent_path = ""
    if parent:
        th = recent.find(parent, {"folder"}) if re.match(r"^(?:it|that|there|this)\b", parent) else None
        parent_path = th.path if th else (_destination(parent) or "")
        if not parent_path:
            return None
    name = name.strip() or "New folder"
    return Intent("files.make_folder",
                  lambda: _say(call("files.make_folder", name=name, parent=parent_path), "Made it."), "files")


def _recycle_thing(th: Thing) -> Reply:
    from modules.agent.files_intents import _recycle
    return _recycle([th.path])


def _open_thing(th: Thing, show: bool) -> Reply:
    if show:
        return _say(call("files.show_in_explorer", path=th.path), "Here it is.")
    if os.path.isdir(th.path):
        return run_tool("files.open_folder", f"open {th.label}", lambda r: f"Opened {r['opened']}.", name=th.path)
    return _say(call("files.open_path", path=th.path), "Opened it.")


def _move_thing(th: Thing, dst: str) -> Reply:
    from modules.agent.files_intents import _move
    return _move(th.path, dst)


def _rename_thing(th: Thing, new: str) -> Reply:
    return run_tool("files.rename", f"rename {os.path.basename(th.path)} to {new}",
                    lambda r: r.get("summary", "Renamed it."), path=th.path, new_name=new)


# ---------------------------------------------------------------------- #
# Cleanup follow-ups
# ---------------------------------------------------------------------- #
_CATEGORY_WORDS = {
    "duplicate": r"duplicates?|copies|copy",
    "extracted": r"extracted|archives?|zips?|rars?",
    "old_installer": r"installers?|setups?|setup files",
    "temp": r"temp(?:orary)?(?: files)?",
    "shader_cache": r"shaders?|shader caches?",
    "windows_update": r"windows update|update (?:files|leftovers|cache)|leftovers",
    "recordings": r"recordings?|clips?|videos?",
    "browser_cache": r"browser(?: caches?)?|chrome|edge|opera|firefox",
    "app_cache": r"app caches?|discord|spotify|vs code",
    "crash_dumps": r"crash(?: dumps?)?|dumps?",
    "dev_cache": r"(?:developer|dev) caches?|pip|npm|uv|yarn",
    "big_download": r"big (?:old )?(?:downloads|files)",
}


def _categories(text: str) -> List[str]:
    return [c for c, pat in _CATEGORY_WORDS.items() if re.search(rf"\b(?:{pat})\b", text)]


def _cleanup_followup(th: Thing, said: str) -> Reply:
    """'Delete it' / 'the other old installers too' after SAINT reported junk."""
    from modules.files.paths import _live_windows, _under
    from modules.files.tools import ask_to_recycle_findings
    rep = th.data.get("report") or {}
    findings = [f for f in rep.get("findings") or [] if f.get("path") and os.path.exists(f["path"])]
    if not findings:
        return Reply("Everything from that check is already gone.")
    cats = _categories(said)
    chosen = [f for f in findings if f["category"] in cats] if cats else \
        [f for f in findings if f["action"] == "recycle"]
    # Windows Update leftovers outside Windows itself (D:\WUDownloadCache) may go
    # when asked for; the ones inside Windows need Disk Cleanup.
    if not chosen and not cats:
        chosen = [f for f in findings if f["category"] == "windows_update"]
    inside = [f for f in chosen if f["category"] == "windows_update" and _under(f["path"], _live_windows())]
    chosen = [f for f in chosen if f not in inside and f["category"] != "old_windows"]
    if not chosen:
        if inside or any(f["action"] == "cleanmgr" for f in findings):
            drive = (inside[0]["path"] if inside else findings[0]["path"])[:1]
            call("files.disk_cleanup", drive=drive)
            return Reply(f"Those are Windows Update files, which only Disk Cleanup can remove safely — I opened "
                         f"it for {drive}:. Tick “Windows Update Cleanup” there.")
        if any(f["category"] == "old_windows" for f in findings):
            return Reply("That's an old Windows installation — too big for the Recycle Bin, so I won't touch it. "
                         "Copy anything you want from its Users folder, then format the drive in Disk Management.")
        return Reply("There's nothing of that kind left from that check.")
    desc = ask_to_recycle_findings(chosen)
    if not desc:
        return Reply("There's nothing of that kind left from that check.")
    return Reply(f"Should I {desc}?", expects_reply=True)

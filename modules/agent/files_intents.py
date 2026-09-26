"""
modules/agent/files_intents.py

Voice commands for files and storage.

Whole-sentence tasks (checked before multi-step splitting, so a long request
isn't cut into parts that each mean something else):
    "Go to my Downloads folder, click the first download, and extract it using
     WinRAR to my games folder"                         -> one files.extract
    "unzip the latest download and delete it afterwards"
    "compress this folder as a rar"

Single commands:
    "how much space is left on E" / "how full are my drives"
    "what's taking up space on D" / "biggest folders on C"
    "clean up my Downloads" / "what can I delete on E" / "only the duplicates"
    "find my emulators" / "where are my ROMs"
    "open my Downloads" / "go to my games folder" / "open the D drive"
    "move this folder to the D drive" / "delete this file" (Recycle Bin, asks first)
    "empty the recycle bin"                              -> never; opens it instead
"""

import os
import re
from typing import List, Optional

from modules.agent.router import Intent, Reply, _clean, call, run_tool

_ARCH_VERB = re.compile(r"\b(extract|unzip|unrar|unpack|decompress|un-?zip)\b", re.I)
_WINRAR = re.compile(r"\s*,?\s*\b(?:using|with|in|through|via)\s+(?:win ?rar|7-?zip|rar)\b", re.I)
_LATEST = re.compile(r"\b(latest|newest|most recent|last|first|top|recent)\s+(?:download|file|one|archive|thing)|"
                     r"\bwhat i (?:just )?downloaded\b|\bthe download\b", re.I)
_DELETE_AFTER = re.compile(r"\b(?:delete|remove|trash|recycle|get rid of|throw away)\b[^.]*?\b(?:it|archive|rar|zip|"
                           r"file|original|download)\b[^.]*?\b(?:after(?:wards?)?|when (?:it'?s |you'?re )?done|"
                           r"once (?:it'?s |you'?re )?done)\b|\band (?:then )?(?:delete|remove|trash) (?:it|the "
                           r"(?:archive|rar|zip))\b", re.I)


def _say(res, fallback: str) -> Reply:
    if res.success:
        return Reply((res.result or {}).get("summary") or fallback)
    return Reply(res.error or fallback, ok=False)


# ====================================================================== #
# Whole-sentence tasks (before composite splitting)
# ====================================================================== #
def parse_files_task(text: str) -> Optional[Intent]:
    raw = _clean(text)
    t = raw.lower().strip(" .!?")
    if _ARCH_VERB.search(t) and _about_an_archive(t):
        return _extract_intent(raw, t)
    m = re.match(r"^(?:compress|zip up|zip|rar|archive|pack up)\s+(?:up\s+)?(this|that|the|my)?\s*(.+?)"
                 r"(?:\s+folder)?(?:\s+(?:as|into|to|in)\s+(?:a\s+)?(zip|rar)(?:\s+file)?)?$", t)
    if m and not re.search(r"\b(song|playlist|window|tab)\b", t):
        target = _resolve_targets(m.group(2), m.group(1))
        if target:
            fmt = m.group(3) or "zip"
            return Intent("files.compress", lambda: _say(call("files.compress", folder=target[0], fmt=fmt),
                                                          "Compressing it."), "files")
    return None


def _about_an_archive(t: str) -> bool:
    """'unzip it' always is; 'extract' only with an archive / download in view —
    not 'extract the key points from this page'."""
    if re.search(r"\b(unzip|unrar|un-zip|decompress|unpack)\b", t):
        return True
    if re.search(r"\b(page|text|points?|info(?:rmation)?|summary|screen|video|audio|frames?|emails?|numbers?|"
                 r"table|quote|data from)\b", t):
        return False
    return bool(re.search(r"\b(zip|rar|7z|winrar|archive|downloads?|it|this|that|file|folder)\b", t))


def _extract_intent(raw: str, t: str) -> Optional[Intent]:
    t2 = _WINRAR.sub("", t)
    delete_after = bool(_DELETE_AFTER.search(t2))
    t2 = _DELETE_AFTER.sub("", t2).strip(" ,.")
    # Destination: the last "to/into <folder>" after the extract verb.
    verb = _ARCH_VERB.search(t2)
    after = t2[verb.end():] if verb else t2
    destination = ""
    m = re.search(r"\b(?:to|into|in)\s+(?:my\s+|the\s+)?([\w:\\/ .'-]+?)(?:\s+folder)?\s*$", after)
    if m:
        from modules.files.paths import resolve_folder
        cand = m.group(1).strip()
        if resolve_folder(cand) or resolve_folder(cand + " folder"):
            destination = cand
            after = after[:m.start()].strip()
    # Source: Explorer selection ("extract this"), a named archive, or the newest download.
    archive, source = "latest", "downloads"
    after = re.sub(r"\b(?:right\s+)?(?:here|in place|next to it|where it is)\b", "", after).strip(" ,")
    words = re.sub(r"^(?:the|it|this|that|them)\b\s*", "", after.strip())
    words = re.sub(r"\b(?:file|archive|rar|zip|download|one)\b", "", words).strip(" ,")
    selected = None
    if re.match(r"^(?:this|that|it|these|the selected|what'?s selected)\b", after.strip()) and \
            not re.search(r"\bdownloads?\b", t):
        selected = _explorer_archive()
    if selected:
        archive, source = selected, ""
    elif words and not _LATEST.search(t) and not re.fullmatch(r"(?:it|this|that|them|using|with|\s)*", words):
        archive = words
    def run():
        kwargs = {"archive": archive, "delete_after": delete_after}
        if source:
            kwargs["source"] = source
        if destination:
            kwargs["destination"] = destination
        return _say(call("files.extract", **kwargs), "Extracting it.")
    return Intent("files.extract", run, "files")


def _explorer_archive() -> Optional[str]:
    try:
        from modules.files.ops import explorer_selection
        from modules.files.archives import is_archive
        sel = explorer_selection()
        hits = [p for p in sel.get("selected") or [] if is_archive(p)]
        return hits[0] if hits else None
    except Exception:
        return None


# ====================================================================== #
# Single commands
# ====================================================================== #
_DRIVE = r"(?:(?:my|the)\s+)?(?P<drive>[a-z])(?::|\s+drive)?"


def _drive_or_folder(s: Optional[str]) -> str:
    s = (s or "").strip()
    m = re.fullmatch(r"(?:(?:my|the)\s+)?([a-z])(?::|\s+drive)?", s)
    return m.group(1).upper() if m else s


def parse_files(text: str) -> Optional[Intent]:
    raw = _clean(text)
    t = raw.lower().strip(" .!?")
    if not t:
        return None

    # ---- space ---------------------------------------------------------------------------
    if re.match(r"^(?:how much|what'?s the)\s+(?:free\s+|disk\s+)?(?:space|storage|room|disk space)"
                r"(?:\s+(?:is|do i have|have i got))?(?:\s+(?:left|free|available|remaining))?"
                r"(?:\s+on\s+(?:my\s+|the\s+)?(?:pc|computer|drives|disks|[a-z](?::|\s+drive)?))?$|"
                r"^how full (?:is|are) (?:my|the) (?:drives|disks|pc|[a-z](?::|\s+drive)?)$|"
                r"^check (?:my\s+)?(?:disk\s+)?(?:space|storage|drives)$|^(?:show me\s+)?my drives$", t):
        dm = re.search(r"\b(?:on|is)\s+(?:my\s+|the\s+)?([b-z])(?::|\s+drive)?$", t)
        drive = dm.group(1).upper() if dm else ""
        return Intent("files.drive_overview", lambda: _say(call("files.drive_overview", drive=drive),
                                                           "Here's your storage."), "files")

    # ---- what's using it -------------------------------------------------------------------------
    m = re.match(r"^what(?:'?s| is)\s+(?:taking|using|eating|hogging)\s+(?:up\s+)?(?:all\s+)?(?:the\s+|my\s+)?"
                 r"(?:most\s+)?(?:space|storage|room)(?:\s+on\s+(?P<t>.+))?$|"
                 r"^(?:what are|show me|find|list)\s+(?:the\s+|my\s+)?(?:biggest|largest)\s+(?:folders|files|things)"
                 r"(?:\s+on\s+(?P<t2>.+))?$|^scan\s+(?:my\s+|the\s+)?(?P<t3>.+?)(?:\s+drive)?$", t)
    if m:
        target = _drive_or_folder(m.group("t") or m.group("t2") or m.group("t3") or "C")
        if m.group("t3") and not re.fullmatch(r"[A-Z]", target) and not _folder_exists(target):
            m = None
        else:
            refresh = t.startswith("scan")
            return Intent("files.biggest", lambda: _say(call("files.biggest", target=target, refresh=refresh),
                                                        "Scanning."), "files")

    # ---- cleanup -------------------------------------------------------------------------------
    m = re.match(r"^(?:clean up|clean out|tidy up|declutter|clear out|free up space (?:in|on))\s+(?:my\s+|the\s+)?"
                 r"(?P<what>downloads?(?:\s+folder)?|[a-z](?::|\s+drive)|pc|computer|drives|storage|disk)$|"
                 r"^(?:what can i (?:delete|get rid of|remove|clean up)|find (?:some\s+)?(?:junk|stuff i can delete)|"
                 r"free up (?:some\s+)?(?:disk\s+)?space)(?:\s+on\s+(?:my\s+|the\s+)?(?P<what2>.+))?$", t)
    if m:
        what = (m.group("what") or m.group("what2") or "").strip()
        scope = "downloads" if (not what and "space" not in t) or what.startswith("download") else \
            _drive_or_folder(what) if re.fullmatch(r"[a-z](?::|\s+drive)?", what) else "all"
        return Intent("files.cleanup_plan", lambda: _cleanup(scope), "files")
    plan_reply = _plan_followup(t)
    if plan_reply is not None:
        return plan_reply

    # ---- games / emulators / ROMs ------------------------------------------------------------
    m = re.match(r"^(?:find|where are|show me|list)\s+(?:all\s+)?(?:of\s+)?my\s+(games|emulators|roms)"
                 r"(?:\s+on (?:my|this) (?:pc|computer))?$|^(?:find|look for)\s+(emulators|roms)$", t)
    if m:
        kind = m.group(1) or m.group(2)
        return Intent("files.find", lambda: _say(call("files.find", kind=kind), "Looking."), "files")

    # ---- open a folder ----------------------------------------------------------------------------
    m = re.match(r"^(?:open|go to|show(?: me)?|take me to|bring up|pull up|browse)\s+(?:up\s+)?(?:my\s+|the\s+)?"
                 r"(?P<f>downloads?|documents|desktop folder|pictures|photos|videos|music folder|games? folder|"
                 r"[a-z](?::|\s+drive)|drive [a-z]|[\w '-]+? folder)(?:\s+(?:folder|in (?:file )?explorer))?$", t)
    if m:
        name = m.group("f")
        from modules.files.paths import resolve_folder
        if resolve_folder(name) or resolve_folder(re.sub(r"\s+folder$", "", name)):
            spoken = re.sub(r"\s+folder$", "", name)
            return Intent("files.open_folder", lambda: run_tool(
                "files.open_folder", f"open {spoken}", lambda r: f"Opened {r['opened']}.", name=name), "files")

    # ---- disk cleanup / recycle bin ---------------------------------------------------------------
    m = re.match(r"^(?:open|run|start)\s+disk cleanup(?:\s+(?:on|for)\s+(?:my\s+|the\s+)?([a-z])(?::|\s+drive)?)?$", t)
    if m:
        drive = (m.group(1) or "c").upper()
        return Intent("files.disk_cleanup", lambda: _say(call("files.disk_cleanup", drive=drive),
                                                         "Opened Disk Cleanup."), "files")
    if re.match(r"^(?:empty|clear|purge)\s+(?:out\s+)?(?:the\s+|my\s+)?(?:recycle bin|trash|bin)$", t):
        def run_bin():
            try:
                os.startfile("shell:RecycleBinFolder")
            except OSError:
                pass
            return Reply("I never delete files permanently, so I won't empty it — I opened the Recycle Bin so "
                         "you can check it and empty it yourself.")
        return Intent("files.recycle_bin", run_bin, "files")

    # ---- move ------------------------------------------------------------------------------------
    m = re.match(r"^(?:move|transfer|put)\s+(?P<src>.+?)\s+(?:to|onto|into|over to)\s+(?:my\s+|the\s+)?(?P<dst>.+)$", t)
    if m and not re.search(r"\b(screen|monitor|display|side|left|right|workspace|playlist|queue|window|tab)\b",
                           m.group("dst")):
        from modules.files.paths import resolve_folder
        dst = m.group("dst")
        if resolve_folder(dst) or resolve_folder(dst + " folder"):
            dst = dst if resolve_folder(dst) else dst + " folder"
            src = _resolve_targets(m.group("src"))
            if src:
                return Intent("files.move", lambda: _move(src[0], dst), "files")

    # ---- delete (Recycle Bin, asks first) -------------------------------------------------------------
    m = re.match(r"^(?:delete|trash|recycle|throw away|get rid of|remove)\s+(.+?)(?:\s+(?:file|folder|files))?$", t)
    if m and not re.search(r"\b(reminder|alarm|timer|memory|memories|alias|workspace|playlist|song|scene|"
                           r"automation|notification)s?\b", t):
        targets = _resolve_targets(m.group(1))
        if targets:
            return Intent("files.recycle", lambda: _recycle(targets), "files")
    return None


def _folder_exists(name: str) -> bool:
    from modules.files.paths import resolve_folder
    return bool(resolve_folder(name))


def _cleanup(scope: str) -> Reply:
    res = call("files.cleanup_plan", scope=scope)
    if not res.success:
        return Reply(res.error or "I couldn't check that.", ok=False)
    r = res.result
    return Reply(r.get("summary", "Done."), expects_reply=bool(r.get("asked")))


_CATS = {"duplicate": r"duplicates?|copies", "extracted": r"extracted|archives?|zips?|rars?",
         "old_installer": r"installers?|setups?", "temp": r"temp(?:orary)?(?: files)?",
         "shader_cache": r"shaders?(?: caches?)?", "browser_cache": r"browser(?: caches?)?|chrome|edge|opera|firefox",
         "app_cache": r"app caches?|discord|spotify|vs code", "crash_dumps": r"crash(?: dumps?)?|dumps?",
         "dev_cache": r"(?:developer|dev) caches?|pip|npm|uv|yarn", "big_download": r"big (?:old )?(?:downloads|files)"}


def _plan_followup(t: str) -> Optional[Intent]:
    """'only the duplicates' / 'clean those up' after SAINT proposed a cleanup."""
    from modules.files.tools import last_plan
    from modules.agent.confirm import confirmations
    plan = last_plan()
    if not plan:
        return None
    m = re.match(r"^(?:only|just)\s+(?:the\s+)?(.+)$|^(?:clean|delete|recycle|remove|get rid of)\s+(?:only\s+|just\s+)?"
                 r"(?:the\s+)?(.+?)(?:\s+only)?$", t)
    if m:
        words = m.group(1) or m.group(2)
        cats = [c for c, pat in _CATS.items() if re.search(rf"\b(?:{pat})\b", words)]
        if cats:
            def run_subset():
                from modules.files.tools import _ask_to_recycle
                desc = _ask_to_recycle(plan, cats)
                if not desc:
                    return Reply("There's nothing of that kind to clear.")
                return Reply(f"Should I {desc}?", expects_reply=True)
            return Intent("files.cleanup_subset", run_subset, "files")
    if re.match(r"^(?:clean (?:those|them|that|it) up|(?:move|put|send) (?:those|them|that|it) (?:to|in) the "
                r"recycle bin|get rid of (?:those|them|it)|do it|go ahead(?: and clean (?:up|it))?)$", t) \
            and confirmations.pending is None:
        def run_all():
            from modules.files.tools import _ask_to_recycle
            desc = _ask_to_recycle(plan)
            return Reply(f"Should I {desc}?", expects_reply=True) if desc else \
                Reply("There's nothing safe to clear from that list.")
        return Intent("files.cleanup_subset", run_all, "files")
    return None


def _resolve_targets(spoken: str, determiner: Optional[str] = None) -> List[str]:
    """Spoken file/folder -> existing paths. 'this/that/these (file|folder)' =
    what's selected in File Explorer (or its open folder); a known folder
    name; or a file in the Explorer folder / Downloads whose name matches."""
    s = (spoken or "").strip().strip("\"'")
    s = re.sub(r"^(?:the|my)\s+", "", s)
    if (determiner in ("this", "that") and s in ("", "folder", "file")) or \
            re.fullmatch(r"(?:this|that|these|those|the selected|what'?s selected|it)(?:\s+(?:file|folder|files|"
                         r"folders|one|ones|stuff))?", s):
        try:
            from modules.files.ops import explorer_selection
            sel = explorer_selection()
        except Exception:
            return []
        if sel.get("selected"):
            return list(sel["selected"])
        if re.search(r"folder", s) and sel.get("folder"):
            return [sel["folder"]]
        return []
    if not s or len(s.split()) > 6:
        return []
    from modules.files.paths import resolve_folder, known_folder
    p = resolve_folder(s) or resolve_folder(s + " folder")
    if p and os.path.splitdrive(p)[1].strip("\\/"):
        return [p]
    words = [w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in ("file", "folder", "the")]
    if not words:
        return []
    places = []
    try:
        from modules.files.ops import explorer_selection
        f = explorer_selection().get("folder")
        if f:
            places.append(f)
    except Exception:
        pass
    dl = known_folder("downloads")
    if dl:
        places.append(dl)
    for place in places:
        try:
            hits = [e.path for e in os.scandir(place)
                    if all(w in re.sub(r"[^a-z0-9]+", " ", e.name.lower()) for w in words)]
        except OSError:
            continue
        if len(hits) == 1:
            return hits
    return []


def _move(src: str, dst: str) -> Reply:
    from modules.files.scan import human
    from modules.files.ops import item_size
    try:
        size = human(item_size(src))
    except OSError:
        size = "?"
    return run_tool("files.move", f"move {os.path.basename(src)} ({size}) to {dst}",
                    lambda r: r.get("summary", "Moving it."), source=src, destination=dst)


def _recycle(paths: List[str]) -> Reply:
    from modules.files.scan import human
    from modules.files.ops import item_size
    total = 0
    for p in paths:
        try:
            total += item_size(p)
        except OSError:
            pass
    what = os.path.basename(paths[0]) if len(paths) == 1 else f"{len(paths)} items"
    return run_tool("files.recycle", f"move {what} ({human(total)}) to the Recycle Bin",
                    lambda r: r.get("summary", "Done."), paths=paths)

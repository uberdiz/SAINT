"""
modules/link/files.py

Sending files between paired devices.

    sender                                    receiver
    file.offer {id, name, size, sha256} ──►   permission "files", size / disk checks
                                       ◄──    {accept}
    chunk frames (32 kB, in order) ─────►     written to a temp file
    file.done {id} ─────────────────────►     size + SHA-256 verified, then moved
                                       ◄──    {ok, name}                into the inbox

Received files land in ``data/link/inbox/<sender>/`` (or ``link.inbox_dir``)
and are never opened or run. Names are sanitised (no paths, no reserved
Windows names), nothing is overwritten, and from a collaborator anything that
could run when double-clicked gets ``.unsafe`` added to its name.
"""

import hashlib
import logging
import os
import re
import shutil
import threading
import time
import uuid
from typing import Callable, Dict, Optional

from modules.link.identity import COLLABORATOR
from modules.link.wire import CHUNK, LinkError

log = logging.getLogger("saint.link")

_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_RUNNABLE = {".exe", ".bat", ".cmd", ".com", ".scr", ".msi", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse",
             ".wsf", ".wsh", ".lnk", ".hta", ".jar", ".reg", ".dll", ".cpl", ".msc", ".pif", ".appx", ".msix"}


def sanitize_filename(name: str, fallback: str = "file") -> str:
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).strip(" .")
    if not name:
        return fallback
    stem, dot, ext = name.rpartition(".")
    base = stem if dot else name
    if base.lower() in _RESERVED:
        name = "_" + name
    if len(name) > 150:
        root, ext2 = os.path.splitext(name)
        name = root[:150 - len(ext2)] + ext2
    return name


def unique_path(folder: str, name: str) -> str:
    path = os.path.join(folder, name)
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(name)
    for i in range(2, 1000):
        candidate = os.path.join(folder, f"{root} ({i}){ext}")
        if not os.path.exists(candidate):
            return candidate
    return os.path.join(folder, f"{root} ({uuid.uuid4().hex[:6]}){ext}")


class _Incoming:
    def __init__(self, tid: bytes, peer_id: str, peer_name: str, role: str, name: str, size: int, sha: str, tmp: str):
        self.tid, self.peer_id, self.peer_name, self.role = tid, peer_id, peer_name, role
        self.name, self.size, self.sha, self.tmp = name, size, sha, tmp
        self.received = 0
        self.fh = open(tmp, "wb")
        self.hash = hashlib.sha256()
        self.started = time.time()
        self.error = ""
        self.lock = threading.Lock()

    def close(self):
        try:
            self.fh.close()
        except OSError:
            pass


class FileTransfers:
    def __init__(self, node, inbox_dir: Optional[Callable[[], str]] = None,
                 max_file_mb: Callable[[], float] = lambda: 1024):
        self.node = node
        self._inbox_dir = inbox_dir
        self._max_mb = max_file_mb
        self._incoming: Dict[bytes, _Incoming] = {}
        self._lock = threading.Lock()
        node.register("file.offer", self.handle_offer)
        node.register("file.done", self.handle_done)
        node.register("file.cancel", self.handle_cancel)

    # ------------------------------------------------------------------ #
    def inbox(self, peer_name: str = "") -> str:
        if self._inbox_dir is not None:
            base = self._inbox_dir()
        else:
            from core.paths import data_path
            base = str(data_path("link", "inbox"))
        folder = os.path.join(base, sanitize_filename(peer_name, "device")) if peer_name else base
        os.makedirs(folder, exist_ok=True)
        return folder

    # ------------------------------------------------------------------ #
    # Receiving
    # ------------------------------------------------------------------ #
    def handle_offer(self, ctx, data: dict) -> dict:
        name = sanitize_filename(str(data.get("name", "")))
        try:
            size = int(data.get("size", -1))
            tid = bytes.fromhex(str(data.get("id", "")))
        except (TypeError, ValueError):
            raise LinkError("bad file offer", "bad_request")
        sha = str(data.get("sha256", "")).lower()
        if size < 0 or len(tid) != 16 or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise LinkError("bad file offer", "bad_request")
        if size > self._max_mb() * 1024 * 1024:
            raise LinkError(f"That file is bigger than the {self._max_mb():.0f} MB limit.", "too_large")
        ctx.require("files", f"receive the file “{name}”")
        folder = self.inbox(ctx.peer.name)
        if size + 50 * 1024 * 1024 > shutil.disk_usage(folder).free:
            raise LinkError("There isn't enough free space here for that file.", "no_space")
        with self._lock:
            if len(self._incoming) >= 4:
                raise LinkError("Too many transfers at once; try again in a moment.", "busy")
            tmp = os.path.join(folder, f".{tid.hex()}.part")
            self._incoming[tid] = _Incoming(tid, ctx.peer.id, ctx.peer.name, ctx.peer.role, name, size, sha, tmp)
        log.info("link.file.offer peer=%s name=%r size=%d", ctx.peer.name, name, size)
        return {"accept": True}

    def on_chunk(self, session, tid: bytes, offset: int, data: bytes):
        with self._lock:
            inc = self._incoming.get(tid)
        if inc is None or inc.peer_id != session.peer.id:
            return                                     # not ours (or cancelled): drop it
        with inc.lock:
            if inc.error:
                return
            if offset != inc.received or inc.received + len(data) > inc.size:
                inc.error = "chunk out of order or too long"
                return
            inc.fh.write(data)
            inc.hash.update(data)
            inc.received += len(data)

    def handle_done(self, ctx, data: dict) -> dict:
        try:
            tid = bytes.fromhex(str(data.get("id", "")))
        except ValueError:
            raise LinkError("bad request", "bad_request")
        with self._lock:
            inc = self._incoming.pop(tid, None)
        if inc is None or inc.peer_id != ctx.peer.id:
            raise LinkError("I'm not receiving that file.", "unknown_transfer")
        inc.close()
        try:
            if inc.error or inc.received != inc.size or inc.hash.hexdigest() != inc.sha:
                raise LinkError(inc.error or "The file arrived damaged, so I threw it away.", "corrupt")
            name = inc.name
            if inc.role == COLLABORATOR and os.path.splitext(name)[1].lower() in _RUNNABLE:
                name += ".unsafe"
            final = unique_path(os.path.dirname(inc.tmp), name)
            os.replace(inc.tmp, final)
        except LinkError:
            self._discard(inc.tmp)
            raise
        except OSError as e:
            self._discard(inc.tmp)
            raise LinkError(f"I couldn't save it: {e}", "io")
        log.info("link.file.received peer=%s path=%s", inc.peer_name, final)
        self.node.emit("link.file", {"peer_id": inc.peer_id, "peer": inc.peer_name, "name": os.path.basename(final),
                                     "path": final, "size": inc.size, "unsafe": final.endswith(".unsafe")})
        return {"ok": True, "name": os.path.basename(final)}

    def handle_cancel(self, ctx, data: dict) -> dict:
        try:
            tid = bytes.fromhex(str(data.get("id", "")))
        except ValueError:
            return {}
        with self._lock:
            inc = self._incoming.pop(tid, None)
        if inc is not None and inc.peer_id == ctx.peer.id:
            inc.close()
            self._discard(inc.tmp)
        return {}

    @staticmethod
    def _discard(path: str):
        try:
            os.remove(path)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Sending
    # ------------------------------------------------------------------ #
    def send(self, peer_id: str, path: str, progress: Optional[Callable[[int, int], None]] = None,
             cancel: Optional[threading.Event] = None) -> dict:
        if not os.path.isfile(path):
            raise LinkError("I can't find that file.", "not_found")
        size = os.path.getsize(path)
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                sha.update(block)
        tid = uuid.uuid4().bytes
        name = os.path.basename(path)
        self.node.request(peer_id, "file.offer", {"id": tid.hex(), "name": name, "size": size,
                                                  "sha256": sha.hexdigest()}, timeout=90.0)
        session = self.node.session_for(peer_id)
        if session is None:
            raise LinkError("the device disconnected", "offline")
        sent = 0
        try:
            with open(path, "rb") as f:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise LinkError("cancelled", "cancelled")
                    block = f.read(CHUNK)
                    if not block:
                        break
                    session.channel.send_chunk(tid, sent, block)
                    sent += len(block)
                    if progress:
                        progress(sent, size)
            return self.node.request(peer_id, "file.done", {"id": tid.hex()}, timeout=60.0)
        except LinkError:
            try:
                session.notify("file.cancel", {"id": tid.hex()})
            except LinkError:
                pass
            raise

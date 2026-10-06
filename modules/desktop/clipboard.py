"""
modules/desktop/clipboard.py

The Windows clipboard as a hands-free input: "read my clipboard", "summarize
what I copied", "translate that into Spanish", "fix the code I copied"
(the fixed version goes back on the clipboard — say "paste").

Anything that looks like a password, key or token is never read aloud.
"""

import re
import time
from typing import Optional

from modules.automation.tools import P, PermissionLevel, Tool, ToolError

_SECRET_PATTERNS = re.compile(
    r"^(?:sk-|sk_live_|pk_live_|rk_|ghp_|gho_|ghs_|github_pat_|xox[abpr]-|AKIA|ASIA|AIza|ya29\.|eyJ[\w-]+\.[\w-]+\.)"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I)


def _open():
    import win32clipboard
    for _ in range(10):                      # another app may hold it for a moment
        try:
            win32clipboard.OpenClipboard()
            return win32clipboard
        except Exception:
            time.sleep(0.05)
    raise ToolError("Another app is using the clipboard — try again.", "BUSY")


def read_text() -> str:
    cb = _open()
    try:
        if cb.IsClipboardFormatAvailable(cb.CF_UNICODETEXT):
            return cb.GetClipboardData(cb.CF_UNICODETEXT) or ""
        if cb.IsClipboardFormatAvailable(cb.CF_HDROP):
            files = cb.GetClipboardData(cb.CF_HDROP) or ()
            return "\n".join(files)
        return ""
    finally:
        cb.CloseClipboard()


def read_plain_text():
    """The clipboard's text, or None when it holds something else (an image, files) or nothing."""
    cb = _open()
    try:
        if cb.IsClipboardFormatAvailable(cb.CF_UNICODETEXT):
            return cb.GetClipboardData(cb.CF_UNICODETEXT) or ""
        return None
    finally:
        cb.CloseClipboard()


def paste_text(text: str, send_paste, restore_after: float = 0.8):
    """Put ``text`` in the focused field through the clipboard (``send_paste`` presses
    ctrl+v), then put back the text that was on the clipboard before. Long text typed as
    keystrokes was garbled by Windows 11 Notepad ("pppp gggg", 2026-10-05) and was slow."""
    import threading
    try:
        saved = read_plain_text()
    except ToolError:
        saved = None
    write_text(text)
    time.sleep(0.03)
    send_paste()
    time.sleep(0.15)

    def restore():
        try:
            if saved is not None and read_plain_text() == text:     # you haven't copied anything since
                write_text(saved)
        except Exception:
            pass
    threading.Timer(restore_after, restore).start()


def write_text(text: str):
    cb = _open()
    try:
        cb.EmptyClipboard()
        cb.SetClipboardData(cb.CF_UNICODETEXT, text)
    finally:
        cb.CloseClipboard()


def looks_secret(text: str) -> bool:
    """A password / API key / token: one long unbroken mixed string, or a known key format."""
    t = (text or "").strip()
    if not t:
        return False
    if _SECRET_PATTERNS.search(t):
        return True
    if " " in t or "\n" in t or len(t) < 16 or len(t) > 200:
        return False
    classes = sum(bool(re.search(p, t)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^\w]"))
    return classes >= 3 and not re.match(r"^https?://", t) and not re.match(r"^[A-Za-z]:\\", t)


def clipboard_read():
    text = read_text()
    if not text.strip():
        return {"text": "", "empty": True}
    return {"text": text, "empty": False, "secret": looks_secret(text), "length": len(text)}


def clipboard_write(text: str):
    write_text(text)
    return {"written": len(text)}


def register_clipboard_tools(registry):
    tools = [
        # Not offered to the LLM: the clipboard may hold private text.
        Tool("clipboard.read", "Read the text on the clipboard", {}, PermissionLevel.LOW, clipboard_read,
             parameters={}, category="clipboard"),
        Tool("clipboard.write", "Put text on the clipboard", {"text": "string"}, PermissionLevel.MEDIUM,
             clipboard_write, parameters={"text": P("string")}, category="clipboard"),
    ]
    for t in tools:
        registry.register(t)


def spoken_preview(text: str, limit: int = 320) -> str:
    """The start of the clipboard, cut at a sentence when possible."""
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return (cut[:end + 1] if end > 80 else cut.rsplit(" ", 1)[0] + "…")


def strip_fences(text: str) -> str:
    m = re.search(r"```[\w+-]*\n(.*?)```", text or "", re.S)
    return (m.group(1) if m else text or "").strip("\n")


def get_text_or_none() -> Optional[str]:
    try:
        return read_text()
    except Exception:
        return None

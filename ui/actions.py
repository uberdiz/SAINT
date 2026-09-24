"""
ui/actions.py

What the UI can ask SAINT to do, in one place: Spotify controls, the mic,
typed requests, scenes. Plus ChatBinder, which keeps any ChatView in sync
with the conversation. Everything runs off the GUI thread.
"""

from datetime import datetime

from PySide6.QtCore import QObject

from core.config import config
from core.events import EventType
from ui.reactive import ui_bus
from ui.widgets import run_async

runtime = None          # set by MainWindow


def _module(name):
    from core.module_manager import module_manager
    return module_manager.get(name)


# ---------------------------------------------------------------------- #
# Spotify
# ---------------------------------------------------------------------- #
def spotify_status():
    sp = _module("spotify")
    if sp is None:
        return False, "Spotify module missing."
    return sp.availability()


def spotify(tool: str, on_error=None, **kwargs):
    from modules.automation.tools import get_tool_registry

    def done(res):
        if not res.success and on_error:
            on_error(res.error or "Spotify couldn't do that.")
    run_async(lambda: get_tool_registry().execute(tool, **kwargs), done,
              lambda e: on_error and on_error(str(e)))


def play_pause(on_error=None):
    spotify("spotify.pause" if ui_bus.spotify.get("is_playing") else "spotify.play", on_error)


def refresh_spotify(on_error=None):
    def go():
        if spotify_status()[0]:
            _module("spotify").tools.current()
    if not ui_bus.demo:
        run_async(go, None, on_error)


def fetch_queue(limit: int = 14):
    """Spotify's upcoming tracks as (track, artists) pairs. Blocking — run it
    with run_async. Raises RuntimeError with a readable reason when Spotify
    can't be asked."""
    ok, reason = spotify_status()
    if not ok:
        raise RuntimeError(reason)
    data = _module("spotify").tools.client.get_queue() or {}
    out = []
    for item in (data.get("queue") or [])[:limit]:
        if item:
            artists = ", ".join(a.get("name", "") for a in (item.get("artists") or [])[:2])
            out.append((item.get("name", "?"), artists))
    return out


def hotwords_on() -> bool:
    return bool(config.get("voice.music_hotwords", True))


# ---------------------------------------------------------------------- #
# Conversation / mic
# ---------------------------------------------------------------------- #
def submit(text: str) -> bool:
    ctrl = getattr(runtime, "controller", None)
    if not text.strip() or ctrl is None:
        return False
    ctrl.submit_text(text.strip())
    return True


def interrupt():
    ctrl = getattr(runtime, "controller", None)
    if ctrl:
        ctrl.interrupt()


def listening() -> bool:
    return bool(runtime and runtime.listening)


def toggle_listening(done=None):
    if runtime is None:
        return
    if runtime.listening:
        runtime.stop_listening()
        if done:
            done(True)
    else:
        run_async(runtime.start_listening, done, lambda e: done and done(False))


# ---------------------------------------------------------------------- #
# Scenes
# ---------------------------------------------------------------------- #
def run_scene(scene, on_done=None):
    from modules.automation.scenes import scenes
    scenes.run_in_background(scene, on_done=on_done)


# ---------------------------------------------------------------------- #
class ChatBinder(QObject):
    """Feeds conversation events into a ChatView."""

    def __init__(self, chat, parent=None):
        super().__init__(parent or chat)
        self.chat = chat
        self._turn = None
        self._meta = ""
        self._saved = []
        ui_bus.event.connect(self._on_event)
        ui_bus.demo_changed.connect(self._demo)

    def _demo(self, on: bool):
        """The demo's scripted conversation disappears when it ends."""
        if on:
            self._saved = [list(m) for m in self.chat._messages]
        else:
            self.chat._messages, self.chat._streaming, self._turn = self._saved, False, None
            self.chat._render()

    def _on_event(self, ev):
        t, p = ev.type, ev.payload or {}
        if t == EventType.CONVERSATION_TURN_START:
            self.chat.end_stream()
            prefix = "↩ " if p.get("is_interruption") else ""
            self.chat.add("user", p.get("text", ""), prefix + datetime.now().strftime("%H:%M"))
            self._turn = p.get("turn_id")
            self.chat.begin_stream()
        elif t == EventType.AI_STREAM_TOKEN:
            if p.get("turn_id") == self._turn:
                self.chat.stream(p.get("token", ""))
        elif t == EventType.AI_STREAM_DONE and p.get("tool_routed"):
            self._meta = "" if p.get("ok", True) else "couldn't finish"
        elif t == EventType.UI_CHAT_RENDER and p.get("role") == "assistant":
            if p.get("source"):
                src = p.get("source", "")
                self.chat.add("assistant", p.get("text", ""),
                              "reminder" if src.startswith(("automation", "reminder")) else "")
            elif p.get("turn_id") == self._turn:
                self.chat.end_stream(p.get("text", ""), self._meta)
                self._meta, self._turn = "", None
        elif t == EventType.CONVERSATION_INTERRUPTED and self._turn is not None:
            self.chat.end_stream(meta="interrupted")
            self._turn = None

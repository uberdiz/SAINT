"""
ui/components/agent_status.py

The strip along the bottom of the window: SAINT's state and what it's listening for, the last
thing you said and what SAINT answered, a box to type to SAINT, the microphone, and Stop.

    ● Ready · Say “Hey SAINT”        You: run the tests  →  SAINT: Running the tests.     [Ask SAINT…] 🎤 ■

Voice stays the primary way in; this is the same conversation, visible.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout

from core.config import config
from core.events import EventType
from ui import actions
from ui.design import tokens
from ui.reactive import ui_bus
from ui.theme import current_palette, state_color, state_word
from ui.widgets import ElidedLabel, IconButton, Orb


def listening_hint(state: str, label: str = "") -> str:
    """What SAINT is listening for, in plain words."""
    if state == "offline":
        return "Turn the microphone on to talk to SAINT — or type here"
    if state in ("command_listening", "listening", "wake_detected"):
        return "Listening — go ahead"
    if state == "speaking":
        return "Talk over me to interrupt"
    if state in ("processing", "observing", "executing"):
        return "Working — say “stop” to cancel"
    if "reply" in (label or "").lower() or "answer" in (label or "").lower():
        return "Waiting for your answer — no wake word needed"
    if config.get("voice.wake_word_enabled", True):
        hot = " · while music plays, just say “skip”" if actions.hotwords_on() else ""
        return f"Say “Hey SAINT”{hot}"
    return "Listening for any clear speech"


class AgentStatusBar(QFrame):
    def __init__(self, shell=None, parent=None):
        super().__init__(parent)
        self.shell = shell
        self.setObjectName("AgentBar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(tokens.SPACE_LG, tokens.SPACE_SM, tokens.SPACE_MD, tokens.SPACE_SM)
        lay.setSpacing(tokens.SPACE_MD)
        self.orb = Orb(26)
        lay.addWidget(self.orb, 0, Qt.AlignVCenter)
        state_col = QVBoxLayout()
        state_col.setSpacing(0)
        self.word = QLabel("Starting")
        self.word.setStyleSheet("font-weight: 600;")
        self.hint = ElidedLabel("")
        self.hint.setObjectName("Faint")
        self.hint.setMinimumWidth(120)
        state_col.addWidget(self.word)
        state_col.addWidget(self.hint)
        lay.addLayout(state_col, 2)
        convo = QVBoxLayout()
        convo.setSpacing(0)
        self.you = ElidedLabel("")
        self.you.setObjectName("Muted")
        self.saint = ElidedLabel("")
        convo.addWidget(self.you)
        convo.addWidget(self.saint)
        lay.addLayout(convo, 4)
        self.ask = QLineEdit()
        self.ask.setObjectName("AskInput")
        self.ask.setPlaceholderText("Ask SAINT…")
        self.ask.setMinimumWidth(180)
        self.ask.setMaximumWidth(360)
        self.ask.returnPressed.connect(self._send)
        lay.addWidget(self.ask, 3)
        self.mic = IconButton("mic", "Microphone on / off", 17)
        self.mic.clicked.connect(lambda: actions.toggle_listening(lambda _ok: self.sync_mic()))
        lay.addWidget(self.mic)
        self.stop = IconButton("stop", "Stop speaking and stop what SAINT is doing", 15)
        self.stop.clicked.connect(self._stop)
        lay.addWidget(self.stop)
        ui_bus.event.connect(self._on_event)
        self.render_state(ui_bus.state)
        self.sync_mic()

    # ------------------------------------------------------------------ #
    def _send(self):
        text = self.ask.text().strip()
        if not text:
            return
        self.ask.clear()
        if not actions.submit(text):
            self.saint.setText("SAINT is still starting — try again in a moment.")

    def _stop(self):
        actions.interrupt()
        actions.task_control("stop")

    def sync_mic(self):
        on = actions.listening()
        self.mic.set_icon("mic" if on else "mic-off")
        self.mic.setToolTip("Stop listening" if on else "Start listening")

    def render_state(self, s: dict):
        state = s.get("state", "offline")
        color = state_color(state, current_palette())
        self.orb.set_state(state)
        self.word.setText(state_word(state))
        self.word.setStyleSheet(f"font-weight: 600; color: {color};")
        detail = s.get("detail") or ""
        hint = listening_hint(state, s.get("label", ""))
        self.hint.setText(f"{detail} · {hint}" if detail and state in ("processing", "executing", "observing")
                          else hint)

    def _on_event(self, ev):
        t, p = ev.type, ev.payload or {}
        if t == EventType.ASSISTANT_STATE:
            self.render_state(p)
        elif t == EventType.VOICE_AUDIO_LEVEL:
            self.orb.set_level(p.get("level", 0.0))
        elif t == EventType.CONVERSATION_TURN_START:
            self.you.setText(f"You: {p.get('text', '')}")
            self.saint.setText("")
        elif t == EventType.UI_CHAT_RENDER and p.get("role") == "assistant":
            self.saint.setText(f"SAINT: {p.get('text', '')}")
        elif t in (EventType.VOICE_LISTENING_START, EventType.VOICE_LISTENING_STOP):
            self.sync_mic()
        elif t == EventType.VOICE_HOTWORD:
            self.orb.flash()

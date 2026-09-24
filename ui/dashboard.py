"""
ui/dashboard.py

Home screen. Everything shown here is driven by events from the core runtime
(AssistantState, wake-word status/score, tool executions, Spotify playback,
scheduler changes) — the dashboard never guesses what SAINT is doing.
"""

import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QProgressBar, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget, QFrame, QScrollArea,
)

from core.assistant_state import assistant_state
from core.config import config
from core.events import event_bus, EventType
from core.module_manager import module_manager
from core.state import app_state
from ui.theme import current_palette, state_color
from ui.widgets import Card, ChatView, LevelMeter, StateOrb, run_async


def _tool_label(tool) -> str:
    """Human description of a tool for the activity feed — never its internal id."""
    try:
        from modules.automation.tools import get_tool_registry
        t = next((x for x in get_tool_registry().list_tools() if x.name == tool), None)
        if t is not None and t.description:
            import re
            d = re.sub(r"\s*\([^)]*\)", "", t.description)
            return re.split(r"[:,;]| — ", d)[0].strip()[:60]
    except Exception:
        pass
    from core.assistant_state import tool_activity
    return tool_activity(tool)


class Dashboard(QWidget):
    def __init__(self, runtime=None):
        super().__init__()
        self.runtime = runtime
        self._voice = module_manager.get("voice")
        self._stream_turn = None
        self._spotify_state = {}
        self._spotify_seen_at = 0.0
        self._build()
        event_bus.event_occurred.connect(self._on_event)

        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(1000)
        self._tick()
        QTimer.singleShot(300, self._initial_sync)

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(14)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        self._title = QLabel("SAINT")
        self._title.setObjectName("PageTitle")
        self._greeting = QLabel("")
        self._greeting.setObjectName("Muted")
        title_col.addWidget(self._title)
        title_col.addWidget(self._greeting)
        header.addLayout(title_col)
        header.addStretch()
        self._listen_btn = QPushButton("Start listening")
        self._listen_btn.setObjectName("Primary")
        self._listen_btn.clicked.connect(self._toggle_listening)
        self._stop_btn = QPushButton("Stop speaking")
        self._stop_btn.clicked.connect(self._interrupt)
        header.addWidget(self._stop_btn)
        header.addWidget(self._listen_btn)
        root.addLayout(header)

        # Error banner (real subsystem failures only)
        self._banner = QFrame()
        self._banner.setObjectName("Banner")
        bl = QHBoxLayout(self._banner)
        bl.setContentsMargins(12, 8, 8, 8)
        self._banner_text = QLabel("")
        self._banner_text.setWordWrap(True)
        close = QPushButton("Dismiss")
        close.setObjectName("Ghost")
        close.clicked.connect(lambda: self._banner.setVisible(False))
        bl.addWidget(self._banner_text, 1)
        bl.addWidget(close)
        self._banner.setVisible(False)
        root.addWidget(self._banner)

        body = QHBoxLayout()
        body.setSpacing(14)
        root.addLayout(body, 1)

        # ---- left column ------------------------------------------------
        left = QVBoxLayout()
        left.setSpacing(14)
        body.addLayout(left, 3)

        hero = Card()
        hl = QHBoxLayout()
        hl.setSpacing(18)
        self._orb = StateOrb()
        hl.addWidget(self._orb, 0, Qt.AlignVCenter)
        info = QVBoxLayout()
        info.setSpacing(4)
        self._state_label = QLabel("Starting…")
        self._state_label.setObjectName("StateLabel")
        self._detail_label = QLabel("")
        self._detail_label.setObjectName("Muted")
        self._detail_label.setWordWrap(True)
        self._wake_label = QLabel("")
        self._wake_label.setWordWrap(True)
        info.addWidget(self._state_label)
        info.addWidget(self._detail_label)
        info.addSpacing(6)
        info.addWidget(self._wake_label)
        meters = QHBoxLayout()
        wcol, mcol = QVBoxLayout(), QVBoxLayout()
        wl = QLabel("Wake score")
        wl.setObjectName("Faint")
        self._wake_meter = LevelMeter(show_threshold=True)
        wcol.addWidget(wl)
        wcol.addWidget(self._wake_meter)
        ml = QLabel("Microphone")
        ml.setObjectName("Faint")
        self._mic_meter = LevelMeter()
        mcol.addWidget(ml)
        mcol.addWidget(self._mic_meter)
        meters.addLayout(wcol)
        meters.addSpacing(12)
        meters.addLayout(mcol)
        info.addLayout(meters)
        hl.addLayout(info, 1)
        hero.body.addLayout(hl)
        left.addWidget(hero)

        self._chat_card = Card("Conversation")
        clear = QPushButton("Clear")
        clear.setObjectName("Ghost")
        clear.clicked.connect(lambda: self._chat.clear_chat())
        self._chat_card.header.addWidget(clear)
        self._chat = ChatView()
        self._chat.setMinimumHeight(220)
        self._chat_card.body.addWidget(self._chat, 1)
        row = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or question — e.g. “remind me in 10 minutes to stretch”")
        self._input.returnPressed.connect(self._send)
        send = QPushButton("Send")
        send.setObjectName("Primary")
        send.clicked.connect(self._send)
        row.addWidget(self._input, 1)
        row.addWidget(send)
        self._chat_card.body.addLayout(row)
        left.addWidget(self._chat_card, 1)

        # ---- right column -------------------------------------------------
        right_wrap = QWidget()
        right = QVBoxLayout(right_wrap)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(14)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(right_wrap)
        scroll.setMinimumWidth(320)
        scroll.setMaximumWidth(420)
        body.addWidget(scroll, 2)

        # Spotify
        self._sp_card = Card("Now playing")
        self._sp_title = QLabel("—")
        self._sp_title.setObjectName("SectionTitle")
        self._sp_title.setWordWrap(True)
        self._sp_artist = QLabel("")
        self._sp_artist.setObjectName("Muted")
        self._sp_progress = QProgressBar()
        self._sp_progress.setTextVisible(False)
        self._sp_meta = QLabel("")
        self._sp_meta.setObjectName("Faint")
        ctl = QHBoxLayout()
        self._sp_prev = QPushButton("◀◀")
        self._sp_play = QPushButton("▶ ❚❚")
        self._sp_next = QPushButton("▶▶")
        for b, tool in ((self._sp_prev, "spotify.previous"), (self._sp_play, None), (self._sp_next, "spotify.next")):
            b.setFixedWidth(52)
            b.clicked.connect(lambda _=False, t=tool: self._spotify_action(t))
            ctl.addWidget(b)
        ctl.addStretch()
        for w in (self._sp_title, self._sp_artist, self._sp_progress, self._sp_meta):
            self._sp_card.body.addWidget(w)
        self._sp_card.body.addLayout(ctl)
        right.addWidget(self._sp_card)

        # Upcoming automations
        self._auto_card = Card("Upcoming")
        self._auto_list = QListWidget()
        self._auto_list.setMaximumHeight(150)
        self._auto_list.setWordWrap(True)
        self._auto_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._auto_card.body.addWidget(self._auto_list)
        right.addWidget(self._auto_card)

        # Activity
        self._act_card = Card("Activity")
        self._activity = QListWidget()
        self._activity.setMinimumHeight(160)
        self._activity.setWordWrap(True)
        self._activity.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._act_card.body.addWidget(self._activity)
        right.addWidget(self._act_card, 1)

        # System
        self._sys_card = Card("System")
        self._sys_label = QLabel("")
        self._sys_label.setObjectName("Muted")
        self._sys_label.setWordWrap(True)
        self._sys_card.body.addWidget(self._sys_label)
        right.addWidget(self._sys_card)
        right.addStretch()

        self.apply_settings()

    def apply_settings(self):
        panels = config.get("dashboard.panels", {}) or {}
        self._chat_card.setVisible(panels.get("conversation", True))
        self._sp_card.setVisible(panels.get("spotify", True))
        self._auto_card.setVisible(panels.get("automations", True))
        self._act_card.setVisible(panels.get("activity", True))
        self._sys_card.setVisible(panels.get("system", True))
        self._wake_meter.set_threshold(config.get("voice.wake_word_threshold", 0.5))
        self._chat._render()      # re-colour bubbles for a new theme/accent
        self._render_state(assistant_state.snapshot())

    # ------------------------------------------------------------------ #
    # Sync
    # ------------------------------------------------------------------ #
    def _initial_sync(self):
        self._render_state(assistant_state.snapshot())
        self._render_wake(self._voice.wake_status())
        self._refresh_listen_button()
        self._refresh_automations()
        self._refresh_spotify_card()

    def _tick(self):
        hour = datetime.now().hour
        part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        self._greeting.setText(f"Good {part} — {datetime.now().strftime('%A, %B %d · %I:%M %p').replace(' 0', ' ')}")
        ai_model = config.get("ai.model", "")
        voice = self._voice.diagnostics
        wake = voice.get("wake", {})
        tts_state = getattr(getattr(self.runtime, "tts", None), "state", None)
        self._sys_label.setText(
            f"CPU {app_state.cpu_percent():.0f}% · RAM {app_state.ram_mb():.0f} MB · Up {app_state.uptime_formatted()}\n"
            f"LLM: {config.get('ai.provider')} / {ai_model}\n"
            f"STT: {voice.get('stt_device') or '—'}  ·  TTS: {getattr(tts_state, 'name', '—')}\n"
            f"Wake model: {wake.get('avg_infer_ms', '—')} ms/frame · barge-ins {voice.get('barge_ins', 0)}")
        # Interpolate Spotify progress between polls.
        st = self._spotify_state
        if st.get("is_playing") and st.get("duration_ms"):
            prog = st.get("progress_ms", 0) + (time.time() - self._spotify_seen_at) * 1000
            self._sp_progress.setValue(int(min(1.0, prog / st["duration_ms"]) * 1000))

    def _refresh_listen_button(self):
        on = self._voice.voice_active
        self._listen_btn.setText("Stop listening" if on else "Start listening")

    # ------------------------------------------------------------------ #
    # Events (GUI thread)
    # ------------------------------------------------------------------ #
    def _on_event(self, ev):
        t, p = ev.type, ev.payload
        if t == EventType.ASSISTANT_STATE:
            self._render_state(p)
        elif t == EventType.VOICE_AUDIO_LEVEL:
            lvl = p.get("level", 0.0)
            self._mic_meter.set_value(lvl)
            self._orb.set_level(lvl)
        elif t == EventType.VOICE_WAKE_SCORE:
            self._wake_meter.set_value(p.get("score", 0.0))
        elif t in (EventType.WAKE_STATUS, EventType.WAKE_ERROR):
            self._render_wake(self._voice.wake_status())
        elif t == EventType.VOICE_WAKE_WORD:
            self._add_activity(f"Wake word heard (score {p.get('score', 0):.2f})", "ok")
        elif t == EventType.VOICE_COMMAND_TIMEOUT:
            self._add_activity("No command after wake word — back to listening", "muted")
        elif t in (EventType.VOICE_LISTENING_START, EventType.VOICE_LISTENING_STOP):
            self._refresh_listen_button()
        elif t == EventType.CONVERSATION_TURN_START:
            self._chat.end_stream()
            prefix = "↩ " if p.get("is_interruption") else ""
            self._chat.add("user", p.get("text", ""), prefix + datetime.now().strftime("%H:%M"))
            self._stream_turn = p.get("turn_id")
            self._chat.begin_stream()
        elif t == EventType.AI_STREAM_TOKEN:
            if p.get("turn_id") == self._stream_turn:
                self._chat.stream(p.get("token", ""))
        elif t == EventType.AI_STREAM_DONE and p.get("tool_routed"):
            self._pending_meta = "" if p.get("ok", True) else "couldn't finish"
        elif t == EventType.UI_CHAT_RENDER and p.get("role") == "assistant":
            if p.get("source"):
                src = p.get("source", "")
                self._chat.add("assistant", p.get("text", ""),
                               "reminder" if src.startswith(("automation", "reminder")) else "")
            elif p.get("turn_id") == self._stream_turn:
                meta = getattr(self, "_pending_meta", "") or ""
                self._pending_meta = ""
                self._chat.end_stream(p.get("text", ""), meta)
                self._stream_turn = None
        elif t == EventType.CONVERSATION_INTERRUPTED:
            if self._stream_turn is not None:
                self._chat.end_stream(meta="interrupted")
                self._stream_turn = None
            self._add_activity("Interrupted", "muted")
        elif t == EventType.TOOL_STARTED:
            self._add_activity(f"Running · {_tool_label(p.get('tool'))}…", "info")
        elif t == EventType.TOOL_COMPLETED:
            self._add_activity(f"✓ {_tool_label(p.get('tool'))} · {p.get('duration_ms', 0):.0f} ms", "ok")
        elif t == EventType.TOOL_FAILED:
            self._add_activity(f"✗ {_tool_label(p.get('tool'))} failed: {p.get('error', '')[:80]}", "error")
        elif t == EventType.AGENT_CONFIRM_REQUIRED:
            self._add_activity(f"Waiting for your OK: {p.get('description')}", "warn")
        elif t == EventType.SPOTIFY_PLAYBACK_CHANGED:
            self._render_spotify(p)
        elif t == EventType.SPOTIFY_ERROR:
            self._sp_meta.setText(p.get("error", "Spotify error"))
        elif t in (EventType.SPOTIFY_CONNECTED, EventType.SPOTIFY_DISCONNECTED):
            self._refresh_spotify_card()
        elif t in (EventType.AUTOMATION_CREATED, EventType.AUTOMATION_UPDATED, EventType.AUTOMATION_CANCELLED):
            if t == EventType.AUTOMATION_CREATED:
                self._add_activity(f"Scheduled: {p.get('title')} — {p.get('when')}", "ok")
            elif t == EventType.AUTOMATION_CANCELLED:
                self._add_activity(f"Cancelled: {p.get('title')}", "muted")
            self._refresh_automations()
        elif t == EventType.AUTOMATION_TRIGGERED:
            self._add_activity(f"⏰ {p.get('title')}", "ok")
        elif t == EventType.AUTOMATION_FAILED:
            self._add_activity(f"Automation failed: {p.get('title')} — {p.get('error')}", "error")
        elif t == EventType.MEMORY_STORED:
            self._add_activity(f"Remembered: {p.get('key') or 'a new fact'}", "ok")
        elif t == EventType.ERROR:
            msg = p.get("error") or p.get("message") or "Unknown error"
            self._show_banner(msg)
            self._add_activity(msg, "error")
        elif t == EventType.TTS_ERROR:
            self._show_banner(f"Speech output problem: {p.get('error', '')[:160]}")
        elif t == EventType.AI_ERROR:
            self._add_activity(f"AI error: {str(p.get('error', ''))[:120]}", "error")

    # ------------------------------------------------------------------ #
    def _render_state(self, s):
        state = s.get("state", "offline")
        pal = current_palette()
        self._orb.set_state(state)
        self._state_label.setText(s.get("label", state))
        self._state_label.setStyleSheet(f"color: {state_color(state, pal)};")
        self._detail_label.setText(s.get("detail", "") or "")
        self._detail_label.setVisible(bool(s.get("detail")))
        self._stop_btn.setEnabled(state in ("speaking", "processing", "executing", "observing"))

    def _render_wake(self, w):
        pal = current_palette()
        if not w.get("enabled"):
            self._wake_label.setText("Wake word off — SAINT reacts to all clear speech.")
            self._wake_label.setStyleSheet(f"color:{pal.muted};")
        elif w.get("ready"):
            self._wake_label.setText(f"Say “Hey SAINT” · model {w.get('model_path', '')} · threshold "
                                     f"{w.get('threshold', 0.5):.2f}")
            self._wake_label.setStyleSheet(f"color:{pal.muted};")
        else:
            self._wake_label.setText(f"Wake word unavailable: {w.get('error', 'unknown error')}")
            self._wake_label.setStyleSheet(f"color:{pal.danger};")
            if w.get("code") not in ("NOT_LOADED",):
                self._show_banner(f"Wake word unavailable: {w.get('error')}")

    def _show_banner(self, text):
        self._banner_text.setText(text)
        self._banner.setVisible(True)

    def _add_activity(self, text, kind="info"):
        pal = current_palette()
        color = {"ok": pal.success, "error": pal.danger, "warn": pal.warning, "muted": pal.faint}.get(kind, pal.text)
        item = QListWidgetItem(f"{datetime.now().strftime('%H:%M:%S')}  {text}")
        item.setForeground(__import__("PySide6.QtGui", fromlist=["QColor"]).QColor(color))
        self._activity.insertItem(0, item)
        while self._activity.count() > 60:
            self._activity.takeItem(self._activity.count() - 1)

    # ------------------------------------------------------------------ #
    # Spotify card
    # ------------------------------------------------------------------ #
    def _refresh_spotify_card(self):
        sp = module_manager.get("spotify")
        ok, reason = sp.availability() if sp else (False, "Spotify module missing")
        for b in (self._sp_prev, self._sp_play, self._sp_next):
            b.setEnabled(ok)
        if not ok:
            self._sp_title.setText("Spotify not available")
            self._sp_artist.setText(reason)
            self._sp_meta.setText("")
            self._sp_progress.setValue(0)
            return
        run_async(lambda: sp.tools.current(), None, lambda e: self._sp_meta.setText(str(e)[:120]))

    def _render_spotify(self, st):
        self._spotify_state = st
        self._spotify_seen_at = time.time()
        if not st.get("track"):
            self._sp_title.setText("Nothing playing")
            self._sp_artist.setText(st.get("device") or "")
            self._sp_progress.setValue(0)
            self._sp_meta.setText("")
            return
        self._sp_title.setText(st["track"])
        self._sp_artist.setText(f"{st.get('artists', '')} — {st.get('album', '')}")
        dur = st.get("duration_ms") or 1
        self._sp_progress.setRange(0, 1000)
        self._sp_progress.setValue(int(1000 * st.get("progress_ms", 0) / dur))
        vol = st.get("volume")
        self._sp_meta.setText(("▶ Playing" if st.get("is_playing") else "⏸ Paused")
                              + (f" on {st['device']}" if st.get("device") else "")
                              + (f" · volume {vol}%" if vol is not None else ""))

    def _spotify_action(self, tool):
        from modules.automation.tools import get_tool_registry
        if tool is None:
            tool = "spotify.pause" if self._spotify_state.get("is_playing") else "spotify.play"

        def done(res):
            if not res.success:
                self._sp_meta.setText(res.error or "Spotify action failed")
        run_async(lambda: get_tool_registry().execute(tool), done)

    # ------------------------------------------------------------------ #
    # Automations card
    # ------------------------------------------------------------------ #
    def _refresh_automations(self):
        from modules.automation.scheduler import scheduler

        def show(items):
            self._auto_list.clear()
            active = [a for a in items if a.status == "active"][:6]
            if not active:
                self._auto_list.addItem("No reminders or automations scheduled.")
            for a in active:
                when = datetime.fromtimestamp(a.next_run).strftime("%a %H:%M") if a.next_run else "—"
                self._auto_list.addItem(f"{when}  ·  {a.title}" + ("  ↻" if a.recurring else ""))
        run_async(lambda: scheduler.list(active_only=True), show)

    # ------------------------------------------------------------------ #
    # Controls
    # ------------------------------------------------------------------ #
    def _toggle_listening(self):
        if self.runtime is None:
            return
        if self._voice.voice_active:
            self.runtime.stop_listening()
            self._refresh_listen_button()
        else:
            self._listen_btn.setEnabled(False)
            self._listen_btn.setText("Starting…")

            def done(ok):
                self._listen_btn.setEnabled(True)
                self._refresh_listen_button()
                if not ok:
                    self._show_banner(self._voice.diagnostics.get("mic_error") or "Couldn't start the microphone.")
            run_async(self.runtime.start_listening, done, lambda e: done(False))

    def _interrupt(self):
        ctrl = getattr(self.runtime, "controller", None)
        if ctrl:
            ctrl.interrupt()

    def _send(self):
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        ctrl = getattr(self.runtime, "controller", None)
        if ctrl is None:
            self._chat.add("system", "SAINT is still starting up — try again in a moment.")
            return
        ctrl.submit_text(text)

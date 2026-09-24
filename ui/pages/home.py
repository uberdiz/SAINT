"""
ui/pages/home.py

The stage. Everything here is driven by real events: the orb and headline
follow the assistant state, tools slide into the Live feed as they run,
Spotify and schedules update themselves.
"""

import re
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
                               QWidget)

from core.config import config
from core.events import EventType
from core.state import app_state
from ui import actions, icons, motion
from ui.pages.music import NowPlaying
from ui.reactive import ui_bus
from ui.theme import current_palette, state_color, state_word
from ui.widgets import (Card, ChatView, ElidedLabel, IconButton, LevelMeter, Orb, Page, StatusDot, chip,
                        clear_layout, run_async, set_chip)


def tool_label(tool: str) -> str:
    """Human description of a tool — never its internal id."""
    try:
        from modules.automation.tools import get_tool_registry
        t = get_tool_registry().get(tool)
        if t is not None and t.description:
            d = re.sub(r"\s*\([^)]*\)", "", t.description)
            return re.split(r"[:,;]| — ", d)[0].strip()[:56]
    except Exception:
        pass
    from core.assistant_state import tool_activity
    return tool_activity(tool)


class ToolRow(QFrame):
    def __init__(self, tool: str):
        super().__init__()
        self.tool = tool
        self.running = True
        self.demo = ui_bus.demo
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(10)
        self.dot = StatusDot(size=8)
        lay.addWidget(self.dot)
        name = ElidedLabel(tool_label(tool))
        lay.addWidget(name, 1)
        self.badge = chip("running", "accent")
        lay.addWidget(self.badge)
        self.blink(True)

    def blink(self, on: bool):
        p = current_palette()
        if self.running:
            self.dot.set_color(p.accent if on else p.border_strong)

    def done(self, ok: bool, ms: float, error: str = ""):
        self.running = False
        p = current_palette()
        self.dot.set_color(p.success if ok else p.danger)
        set_chip(self.badge, f"{ms:.0f} ms" if ok else "failed", "" if ok else "err")
        if error:
            self.setToolTip(error)


def drop_demo_rows(rows: list, empty_label):
    for r in [r for r in rows if r.demo]:
        rows.remove(r)
        r.deleteLater()
    empty_label.setVisible(not rows)


class HomePage(Page):
    def __init__(self, shell):
        super().__init__("Good evening", "")
        self.shell = shell
        from core.module_manager import module_manager
        self._voice = module_manager.get("voice")

        self.overlay_btn = QPushButton(" Overlay")
        self.overlay_btn.setToolTip(f"Open the SAINT overlay ({config.get('overlay.hotkey', '')})")
        self.overlay_btn.clicked.connect(shell.toggle_overlay)
        self.stop_btn = QPushButton("Stop speaking")
        self.stop_btn.clicked.connect(actions.interrupt)
        self.listen_btn = QPushButton("Start listening")
        self.listen_btn.setObjectName("Primary")
        self.listen_btn.clicked.connect(self._toggle_listening)
        for b in (self.overlay_btn, self.stop_btn, self.listen_btn):
            self.actions.addWidget(b)

        self.banner = QFrame()
        self.banner.setObjectName("Banner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(14, 10, 8, 10)
        self.banner_icon = QLabel()
        self.banner_text = QLabel("")
        self.banner_text.setWordWrap(True)
        dismiss = QPushButton("Dismiss")
        dismiss.setObjectName("Ghost")
        dismiss.clicked.connect(self.banner.hide)
        bl.addWidget(self.banner_icon)
        bl.addWidget(self.banner_text, 1)
        bl.addWidget(dismiss)
        self.banner.hide()
        self.root.addWidget(self.banner)

        body = QHBoxLayout()
        body.setSpacing(16)
        self.root.addLayout(body, 1)
        left = QVBoxLayout()
        left.setSpacing(16)
        body.addLayout(left, 3)

        # ---- hero ---------------------------------------------------------
        hero = Card()
        hl = QHBoxLayout()
        hl.setSpacing(26)
        self.orb = Orb(156)
        hl.addWidget(self.orb, 0, Qt.AlignVCenter)
        info = QVBoxLayout()
        info.setSpacing(6)
        self.state_chip = chip("Starting", "accent")
        chips = QHBoxLayout()
        chips.addWidget(self.state_chip)
        chips.addStretch()
        info.addStretch()
        info.addLayout(chips)
        self.state_label = QLabel("Starting…")
        self.state_label.setObjectName("StateLabel")
        self.detail = QLabel("")
        self.detail.setObjectName("Muted")
        self.detail.setWordWrap(True)
        self.wake_line = QLabel("")
        self.wake_line.setObjectName("Faint")
        self.wake_line.setWordWrap(True)
        info.addWidget(self.state_label)
        info.addWidget(self.detail)
        info.addWidget(self.wake_line)
        meters = QGridLayout()
        meters.setHorizontalSpacing(16)
        meters.setVerticalSpacing(4)
        for c, text in enumerate(("Wake score", "Microphone")):
            lab = QLabel(text)
            lab.setObjectName("Faint")
            meters.addWidget(lab, 0, c)
        self.wake_meter = LevelMeter(show_threshold=True)
        self.mic_meter = LevelMeter()
        meters.addWidget(self.wake_meter, 1, 0)
        meters.addWidget(self.mic_meter, 1, 1)
        info.addSpacing(6)
        info.addLayout(meters)
        info.addStretch()
        hl.addLayout(info, 1)
        hero.body.addLayout(hl)
        left.addWidget(hero)

        # ---- conversation ------------------------------------------------
        chat_card = Card("Conversation")
        clear = QPushButton("Clear")
        clear.setObjectName("Ghost")
        clear.clicked.connect(lambda: self.chat.clear_chat())
        chat_card.header.addWidget(clear)
        self.chat = ChatView()
        self.chat.setMinimumHeight(200)
        chat_card.body.addWidget(self.chat, 1)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.input = QLineEdit()
        self.input.setObjectName("BigInput")
        self.input.setPlaceholderText("Ask or tell SAINT anything — “remind me in 10 minutes to stretch”")
        self.input.returnPressed.connect(self._send)
        send = IconButton("send", "Send", 18)
        send.clicked.connect(self._send)
        row.addWidget(self.input, 1)
        row.addWidget(send)
        chat_card.body.addLayout(row)
        left.addWidget(chat_card, 1)
        actions.ChatBinder(self.chat, self)

        # ---- right column ------------------------------------------------
        right_w = QWidget()
        right_w.setMaximumWidth(400)
        right_w.setMinimumWidth(300)
        right = QVBoxLayout(right_w)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(16)
        body.addWidget(right_w, 2)

        np_card = Card("Now playing")
        self.now_playing = NowPlaying(cover=76)
        np_card.body.addWidget(self.now_playing)
        right.addWidget(np_card)

        live = Card("Live")
        self.live_list = QVBoxLayout()
        self.live_list.setSpacing(0)
        self.live_empty = QLabel("Tools SAINT runs appear here as they happen.")
        self.live_empty.setObjectName("Faint")
        self.live_empty.setWordWrap(True)
        live.body.addWidget(self.live_empty)
        live.body.addLayout(self.live_list)
        right.addWidget(live)
        self._rows = []

        nxt = Card("Up next")
        self.next_list = QVBoxLayout()
        self.next_list.setSpacing(6)
        nxt.body.addLayout(self.next_list)
        self.scene_row = QHBoxLayout()
        self.scene_row.setSpacing(6)
        nxt.body.addLayout(self.scene_row)
        right.addWidget(nxt)

        sysc = Card("System")
        self.sys_label = QLabel("")
        self.sys_label.setObjectName("Muted")
        self.sys_label.setWordWrap(True)
        sysc.body.addWidget(self.sys_label)
        right.addWidget(sysc)
        right.addStretch()

        ui_bus.event.connect(self._on_event)
        ui_bus.demo_changed.connect(lambda on: None if on else drop_demo_rows(self._rows, self.live_empty))
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(1000)
        self._blink = False
        self._blinker = QTimer(self)
        self._blinker.timeout.connect(self._blink_rows)
        self._blinker.start(420)
        self._tick()
        QTimer.singleShot(300, self.sync)

    # ------------------------------------------------------------------ #
    def apply_theme(self):
        p = current_palette()
        self.overlay_btn.setIcon(icons.icon("overlay", p.text, 16))
        self.banner_icon.setPixmap(icons.pixmap("alert", p.danger, 18))
        self.wake_meter.set_threshold(config.get("voice.wake_word_threshold", 0.5))
        self.overlay_btn.setToolTip(f"Open the SAINT overlay ({config.get('overlay.hotkey', '')})")
        self.chat._render()
        self.render_state(ui_bus.state)

    def sync(self):
        self.render_state(ui_bus.state)
        self._render_wake()
        self._refresh_listen()
        self.refresh_next()
        actions.refresh_spotify()
        if self.chat.is_empty():
            self.chat.add("system", "Say “Hey SAINT” — or type below.")

    def _tick(self):
        now = datetime.now()
        part = "morning" if now.hour < 12 else "afternoon" if now.hour < 18 else "evening"
        self.title.setText(f"Good {part}")
        self.subtitle.setText(now.strftime("%A, %B %d · %I:%M %p").replace(" 0", " "))
        self.subtitle.show()
        diag = self._voice.diagnostics if self._voice else {}
        tts = getattr(getattr(actions.runtime, "tts", None), "state", None)
        self.sys_label.setText(
            f"CPU {app_state.cpu_percent():.0f}%  ·  RAM {app_state.ram_mb():.0f} MB  ·  up {app_state.uptime_formatted()}\n"
            f"{config.get('ai.provider')} / {config.get('ai.model', '')}\n"
            f"STT {diag.get('stt_device') or '—'}  ·  TTS {getattr(tts, 'name', '—')}")

    def _blink_rows(self):
        self._blink = not self._blink
        for r in self._rows:
            r.blink(self._blink)

    # ------------------------------------------------------------------ #
    def _on_event(self, ev):
        t, p = ev.type, ev.payload or {}
        if t == EventType.ASSISTANT_STATE:
            self.render_state(p)
        elif t == EventType.VOICE_AUDIO_LEVEL:
            lvl = p.get("level", 0.0)
            self.mic_meter.set_value(lvl)
            self.orb.set_level(lvl)
        elif t == EventType.VOICE_WAKE_SCORE:
            self.wake_meter.set_value(p.get("score", 0.0))
        elif t in (EventType.WAKE_STATUS, EventType.WAKE_ERROR):
            self._render_wake()
        elif t in (EventType.VOICE_LISTENING_START, EventType.VOICE_LISTENING_STOP):
            self._refresh_listen()
        elif t == EventType.VOICE_HOTWORD:
            self.orb.flash()
        elif t == EventType.TOOL_STARTED:
            self._tool_started(p.get("tool", ""))
        elif t in (EventType.TOOL_COMPLETED, EventType.TOOL_FAILED):
            self._tool_done(p.get("tool", ""), t == EventType.TOOL_COMPLETED, p.get("duration_ms", 0),
                            p.get("error", ""))
        elif t in (EventType.AUTOMATION_CREATED, EventType.AUTOMATION_UPDATED, EventType.AUTOMATION_CANCELLED,
                   EventType.AUTOMATION_TRIGGERED):
            self.refresh_next()
        elif t == EventType.ERROR:
            self.show_banner(p.get("error") or p.get("message") or "Something went wrong")
        elif t == EventType.TTS_ERROR:
            self.show_banner(f"Speech output problem: {str(p.get('error', ''))[:160]}")

    def render_state(self, s):
        state = s.get("state", "offline")
        color = state_color(state, current_palette())
        self.orb.set_state(state)
        label = s.get("label", state)
        set_chip(self.state_chip, state_word(state), "accent" if state not in ("offline", "idle", "error") else
                 ("err" if state == "error" else ""))
        self.state_label.setText(label)
        self.state_label.setStyleSheet(f"color: {color};")
        self.detail.setText(s.get("detail", "") or "")
        self.detail.setVisible(bool(s.get("detail")))
        self.stop_btn.setEnabled(state in ("speaking", "processing", "executing", "observing"))

    def _render_wake(self):
        if ui_bus.demo or self._voice is None:
            return
        w = self._voice.wake_status()
        if not w.get("enabled"):
            self.wake_line.setText("Wake word off — SAINT answers any clear speech.")
        elif w.get("ready"):
            self.wake_line.setText(f"Say “Hey SAINT” · threshold {w.get('threshold', 0.5):.2f}"
                                   + ("  ·  while music plays, just say “skip”" if actions.hotwords_on() else ""))
        else:
            self.wake_line.setText(f"Wake word unavailable: {w.get('error', 'unknown error')}")
            if w.get("code") not in ("NOT_LOADED",):
                self.show_banner(f"Wake word unavailable: {w.get('error')}")

    def show_banner(self, text):
        self.banner_text.setText(text)
        if not self.banner.isVisible():
            motion.fade_in(self.banner)

    def _refresh_listen(self):
        self.listen_btn.setEnabled(True)
        self.listen_btn.setText("Stop listening" if actions.listening() else "Start listening")

    def _toggle_listening(self):
        self.listen_btn.setEnabled(False)
        self.listen_btn.setText("…")

        def done(ok):
            self._refresh_listen()
            if ok is False and self._voice is not None:
                self.show_banner(self._voice.diagnostics.get("mic_error") or "Couldn't start the microphone.")
        actions.toggle_listening(done)

    def _send(self):
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        if not actions.submit(text):
            self.chat.add("system", "SAINT is still starting up — try again in a moment.")

    # ------------------------------------------------------------------ #
    def _tool_started(self, tool):
        self.live_empty.hide()
        row = ToolRow(tool)
        self._rows.insert(0, row)
        self.live_list.insertWidget(0, row)
        motion.fade_in(row, motion.BASE)
        while len(self._rows) > 5:
            self._rows.pop().deleteLater()

    def _tool_done(self, tool, ok, ms, error=""):
        row = next((r for r in self._rows if r.tool == tool and r.running), None)
        if row is None:
            self._tool_started(tool)
            row = self._rows[0]
        row.done(ok, ms, error)

    def refresh_next(self):
        from modules.automation.scenes import scenes
        from modules.automation.scheduler import scheduler

        def load():
            return [a for a in scheduler.list(active_only=True) if a.status == "active"][:4], scenes.all()[:3]

        def show(data):
            autos, scs = data
            clear_layout(self.next_list)
            if not autos:
                lab = QLabel("Nothing scheduled. Try “remind me at 5 to stretch”.")
                lab.setObjectName("Faint")
                lab.setWordWrap(True)
                self.next_list.addWidget(lab)
            for a in autos:
                row = QHBoxLayout()
                when = QLabel(datetime.fromtimestamp(a.next_run).strftime("%a %H:%M") if a.next_run else "—")
                when.setObjectName("Faint")
                when.setFixedWidth(70)
                row.addWidget(when)
                row.addWidget(ElidedLabel(a.title + ("  ↻" if a.recurring else "")), 1)
                self.next_list.addLayout(row)
            clear_layout(self.scene_row)
            for s in scs:
                b = QPushButton(s.name)
                b.setObjectName("SceneButton")
                b.setIcon(icons.icon("zap", current_palette().accent, 14))
                b.setToolTip("Run: " + " → ".join(s.steps))
                b.clicked.connect(lambda _=False, sc=s: actions.run_scene(sc))
                self.scene_row.addWidget(b)
            self.scene_row.addStretch()
        run_async(load, show)

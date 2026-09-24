"""
ui/overlay.py

The SAINT overlay — like the Steam overlay in a game. It opens over whatever
you're doing (hotkey, the Halo's edge tab, or the tray) on a frosted copy of
your screen: SAINT's state, an input, now playing, the conversation, what's
next, scenes and quick toggles. Esc or a click on the background closes it.
"""

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from core.config import config
from core.events import EventType
from ui import actions, icons, motion
from ui.pages.music import NowPlaying
from ui.reactive import ui_bus
from ui.theme import current_palette, state_color, state_word
from ui.widgets import ChatView, ElidedLabel, IconButton, Orb, chip, clear_layout, kbd, run_async, set_chip, with_alpha


def frost(shot: QPixmap, size) -> QPixmap:
    """Cheap, good-looking blur: shrink hard, then grow back smoothly."""
    if shot.isNull():
        return QPixmap()
    small = shot.scaled(max(1, shot.width() // 16), max(1, shot.height() // 16), Qt.IgnoreAspectRatio,
                        Qt.SmoothTransformation)
    small = small.scaled(small.width() * 2, small.height() * 2, Qt.IgnoreAspectRatio, Qt.SmoothTransformation) \
        .scaled(small.width(), small.height(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return small.scaled(size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def _card(title: str = "") -> tuple:
    f = QFrame()
    f.setObjectName("OverlayCard")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(20, 16, 20, 18)
    lay.setSpacing(10)
    if title:
        t = QLabel(title.upper())
        t.setObjectName("CardTitle")
        lay.addWidget(t)
    return f, lay


class Overlay(QWidget):
    def __init__(self, shell):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.shell = shell
        self.setWindowTitle("SAINT overlay")
        self._bg = QPixmap()
        self._closing = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.content = QWidget()
        self.content.setMaximumWidth(1240)
        outer.addWidget(self.content, 1, Qt.AlignHCenter)
        root = QVBoxLayout(self.content)
        root.setContentsMargins(40, 34, 40, 34)
        root.setSpacing(22)

        # ---- top bar ----------------------------------------------------------
        top = QHBoxLayout()
        top.setSpacing(10)
        self.logo = QLabel()
        top.addWidget(self.logo)
        word = QLabel("SAINT")
        word.setObjectName("Wordmark")
        top.addWidget(word)
        self.state_chip = chip("", "accent")
        top.addWidget(self.state_chip, 0, Qt.AlignVCenter)
        top.addStretch()
        self.clock = QLabel("")
        self.clock.setStyleSheet("font-size: 30px; font-weight: 300;")
        self.date = QLabel("")
        self.date.setObjectName("Muted")
        dc = QVBoxLayout()
        dc.setSpacing(0)
        dc.addWidget(self.clock, 0, Qt.AlignRight)
        dc.addWidget(self.date, 0, Qt.AlignRight)
        top.addLayout(dc)
        top.addSpacing(18)
        top.addWidget(kbd("Esc"), 0, Qt.AlignVCenter)
        close = IconButton("x", "Close (Esc)", 18)
        close.clicked.connect(self.close_overlay)
        top.addWidget(close)
        root.addLayout(top)

        # ---- hero -------------------------------------------------------------
        hero = QHBoxLayout()
        hero.setSpacing(24)
        hero.addStretch()
        self.orb = Orb(132)
        hero.addWidget(self.orb)
        hc = QVBoxLayout()
        hc.setSpacing(10)
        self.state_label = QLabel("")
        self.state_label.setObjectName("StateLabel")
        hc.addWidget(self.state_label)
        ir = QHBoxLayout()
        ir.setSpacing(8)
        self.input = QLineEdit()
        self.input.setObjectName("BigInput")
        self.input.setPlaceholderText("Ask SAINT anything…")
        self.input.setMinimumWidth(560)
        self.input.returnPressed.connect(self._send)
        ir.addWidget(self.input)
        self.mic = IconButton("mic", "Microphone on / off", 20, checkable=True)
        self.mic.clicked.connect(lambda: actions.toggle_listening(lambda _ok: self._sync_toggles()))
        ir.addWidget(self.mic)
        hc.addLayout(ir)
        self.reply = ElidedLabel("")
        self.reply.setObjectName("Muted")
        hc.addWidget(self.reply)
        hero.addLayout(hc)
        hero.addStretch()
        root.addLayout(hero)

        # ---- grid -------------------------------------------------------------
        grid = QGridLayout()
        grid.setSpacing(16)
        self.np_card, npl = _card("Now playing")
        self.now_playing = NowPlaying(cover=132)
        npl.addWidget(self.now_playing)
        qt = QLabel("NEXT IN QUEUE")
        qt.setObjectName("CardTitle")
        npl.addSpacing(4)
        npl.addWidget(qt)
        self.queue_list = QVBoxLayout()
        self.queue_list.setSpacing(4)
        npl.addLayout(self.queue_list)
        self._queue_track = None
        self.live_card, ll = _card("Live")
        self.live_list = QVBoxLayout()
        self.live_list.setSpacing(0)
        self.live_empty = QLabel("What SAINT does shows up here as it happens.")
        self.live_empty.setObjectName("Faint")
        self.live_empty.setWordWrap(True)
        ll.addWidget(self.live_empty)
        ll.addLayout(self.live_list)
        ll.addStretch()
        self._rows = []
        self.chat_card, cl = _card("Conversation")
        self.chat = ChatView(max_messages=14)
        self.chat.setMinimumHeight(220)
        cl.addWidget(self.chat)
        actions.ChatBinder(self.chat, self)
        self.next_card, nl = _card("Up next")
        self.next_list = QVBoxLayout()
        self.next_list.setSpacing(6)
        nl.addLayout(self.next_list)
        st = QLabel("SCENES")
        st.setObjectName("CardTitle")
        nl.addSpacing(8)
        nl.addWidget(st)
        self.scene_list = QVBoxLayout()
        self.scene_list.setSpacing(6)
        nl.addLayout(self.scene_list)
        nl.addStretch()
        grid.addWidget(self.np_card, 0, 0)
        grid.addWidget(self.live_card, 1, 0)
        grid.addWidget(self.chat_card, 0, 1, 2, 1)
        grid.addWidget(self.next_card, 0, 2, 2, 1)
        grid.setColumnStretch(0, 5)
        grid.setColumnStretch(1, 5)
        grid.setColumnStretch(2, 4)
        grid.setRowStretch(1, 1)
        root.addLayout(grid, 1)

        # ---- toggles ---------------------------------------------------------
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.t_halo = QPushButton(" Halo")
        self.t_widget = QPushButton(" Mini player")
        self.t_demo = QPushButton(" Demo")
        for b in (self.t_halo, self.t_widget):
            b.setCheckable(True)
        self.t_halo.clicked.connect(lambda on: shell.set_halo_mode("minimized" if on else "off"))
        self.t_widget.clicked.connect(lambda on: shell.set_widget(on))
        self.t_demo.clicked.connect(lambda: (self.close_overlay(), QTimer.singleShot(250, shell.start_demo)))
        for b in (self.t_halo, self.t_widget, self.t_demo):
            bottom.addWidget(b)
        bottom.addStretch()
        hint = QLabel(f"Toggle with {config.get('overlay.hotkey', '')}")
        hint.setObjectName("Faint")
        self.hotkey_hint = hint
        bottom.addWidget(hint)
        bottom.addSpacing(12)
        self.open_app = QPushButton("Open SAINT")
        self.open_app.setObjectName("Primary")
        self.open_app.clicked.connect(self._open_app)
        bottom.addWidget(self.open_app)
        root.addLayout(bottom)

        self._cards = [self.np_card, self.live_card, self.chat_card, self.next_card]
        ui_bus.event.connect(self._on_event)
        ui_bus.demo_changed.connect(self._demo_changed)
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)

    # ------------------------------------------------------------------ #
    def apply_theme(self):
        p = current_palette()
        from ui.main_window import logo_pixmap
        self.logo.setPixmap(logo_pixmap(26))
        for b, name in ((self.t_halo, "halo"), (self.t_widget, "widget"), (self.t_demo, "demo")):
            b.setIcon(icons.icon(name, p.text, 16))
        hk = config.get("overlay.hotkey", "")
        self.hotkey_hint.setText(f"Toggle with {hk}")
        self.hotkey_hint.setVisible(bool(hk))
        self.chat._render()

    def toggle(self):
        if not self.isVisible():
            self.open_overlay()
        elif not self._closing:
            self.close_overlay()

    def open_overlay(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = screen.geometry()
        self._bg = frost(screen.grabWindow(0), geo.size())
        self.setGeometry(geo)
        self._closing = False
        self.apply_theme()
        self._render_state(ui_bus.state)
        self._sync_toggles()
        self._refresh_next()
        self._refresh_queue()
        self._tick()
        self._clock.start(1000)
        self.setWindowOpacity(0.0 if motion.enabled() else 1.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()
        motion.animate(self, b"windowOpacity", 0.0, 1.0, motion.BASE)
        QTimer.singleShot(0, lambda: motion.stagger(self._cards, motion.SLOW, 50, dy=14))

    def close_overlay(self):
        if not self.isVisible() or self._closing:
            return
        self._closing = True
        self._clock.stop()

        def done():
            self.hide()
            self._closing = False
            self._bg = QPixmap()
        motion.animate(self, b"windowOpacity", self.windowOpacity(), 0.0, motion.FAST, on_done=done)

    def _open_app(self):
        self.close_overlay()
        self.shell.show_normal()

    # ------------------------------------------------------------------ #
    def paintEvent(self, _):
        p = current_palette()
        g = QPainter(self)
        if not self._bg.isNull():
            g.drawPixmap(self.rect(), self._bg)
        g.fillRect(self.rect(), QColor(8, 9, 11, 170) if p.dark else QColor(246, 246, 248, 175))
        glow = QRadialGradient(self.width() / 2, 0, self.width() * 0.55)
        glow.setColorAt(0, with_alpha(state_color(ui_bus.state.get("state", "offline"), p), 46))
        glow.setColorAt(1, with_alpha("#000000", 0))
        g.fillRect(self.rect(), glow)
        g.end()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close_overlay()
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):
        if self.childAt(e.position().toPoint()) in (None, self.content):     # empty background
            self.close_overlay()

    # ------------------------------------------------------------------ #
    def _tick(self):
        now = datetime.now()
        self.clock.setText(now.strftime("%I:%M").lstrip("0"))
        self.date.setText(now.strftime("%A, %B %d").replace(" 0", " "))

    def _render_state(self, s):
        state = s.get("state", "offline")
        self.orb.set_state(state)
        self.state_label.setText(s.get("label", state))
        self.state_label.setStyleSheet(f"color: {state_color(state, current_palette())};")
        set_chip(self.state_chip, state_word(state), "accent")
        self.update()

    def _sync_toggles(self):
        self.mic.setChecked(actions.listening())
        self.mic.set_icon("mic" if actions.listening() else "mic-off")
        self.t_halo.setChecked(config.get("overlay.halo", "minimized") != "off")
        self.t_widget.setChecked(bool(config.get("widgets.spotify", False)))

    def _send(self):
        text = self.input.text().strip()
        if text and actions.submit(text):
            self.input.clear()
            self.reply.setText("…")

    def _on_event(self, ev):
        t, p = ev.type, ev.payload or {}
        if t == EventType.TOOL_STARTED:
            from ui.pages.home import ToolRow
            self.live_empty.hide()
            row = ToolRow(p.get("tool", ""))
            self._rows.insert(0, row)
            self.live_list.insertWidget(0, row)
            while len(self._rows) > 6:
                self._rows.pop().deleteLater()
        elif t in (EventType.TOOL_COMPLETED, EventType.TOOL_FAILED):
            row = next((r for r in self._rows if r.tool == p.get("tool") and r.running), None)
            if row:
                row.done(t == EventType.TOOL_COMPLETED, p.get("duration_ms", 0), p.get("error", ""))
        if not self.isVisible():
            return
        if t == EventType.ASSISTANT_STATE:
            self._render_state(p)
        elif t == EventType.VOICE_AUDIO_LEVEL:
            self.orb.set_level(p.get("level", 0.0))
        elif t == EventType.VOICE_HOTWORD:
            self.orb.flash()
        elif t == EventType.UI_CHAT_RENDER and p.get("role") == "assistant":
            self.reply.setText(p.get("text", ""))
        elif t in (EventType.VOICE_LISTENING_START, EventType.VOICE_LISTENING_STOP):
            self._sync_toggles()
        elif t in (EventType.AUTOMATION_CREATED, EventType.AUTOMATION_UPDATED, EventType.AUTOMATION_CANCELLED):
            self._refresh_next()
        elif t == EventType.SPOTIFY_PLAYBACK_CHANGED and p.get("id") != self._queue_track:
            QTimer.singleShot(600, self._refresh_queue)      # the queue moved on with the track

    def _demo_changed(self, on):
        if not on:
            from ui.pages.home import drop_demo_rows
            drop_demo_rows(self._rows, self.live_empty)

    def _refresh_queue(self):
        if ui_bus.demo:
            return
        self._queue_track = ui_bus.spotify.get("id")

        def show(items):
            clear_layout(self.queue_list)
            if isinstance(items, str) or not items:
                lab = QLabel(items if isinstance(items, str) else "The queue is empty.")
                lab.setObjectName("Faint")
                lab.setWordWrap(True)
                self.queue_list.addWidget(lab)
                return
            for name, artists in items:
                row = QHBoxLayout()
                row.setSpacing(8)
                row.addWidget(ElidedLabel(name), 3)
                by = ElidedLabel(artists)
                by.setObjectName("Faint")
                row.addWidget(by, 2)
                self.queue_list.addLayout(row)

        def load():
            try:
                return actions.fetch_queue(5)
            except RuntimeError as e:
                return str(e)
        run_async(load, show, lambda e: show(f"Couldn't load the queue: {e}"))

    def _refresh_next(self):
        from modules.automation.scenes import scenes
        from modules.automation.scheduler import scheduler

        def load():
            return [a for a in scheduler.list(active_only=True) if a.status == "active"][:5], scenes.all()[:5]

        def show(data):
            autos, scs = data
            p = current_palette()
            clear_layout(self.next_list)
            clear_layout(self.scene_list)
            if not autos:
                lab = QLabel("Nothing scheduled.")
                lab.setObjectName("Faint")
                self.next_list.addWidget(lab)
            for a in autos:
                row = QHBoxLayout()
                when = QLabel(datetime.fromtimestamp(a.next_run).strftime("%a %H:%M") if a.next_run else "—")
                when.setObjectName("Faint")
                when.setFixedWidth(72)
                row.addWidget(when)
                row.addWidget(ElidedLabel(a.title), 1)
                self.next_list.addLayout(row)
            if not scs:
                lab = QLabel("Make scenes in Automations.")
                lab.setObjectName("Faint")
                self.scene_list.addWidget(lab)
            for s in scs:
                b = QPushButton(s.name)
                b.setObjectName("SceneButton")
                b.setIcon(icons.icon("zap", p.accent, 14))
                b.clicked.connect(lambda _=False, sc=s: actions.run_scene(sc))
                self.scene_list.addWidget(b)
        run_async(load, show)

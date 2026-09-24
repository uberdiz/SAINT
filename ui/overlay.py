"""
ui/overlay.py

The SAINT overlay — like the Steam overlay in a game. It opens over whatever
you're doing (hotkey, the Halo's edge tab, or the tray) on a frosted copy of
your screen: SAINT's state, an input, now playing, the conversation, what's
next, scenes and quick switches. Esc or a click on the background closes it.

Every card can be moved (drag its title bar) and resized (drag an edge or the
corner) anywhere on the screen; the arrangement is remembered in
overlay.layout and "Reset layout" puts the cards back.
"""

from datetime import datetime

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from core.config import config
from core.events import EventType
from ui import actions, icons, motion
from ui.pages.music import NowPlaying
from ui.reactive import ui_bus
from ui.theme import current_palette, state_color, state_word
from ui.widgets import (ChatView, ElidedLabel, IconButton, Orb, Switch, chip, clear_layout, kbd, run_async, set_chip,
                        with_alpha)

BAND = 1240           # width of the centred column the cards start in
GRID = 8              # cards snap to an 8 px grid
EDGE = 8              # grab zone for resizing, px
MIN_W, MIN_H = 240, 130
GAP = 16

# Starting arrangement inside the centred band: x, y, w, h as fractions
DEFAULT_LAYOUT = {
    "now_playing": (0.0, 0.0, 5 / 14, 0.47),
    "live": (0.0, 0.47, 5 / 14, 0.53),
    "chat": (5 / 14, 0.0, 5 / 14, 1.0),
    "next": (10 / 14, 0.0, 4 / 14, 1.0),
}


def frost(shot: QPixmap, size) -> QPixmap:
    """Cheap, good-looking blur: shrink hard, then grow back smoothly."""
    if shot.isNull():
        return QPixmap()
    small = shot.scaled(max(1, shot.width() // 16), max(1, shot.height() // 16), Qt.IgnoreAspectRatio,
                        Qt.SmoothTransformation)
    small = small.scaled(small.width() * 2, small.height() * 2, Qt.IgnoreAspectRatio, Qt.SmoothTransformation) \
        .scaled(small.width(), small.height(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return small.scaled(size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def _snap(v: int) -> int:
    return int(round(v / GRID) * GRID)


class FloatingCard(QFrame):
    """An overlay card you can move by its title bar and resize from any edge."""
    changed = Signal()            # moved or resized by the user

    def __init__(self, key: str, title: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.setObjectName("OverlayCard")
        self.setMouseTracking(True)
        self.setMinimumSize(MIN_W, MIN_H)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 12, 20, 18)
        outer.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(6)
        self.title = QLabel(title.upper())
        self.title.setObjectName("CardTitle")
        head.addWidget(self.title)
        head.addStretch()
        self.grip = QLabel()
        head.addWidget(self.grip)
        outer.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body, 1)
        self.setToolTip("")
        for w in (self.title, self.grip):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._mode = None          # "move", or the edges being dragged: "r", "b", "br", "tl", ...
        self._press = QPoint()
        self._start = QRect()
        self._hover = False

    def apply_theme(self):
        p = current_palette()
        self.grip.setPixmap(icons.pixmap("grip", p.faint, 14))
        # A little more solid than the stylesheet's card so moved cards stay readable when they overlap.
        c = QColor(p.surface)
        edge = "255,255,255" if p.dark else "0,0,0"
        self.setStyleSheet(f"QFrame#OverlayCard {{ background: rgba({c.red()},{c.green()},{c.blue()},248); "
                           f"border: 1px solid rgba({edge},22); border-radius: 16px; }}")

    # -- where the mouse is -------------------------------------------------- #
    def _zone(self, pos: QPoint):
        r = self.rect()
        v = "t" if pos.y() <= EDGE else "b" if pos.y() >= r.height() - EDGE else ""
        h = "l" if pos.x() <= EDGE else "r" if pos.x() >= r.width() - EDGE else ""
        if v or h:
            return v + h
        if pos.x() >= r.width() - 18 and pos.y() >= r.height() - 18:
            return "br"
        return "move" if pos.y() <= 40 else None

    @staticmethod
    def _cursor(zone):
        return {"move": Qt.OpenHandCursor, "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
                "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor, "tl": Qt.SizeFDiagCursor,
                "br": Qt.SizeFDiagCursor, "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor}.get(zone, Qt.ArrowCursor)

    # -- dragging -------------------------------------------------------------- #
    def mousePressEvent(self, e):
        zone = self._zone(e.position().toPoint())
        if e.button() == Qt.LeftButton and zone:
            self._mode = zone
            self._press = e.globalPosition().toPoint()
            self._start = self.geometry()
            self.raise_()
            if zone == "move":
                self.setCursor(Qt.ClosedHandCursor)
            self.update()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._mode is None:
            self.setCursor(self._cursor(self._zone(e.position().toPoint())))
            super().mouseMoveEvent(e)
            return
        d = e.globalPosition().toPoint() - self._press
        area = self.parentWidget().rect()
        s = self._start
        if self._mode == "move":
            x = max(0, min(_snap(s.x() + d.x()), area.width() - s.width()))
            y = max(0, min(_snap(s.y() + d.y()), area.height() - s.height()))
            self.move(x, y)
            return
        left, top, right, bottom = s.left(), s.top(), s.left() + s.width(), s.top() + s.height()
        if "r" in self._mode:
            right = min(area.width(), max(left + MIN_W, _snap(right + d.x())))
        if "b" in self._mode:
            bottom = min(area.height(), max(top + MIN_H, _snap(bottom + d.y())))
        if "l" in self._mode:
            left = max(0, min(right - MIN_W, _snap(left + d.x())))
        if "t" in self._mode:
            top = max(0, min(bottom - MIN_H, _snap(top + d.y())))
        self.setGeometry(left, top, right - left, bottom - top)

    def mouseReleaseEvent(self, e):
        if self._mode is not None:
            self._mode = None
            self.setCursor(self._cursor(self._zone(e.position().toPoint())))
            self.update()
            self.changed.emit()
            return
        super().mouseReleaseEvent(e)

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.unsetCursor()
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)
        if not (self._hover or self._mode):
            return
        p = current_palette()
        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        g.setPen(QPen(with_alpha(p.accent, 170 if self._mode else 90), 1.4))
        r = QRectF(self.rect())
        for i in (7, 12):                                   # the resize corner
            g.drawLine(int(r.right() - i), int(r.bottom() - 5), int(r.right() - 5), int(r.bottom() - i))
        if self._mode:
            g.setBrush(Qt.NoBrush)
            g.drawRoundedRect(r.adjusted(1, 1, -1, -1), 16, 16)
        g.end()


class CardCanvas(QWidget):
    """Holds the floating cards and remembers where they are (as fractions of
    the screen area, so the layout survives resolution changes)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cards = {}

    def add(self, card: FloatingCard):
        card.setParent(self)
        card.changed.connect(self.save)
        self.cards[card.key] = card

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.restore()

    def default_rect(self, key: str) -> QRect:
        W, H = self.width(), self.height()
        band = min(W, BAND)
        x0 = (W - band) // 2
        fx, fy, fw, fh = DEFAULT_LAYOUT.get(key, (0, 0, 0.3, 0.3))
        x, y = x0 + int(fx * band), int(fy * H)
        w, h = int(fw * band), int(fh * H)
        # gaps between neighbours
        return QRect(x + (GAP // 2 if fx > 0 else 0), y + (GAP // 2 if fy > 0 else 0),
                     w - (GAP // 2 if fx > 0 else 0) - (GAP // 2 if fx + fw < 0.999 else 0),
                     h - (GAP // 2 if fy > 0 else 0) - (GAP // 2 if fy + fh < 0.999 else 0))

    def rect_for(self, key: str) -> QRect:
        saved = (config.get("overlay.layout") or {}).get(key)
        W, H = max(1, self.width()), max(1, self.height())
        if isinstance(saved, (list, tuple)) and len(saved) == 4:
            x, y, w, h = saved
            rect = QRect(int(x * W), int(y * H), max(MIN_W, int(w * W)), max(MIN_H, int(h * H)))
        else:
            rect = self.default_rect(key)
        rect.setWidth(min(rect.width(), W))
        rect.setHeight(min(rect.height(), H))
        rect.moveLeft(max(0, min(rect.x(), W - rect.width())))
        rect.moveTop(max(0, min(rect.y(), H - rect.height())))
        return rect

    def restore(self):
        for key, card in self.cards.items():
            card.setGeometry(self.rect_for(key))

    def save(self):
        W, H = max(1, self.width()), max(1, self.height())
        config.set("overlay.layout", {k: [round(c.x() / W, 4), round(c.y() / H, 4), round(c.width() / W, 4),
                                          round(c.height() / H, 4)] for k, c in self.cards.items()})

    def reset(self):
        config.set("overlay.layout", {})
        for key, card in self.cards.items():
            old, new = card.geometry(), self.rect_for(key)
            if old != new:
                motion.animate(card, b"geometry", old, new, motion.SLOW)


def _centred(root, layout) -> QWidget:
    """Add ``layout`` to ``root`` in a band as wide as the default card area, centred."""
    band = QWidget()
    band.setLayout(layout)
    band.setMaximumWidth(BAND)
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addStretch(1)
    row.addWidget(band, 1000)
    row.addStretch(1)
    root.addLayout(row)
    return band


class Overlay(QWidget):
    def __init__(self, shell):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.shell = shell
        self.setWindowTitle("SAINT overlay")
        self._bg = QPixmap()
        self._closing = False
        root = QVBoxLayout(self)
        root.setContentsMargins(40, 34, 40, 34)
        root.setSpacing(22)

        # ---- top bar ----------------------------------------------------------
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
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
        self.top_band = _centred(root, top)

        # ---- hero -------------------------------------------------------------
        hero = QHBoxLayout()
        hero.setContentsMargins(0, 0, 0, 0)
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
        self.hero_band = _centred(root, hero)

        # ---- cards: drag a title bar to move, an edge or corner to resize -----
        self.canvas = CardCanvas()

        def card(key, title):
            c = FloatingCard(key, title)
            self.canvas.add(c)
            return c, c.body

        self.np_card, npl = card("now_playing", "Now playing")
        self.now_playing = NowPlaying(cover=132, any_media=True)
        npl.addWidget(self.now_playing)
        qt = QLabel("NEXT IN QUEUE")
        qt.setObjectName("CardTitle")
        npl.addSpacing(4)
        npl.addWidget(qt)
        self.queue_list = QVBoxLayout()
        self.queue_list.setSpacing(4)
        npl.addLayout(self.queue_list)
        npl.addStretch()
        self._queue_title = qt
        self._queue_track = None
        self.live_card, ll = card("live", "Live")
        self.live_list = QVBoxLayout()
        self.live_list.setSpacing(0)
        self.live_empty = QLabel("What SAINT does shows up here as it happens.")
        self.live_empty.setObjectName("Faint")
        self.live_empty.setWordWrap(True)
        ll.addWidget(self.live_empty)
        ll.addLayout(self.live_list)
        ll.addStretch()
        self._rows = []
        self.chat_card, cl = card("chat", "Conversation")
        self.chat = ChatView(max_messages=14)
        self.chat.setMinimumHeight(60)
        cl.addWidget(self.chat)
        actions.ChatBinder(self.chat, self)
        self.next_card, nl = card("next", "Up next")
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
        root.addWidget(self.canvas, 1)

        # ---- switches -----------------------------------------------------------
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(18)
        self.t_halo = Switch("Halo")
        self.t_halo.setToolTip("The glow around your screen edge — it lights up right away so you can see it")
        self.t_widget = Switch("Mini player")
        self.t_widget.setToolTip("Floating player for whatever is playing")
        self.t_notices = Switch("Action notices")
        self.t_notices.setToolTip("A small notice at the bottom of the screen when SAINT does something")
        self.t_halo.clicked.connect(lambda on: shell.set_halo_mode("minimized" if on else "off"))
        self.t_widget.clicked.connect(lambda on: shell.set_widget(on))
        self.t_notices.clicked.connect(lambda on: shell.set_action_notices(on))
        for b in (self.t_halo, self.t_widget, self.t_notices):
            bottom.addWidget(b)
        self.t_demo = QPushButton(" Demo")
        self.t_demo.clicked.connect(lambda: (self.close_overlay(), QTimer.singleShot(250, shell.start_demo)))
        bottom.addWidget(self.t_demo)
        bottom.addStretch()
        self.reset_layout = QPushButton(" Reset layout")
        self.reset_layout.setToolTip("Put the cards back where they started")
        self.reset_layout.clicked.connect(self.canvas.reset)
        bottom.addWidget(self.reset_layout)
        bottom.addSpacing(8)
        hint = QLabel(f"Toggle with {config.get('overlay.hotkey', '')}")
        hint.setObjectName("Faint")
        self.hotkey_hint = hint
        bottom.addWidget(hint)
        bottom.addSpacing(12)
        self.open_app = QPushButton("Open SAINT")
        self.open_app.setObjectName("Primary")
        self.open_app.clicked.connect(self._open_app)
        bottom.addWidget(self.open_app)
        self.bottom_band = _centred(root, bottom)

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
        self.t_demo.setIcon(icons.icon("demo", p.text, 16))
        self.reset_layout.setIcon(icons.icon("layout", p.text, 16))
        for c in self.canvas.cards.values():
            c.apply_theme()
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
        self.canvas.restore()
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
        if self.childAt(e.position().toPoint()) in (None, self.canvas, self.top_band, self.hero_band,
                                                    self.bottom_band):     # empty background
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
        for sw, on in ((self.t_halo, config.get("overlay.halo", "minimized") != "off"),
                       (self.t_widget, bool(config.get("widgets.spotify", False))),
                       (self.t_notices, bool(config.get("notifications.actions", True)))):
            if sw.isChecked() != on:
                sw.setChecked(on)

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
        elif t == EventType.MEDIA_CHANGED:
            self._show_queue(ui_bus.now_playing().get("source", "spotify") == "spotify")

    def _demo_changed(self, on):
        if not on:
            from ui.pages.home import drop_demo_rows
            drop_demo_rows(self._rows, self.live_empty)

    def _show_queue(self, on: bool):
        """The Spotify queue only means something while Spotify is what's shown."""
        self._queue_title.setVisible(on)
        for i in range(self.queue_list.count()):
            w = self.queue_list.itemAt(i).widget()
            if w is not None:
                w.setVisible(on)
            elif self.queue_list.itemAt(i).layout() is not None:
                lay = self.queue_list.itemAt(i).layout()
                for j in range(lay.count()):
                    if lay.itemAt(j).widget():
                        lay.itemAt(j).widget().setVisible(on)

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
            else:
                for name, artists in items:
                    row = QHBoxLayout()
                    row.setSpacing(8)
                    row.addWidget(ElidedLabel(name), 3)
                    by = ElidedLabel(artists)
                    by.setObjectName("Faint")
                    row.addWidget(by, 2)
                    self.queue_list.addLayout(row)
            self._show_queue(ui_bus.now_playing().get("source", "spotify") == "spotify")

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

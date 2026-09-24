"""
ui/demo.py

Demo mode: SAINT drives its own UI for ~30 seconds — wake word, a request,
a tool call, music with covers, a hands-free “skip”, page tours, the command
palette and the overlay. Great for showing SAINT off or recording it.

Everything is scripted through ui_bus.inject(), so no real command runs, no
music plays and nothing is written to your history. Esc stops it.
"""

import math
import random
import time

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen, QPixmap, QRadialGradient

from core.events import EventType
from ui.reactive import ui_bus
from ui.widgets import covers

# Fictional tracks with generated covers — nothing personal, nothing licensed.
TRACKS = [
    {"track": "Glass Harbor", "artists": "Low Tide Club", "album": "Glass Harbor", "duration_ms": 204000,
     "colors": ("#12355b", "#4fc3d9"), "seed": 3},
    {"track": "Neon Skyline", "artists": "The Midnight Drive", "album": "Afterglow Avenue", "duration_ms": 231000,
     "colors": ("#ff5f6d", "#ffc371"), "seed": 7},
    {"track": "Paper Satellites", "artists": "Aurora Lane", "album": "Signals", "duration_ms": 198000,
     "colors": ("#4a1d8f", "#c86dd7"), "seed": 11},
]


def make_cover(c1: str, c2: str, seed: int, size: int = 480) -> QPixmap:
    """A simple generative album cover: gradient, soft orbs and a horizon line."""
    rnd = random.Random(seed)
    pm = QPixmap(size, size)
    g = QPainter(pm)
    g.setRenderHint(QPainter.Antialiasing)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0, QColor(c1))
    grad.setColorAt(1, QColor(c2))
    g.fillRect(pm.rect(), grad)
    for _ in range(5):
        x, y, r = rnd.uniform(0, size), rnd.uniform(0, size), rnd.uniform(size * 0.08, size * 0.35)
        orb = QRadialGradient(QPointF(x, y), r)
        orb.setColorAt(0, QColor(255, 255, 255, rnd.randint(40, 110)))
        orb.setColorAt(1, QColor(255, 255, 255, 0))
        g.setPen(Qt.NoPen)
        g.setBrush(orb)
        g.drawEllipse(QPointF(x, y), r, r)
    g.setPen(QPen(QColor(255, 255, 255, 150), size * 0.012))
    y = size * rnd.uniform(0.55, 0.72)
    g.drawLine(QPointF(size * 0.12, y), QPointF(size * 0.88, y))
    g.setPen(Qt.NoPen)
    g.setBrush(QColor(255, 255, 255, 200))
    g.drawEllipse(QRectF(size * 0.5 - size * 0.09, y - size * 0.2, size * 0.18, size * 0.18))
    g.end()
    return pm


def track_state(i: int, playing: bool = True, progress_ms: int = 42000) -> dict:
    t = TRACKS[i % len(TRACKS)]
    url = f"demo://cover/{i}"
    if covers.color(url) is None:
        covers.put(url, make_cover(*t["colors"], t["seed"]))
    return {"is_playing": playing, "track": t["track"], "artists": t["artists"], "album": t["album"],
            "id": f"demo{i}", "progress_ms": progress_ms, "duration_ms": t["duration_ms"], "device": "This PC",
            "volume": 60, "shuffle": False, "repeat": "off", "image": url, "image_large": url}


def state(name: str, label: str, detail: str = "") -> dict:
    return {"state": name, "label": label, "detail": detail}


class Demo(QObject):
    finished = Signal()

    def __init__(self, shell):
        super().__init__(shell)
        self.shell = shell
        self.running = False
        self._timers = []
        self._levels = QTimer(self)
        self._levels.setInterval(80)
        self._levels.timeout.connect(self._level)

    def start(self):
        if self.running:
            return
        self.running = True
        ui_bus.set_demo(True)
        self.shell.show_normal()
        self.shell.toast("Demo mode", "SAINT is driving itself for 30 seconds. Nothing real runs — Esc stops it.",
                         icon="demo")
        t = 0
        for delay, fn in self._script():
            t += delay
            tm = QTimer(self)
            tm.setSingleShot(True)
            tm.timeout.connect(fn)
            tm.start(t)
            self._timers.append(tm)

    def stop(self):
        if not self.running:
            return
        for tm in self._timers:
            tm.stop()
            tm.deleteLater()
        self._timers.clear()
        self._levels.stop()
        self.running = False
        self.shell.palette.close_palette()
        self.shell.overlay.close_overlay()
        ui_bus.set_demo(False)
        self.finished.emit()

    # ------------------------------------------------------------------ #
    def _level(self):
        t = time.monotonic()
        ui_bus.inject(EventType.VOICE_AUDIO_LEVEL, {"level": 0.15 + 0.6 * abs(math.sin(t * 7)) * random.random()})

    def _tokens(self, turn, text):
        steps = []
        for word in text.split(" "):
            steps.append((110, lambda w=word + " ": ui_bus.inject(EventType.AI_STREAM_TOKEN,
                                                                  {"turn_id": turn, "token": w})))
        return steps

    def _type(self, text):
        return [(90, lambda s=text[:i + 1]: self.shell.palette.input.setText(s)) for i in range(len(text))]

    def _script(self):
        inj, sh = ui_bus.inject, self.shell
        turn = 900001
        reply = "Playing Neon Skyline by The Midnight Drive."
        return [
            (0, lambda: (sh.navigate("Home"),
                         inj(EventType.ASSISTANT_STATE, state("wake_listening", "Listening for “Hey SAINT”")),
                         inj(EventType.SPOTIFY_PLAYBACK_CHANGED, track_state(0)))),
            (1500, lambda: (inj(EventType.VOICE_WAKE_WORD, {"score": 0.93}),
                            inj(EventType.ASSISTANT_STATE, state("wake_detected", "Wake word detected")))),
            (400, lambda: (inj(EventType.ASSISTANT_STATE, state("command_listening", "Listening")),
                           self._levels.start())),
            (1800, lambda: (self._levels.stop(), inj(EventType.VOICE_AUDIO_LEVEL, {"level": 0.0}),
                            inj(EventType.ASSISTANT_STATE, state("processing", "Thinking")),
                            inj(EventType.CONVERSATION_TURN_START, {"text": "play something upbeat",
                                                                    "turn_id": turn}))),
            (700, lambda: (inj(EventType.TOOL_STARTED, {"tool": "spotify.play_recommended"}),
                           inj(EventType.ASSISTANT_STATE, state("executing", "Working on it", "Working with Spotify")))),
            (1000, lambda: (inj(EventType.TOOL_COMPLETED, {"tool": "spotify.play_recommended", "duration_ms": 640}),
                            inj(EventType.SPOTIFY_PLAYBACK_CHANGED, track_state(1, progress_ms=1000)),
                            inj(EventType.ASSISTANT_STATE, state("speaking", "Speaking")))),
            *self._tokens(turn, reply),
            (300, lambda: (inj(EventType.UI_CHAT_RENDER, {"turn_id": turn, "role": "assistant", "text": reply}),
                           inj(EventType.ASSISTANT_STATE, state("wake_listening", "Listening for “Hey SAINT”")))),
            (1600, lambda: (inj(EventType.VOICE_HOTWORD, {"text": "skip"}),
                            inj(EventType.TOOL_STARTED, {"tool": "spotify.next"}))),
            (500, lambda: (inj(EventType.TOOL_COMPLETED, {"tool": "spotify.next", "duration_ms": 212}),
                           inj(EventType.SPOTIFY_PLAYBACK_CHANGED, track_state(2, progress_ms=500)))),
            (1800, lambda: sh.navigate("Music")),
            (4200, lambda: sh.navigate("Automations")),
            (3000, lambda: sh.navigate("History")),
            (3000, lambda: (sh.navigate("Home"), sh.palette.open())),
            *self._type("mini player"),
            (1400, lambda: sh.palette.close_palette()),
            (500, lambda: sh.overlay.open_overlay()),
            (5500, lambda: sh.overlay.close_overlay()),
            (700, lambda: (sh.toast("That's SAINT", "Say “Hey SAINT” to try it for real.", icon="sparkles"),
                           self.stop())),
        ]

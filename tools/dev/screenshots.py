"""
tools/dev/screenshots.py — regenerate the README screenshots.

Builds the real UI against a throwaway data directory seeded with synthetic
data (fictional tracks, scenes, history), so no personal data ever ends up in
docs/screenshots. Also a quick end-to-end smoke test of every page.

    python tools/dev/screenshots.py            # writes docs/screenshots/*.png
"""

import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
DATA = Path(tempfile.mkdtemp(prefix="saint-shots-"))
os.environ["SAINT_DATA_DIR"] = str(DATA)
OUT = ROOT / "docs" / "screenshots"

(DATA / "config.json").write_text(json.dumps({
    "config_version": 6,
    "ai": {"provider": "mock", "model": "llama3.1"},
    "modules": {"ai": True, "voice": False, "automation": True, "vision": False, "memory": True,
                "spotify": False, "desktop": False},
    "voice": {"stt_backend": "mock", "tts_backend": "mock", "auto_start": False, "wake_word_enabled": True},
    "appearance": {"theme": "Dark", "accent": "#feaa34", "font_family": "Segoe UI", "font_size": 13},
    "overlay": {"hotkey": ""},
    "notifications": {"tray": False},
    "logging": {"level": "Errors Only"},
}), encoding="utf-8")


def seed():
    now = time.time()
    rnd = random.Random(4)
    asks = [("voice", "play something upbeat", "Playing Neon Skyline by The Midnight Drive.",
             [("spotify.play_recommended", 640)]),
            ("hotword", "skip", "Skipped it.", [("spotify.next", 212)]),
            ("voice", "remind me in 20 minutes to stretch", "Okay — I'll remind you at 4:20 PM.",
             [("automation.create_reminder", 18)]),
            ("typed", "what's on my screen", "You're in VS Code with main.py open.", [("screen.describe", 890)]),
            ("voice", "open chrome", "Chrome is open.", [("desktop.open_app", 420)]),
            ("hotword", "louder", "Volume 70%.", [("spotify.volume_step", 150)]),
            ("voice", "what's the weather tomorrow", "Sunny, 24 degrees.", []),
            ("scene", "Focus mode", "", [])]
    with open(DATA / "history.jsonl", "w", encoding="utf-8") as f:
        for day in range(30):
            for _ in range(rnd.randint(2, 14) if day % 6 else rnd.randint(0, 3)):
                src, user, reply, tools = rnd.choice(asks)
                ts = now - day * 86400 - rnd.randint(0, 36000)
                f.write(json.dumps({"ts": ts, "source": src, "user": user, "reply": reply, "ms": rnd.randint(700, 2600),
                                    "tools": [{"tool": t, "ok": True, "ms": ms} for t, ms in tools]}) + "\n")
    (DATA / "scenes.json").write_text(json.dumps([
        {"id": "a1", "name": "Focus mode", "phrase": "focus time", "steps": ["play lofi beats", "set volume to 35"],
         "schedule": "", "automation_id": "", "last_run": 0},
        {"id": "a2", "name": "Wind down", "phrase": "", "steps": ["play something chill", "set volume to 20"],
         "schedule": "every day at 10 PM", "automation_id": "", "last_run": 0},
        {"id": "a3", "name": "Morning", "phrase": "good morning", "steps": ["play my liked songs", "open chrome"],
         "schedule": "", "automation_id": "", "last_run": 0}]), encoding="utf-8")


def main():
    seed()
    from core.logger import init_logger
    init_logger("Errors Only")
    from core import analytics, history, state  # noqa: F401
    from PySide6.QtCore import QPointF, QRect, Qt
    from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap, QRadialGradient
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    from core.module_manager import module_manager  # noqa: F401
    from core.events import EventType
    from modules.desktop.media import media
    media.start = lambda: None          # never show what's really playing on this PC
    from modules.automation.scheduler import scheduler
    from modules.automation.timeparse import parse_schedule
    for what, when in (("Stand up and stretch", "in 20 minutes"), ("Check the calendar", "every weekday at 8:30")):
        scheduler.create("reminder", what, parse_schedule(when)[0])
    from modules.memory.service import memory_service
    for fact in ("My favorite programming language is Python", "I prefer lofi while working"):
        memory_service.remember(content=fact, category="preference")

    from ui.demo import state as st, track_state
    from ui.main_window import MainWindow
    from ui.reactive import ui_bus
    w = MainWindow(app, None)
    for win in (w, w.overlay, w.widget):           # render for real, but never appear on screen
        win.setAttribute(Qt.WA_DontShowOnScreen)
    if w.tray:
        w.tray.hide()
    w.resize(1440, 900)
    w.show()
    ui_bus.set_demo(True)
    inj = ui_bus.inject
    inj(EventType.ASSISTANT_STATE, st("wake_listening", "Listening for “Hey SAINT”"))
    inj(EventType.SPOTIFY_PLAYBACK_CHANGED, track_state(1, progress_ms=81000))
    inj(EventType.CONVERSATION_TURN_START, {"text": "play something upbeat", "turn_id": 1})
    inj(EventType.UI_CHAT_RENDER, {"turn_id": 1, "role": "assistant", "text": "Playing Neon Skyline by The Midnight Drive."})
    inj(EventType.CONVERSATION_TURN_START, {"text": "remind me in 20 minutes to stretch", "turn_id": 2})
    inj(EventType.UI_CHAT_RENDER, {"turn_id": 2, "role": "assistant", "text": "Okay — I'll remind you at 4:20 PM."})
    for tool, ms in (("spotify.play_recommended", 640), ("automation.create_reminder", 18), ("spotify.next", 212)):
        inj(EventType.TOOL_STARTED, {"tool": tool})
        inj(EventType.TOOL_COMPLETED, {"tool": tool, "duration_ms": ms})
    inj(EventType.TOOL_STARTED, {"tool": "screen.describe"})
    w.music.set_queue(["Paper Satellites   ·   Aurora Lane", "Glass Harbor   ·   Low Tide Club",
                       "Night Swim   ·   Coral Static", "Afterimage   ·   The Midnight Drive"])

    def settle(ms=700):
        end = time.time() + ms / 1000
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)

    OUT.mkdir(parents=True, exist_ok=True)
    for old in ("dashboard.png", "memory.png", "settings-appearance.png", "settings-wake-word.png"):
        (OUT / old).unlink(missing_ok=True)
    shots = {"home": "Home", "music": "Music", "automations": "Automations", "history": "History",
             "system": "System", "settings": "Settings"}
    for fname, page in shots.items():
        w.navigate(page, animate=False)
        settle(900)
        if page == "Automations":
            w.stack.currentWidget().scenes.list.setCurrentRow(0)
            settle(300)
        w.grab().save(str(OUT / f"{fname}.png"))
        print("saved", fname)

    # A made-up desktop to show the overlay, the Halo and the mini player on.
    W, H = 1600, 900
    desk = QPixmap(W, H)
    g = QPainter(desk)
    g.setRenderHint(QPainter.Antialiasing)
    bg = QLinearGradient(0, 0, W, H)
    bg.setColorAt(0, QColor("#1b2a49"))
    bg.setColorAt(1, QColor("#3b1f4a"))
    g.fillRect(desk.rect(), bg)
    for x, y, r, c in ((300, 200, 420, "#4fc3d9"), (1300, 700, 520, "#ff8a5c")):
        glow = QRadialGradient(QPointF(x, y), r)
        glow.setColorAt(0, QColor(c).lighter(110))
        glow.setColorAt(1, QColor(0, 0, 0, 0))
        g.fillRect(desk.rect(), glow)
    g.setBrush(QColor(20, 22, 28, 235))
    g.setPen(QColor(255, 255, 255, 30))
    g.drawRoundedRect(QRect(180, 120, 900, 560), 10, 10)
    g.setPen(QColor(255, 255, 255, 60))
    for i in range(14):
        g.drawLine(220, 190 + i * 30, 220 + random.Random(i).randint(200, 760), 190 + i * 30)
    g.end()

    from ui.overlay import frost
    ov = w.overlay
    ov.apply_theme()
    ov._render_state(ui_bus.state)
    ov._sync_toggles()
    ov._refresh_next()
    ov._tick()
    ov._bg = frost(desk, desk.size())
    ov.setWindowOpacity(1.0)
    ov.setGeometry(0, 0, W, H)
    ov.show()
    settle(900)
    ov.grab().save(str(OUT / "overlay.png"))
    ov.hide()
    print("saved overlay")

    w.set_widget(True)
    settle(900)
    w.widget.grab().save(str(OUT / "mini-player.png"))
    print("saved mini-player")

    from ui.halo import THICK

    class FakeStrip:
        def __init__(self, edge, rect):
            self.edge, self.screen_geo, self._r = edge, QRect(0, 0, W, H), rect

        def width(self):
            return self._r.width()

        def height(self):
            return self._r.height()

        def rect(self):
            return QRect(0, 0, self._r.width(), self._r.height())

    halo = w.halo
    halo._color = QColor("#ff8a5c")
    halo._cur = [0.10, 1.0, 0.0, 0.16]
    halo._comets = 2
    halo._head = 0.08
    shot = desk.copy()
    g = QPainter(shot)
    for edge, r in (("top", QRect(0, 0, W, THICK)), ("bottom", QRect(0, H - THICK, W, THICK)),
                    ("left", QRect(0, THICK, THICK, H - 2 * THICK)), ("right", QRect(W - THICK, THICK, THICK, H - 2 * THICK))):
        layer = QPixmap(r.size())
        layer.fill(Qt.transparent)
        lp = QPainter(layer)
        halo.paint_strip(FakeStrip(edge, r), lp)
        lp.end()
        g.drawPixmap(r.topLeft(), layer)
    widget_shot = w.widget.grab()
    g.drawPixmap(W - widget_shot.width() - 20, H - widget_shot.height() - 20, widget_shot)
    g.end()
    shot.save(str(OUT / "halo.png"))
    print("saved halo")
    w.demo.stop()
    w.quit()


if __name__ == "__main__":
    main()

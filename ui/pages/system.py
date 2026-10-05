"""
ui/pages/system.py

Health, speed and modules in one place: live resource use, whether each
subsystem is up, voice-pipeline latency against its goal, and module status.
"""

import platform

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout,
                               QWidget)

from core.analytics import analytics
from core.config import config
from core.state import app_state
from ui.theme import current_palette
from ui.widgets import Card, ElidedLabel, Page, chip, run_async, set_chip

# (title, analytics key, goal ms)
LATENCY = [("Overall response", "avg_overall_ms", 250), ("Speech-to-text", "avg_stt_ms", 100),
           ("Model", "avg_model_ms", 800), ("First token", "avg_ai_first_token_ms", 400),
           ("Intent routing", "avg_intent_ms", 50), ("Text-to-speech", "avg_tts_inference_ms", 150)]


class Tile(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setObjectName("StatCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 13, 16, 13)
        lay.setSpacing(3)
        t = QLabel(title.upper())
        t.setObjectName("CardTitle")
        self.value = ElidedLabel("—")
        self.value.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.sub = ElidedLabel("")
        self.sub.setObjectName("Faint")
        for w in (t, self.value, self.sub):
            lay.addWidget(w)

    def set(self, value, sub="", tone=""):
        p = current_palette()
        self.value.setText(str(value))
        self.value.setStyleSheet(f"font-size: 18px; font-weight: 600; color: {getattr(p, tone, p.text)};")
        self.sub.setText(sub)


class SystemPage(Page):
    def __init__(self):
        super().__init__("System", "Health, speed and modules.")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        body = QVBoxLayout(inner)
        body.setContentsMargins(0, 0, 8, 0)
        body.setSpacing(16)
        scroll.setWidget(inner)
        self.root.addWidget(scroll, 1)

        grid = QGridLayout()
        grid.setSpacing(12)
        names = ["Status", "CPU", "Memory", "Uptime", "Language model", "Speech-to-text", "Voice", "Wake word"]
        self.t = {n: Tile(n) for n in names}
        for i, n in enumerate(names):
            grid.addWidget(self.t[n], i // 4, i % 4)
        body.addLayout(grid)


        # What started and what didn't (core/startup.py), with Retry for failures.
        boot = Card("Startup")
        self.boot_rows = QVBoxLayout()
        self.boot_rows.setSpacing(8)
        boot.body.addLayout(self.boot_rows)
        body.addWidget(boot)
        self._boot_sig = None

        lat = Card("Voice pipeline latency")
        lg = QGridLayout()
        lg.setSpacing(12)
        self.lat = {}
        for i, (title, key, goal) in enumerate(LATENCY):
            tile = Tile(title)
            self.lat[key] = (tile, goal)
            lg.addWidget(tile, i // 3, i % 3)
        lat.body.addLayout(lg)
        body.addWidget(lat)

        mods = Card("Modules")
        self.mod_rows = QVBoxLayout()
        self.mod_rows.setSpacing(10)
        mods.body.addLayout(self.mod_rows)
        from core.module_manager import module_manager
        self._chips = {}
        for key, m in module_manager.modules.items():
            row = QHBoxLayout()
            name = QLabel(m.name)
            name.setStyleSheet("font-weight: 600;")
            name.setFixedWidth(110)
            desc = ElidedLabel(m.description)
            desc.setObjectName("Muted")
            c = chip("")
            self._chips[key] = (m, c)
            row.addWidget(name)
            row.addWidget(desc, 1)
            row.addWidget(c)
            self.mod_rows.addLayout(row)
        body.addWidget(mods)
        body.addStretch()

        self._ollama = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()
        self.timer.start(3000)

    def hideEvent(self, e):
        super().hideEvent(e)
        self.timer.stop()

    def refresh(self):
        from core.module_manager import module_manager
        errors, crashes = app_state.error_count, app_state.module_crash_count
        ok = errors == 0 and crashes == 0
        self.t["Status"].set("Healthy" if ok else "Degraded", f"{errors} errors · {crashes} crashes",
                             "success" if ok else "warning")
        self.t["CPU"].set(f"{app_state.cpu_percent():.0f}%", f"{platform.processor()[:28] or platform.machine()}")
        self.t["Memory"].set(f"{app_state.ram_mb():.0f} MB", "SAINT process")
        self.t["Uptime"].set(app_state.uptime_formatted(), f"Python {platform.python_version()}")

        provider, model = config.get("ai.provider", "mock"), config.get("ai.model", "")
        if provider == "ollama":
            def ping():
                import requests
                return requests.get(config.get("ai.base_url", "http://localhost:11434").rstrip("/") + "/api/version",
                                    timeout=2).ok
            run_async(ping, lambda up: self.t["Language model"].set(
                model, "Ollama · connected" if up else "Ollama · not reachable", "" if up else "danger"),
                lambda _e: self.t["Language model"].set(model, "Ollama · not reachable", "danger"))
        else:
            self.t["Language model"].set(model or provider, provider)

        voice = module_manager.get("voice")
        diag = voice.diagnostics if voice else {}
        self.t["Speech-to-text"].set(config.get("voice.stt_model", "—"), diag.get("stt_device") or "not loaded",
                                     "" if diag.get("stt_loaded") else "warning")
        self.t["Voice"].set(config.get("voice.tts_backend", "—").capitalize(), config.get("voice.tts_voice", ""))
        w = voice.wake_status() if voice else {}
        if not w.get("enabled"):
            self.t["Wake word"].set("Off", "responds to all speech", "warning")
        else:
            self.t["Wake word"].set("Ready" if w.get("ready") else "Unavailable",
                                    f"{w.get('avg_infer_ms', '—')} ms/frame" if w.get("ready") else w.get("error", ""),
                                    "success" if w.get("ready") else "danger")

        snap = analytics.snapshot()
        for key, (tile, goal) in self.lat.items():
            v = float(snap.get(key, 0.0) or 0.0)
            if v <= 0:
                tile.set("—", f"goal {goal} ms")
            else:
                tone = "success" if v <= goal else "" if v <= goal * 1.3 else "warning"
                tile.set(f"{v:.0f} ms", f"goal {goal} ms", tone)

        for key, (m, c) in self._chips.items():
            set_chip(c, "On" if m.enabled else "Off", "ok" if m.enabled else "")
        self._render_startup()

    def _render_startup(self):
        from core.startup import startup
        steps = startup.steps()
        sig = tuple((s["key"], s["status"], s["detail"]) for s in steps)
        if sig == self._boot_sig:
            return
        self._boot_sig = sig
        while self.boot_rows.count():
            item = self.boot_rows.takeAt(0)
            lay = item.layout()
            while lay is not None and lay.count():
                w = lay.takeAt(0).widget()
                if w is not None:
                    w.deleteLater()
        if not steps:
            self.boot_rows.addLayout(self._boot_row("SAINT is still starting…", "", "", None))
        for s in steps:
            self.boot_rows.addLayout(self._boot_row(s["label"], s["status"], s["detail"],
                                                    s["key"] if s["retryable"] else None))

    def _boot_row(self, label, status, detail, retry_key):
        row = QHBoxLayout()
        name = QLabel(label)
        name.setStyleSheet("font-weight: 600;")
        name.setFixedWidth(230)
        desc = ElidedLabel(detail)
        desc.setObjectName("Muted")
        c = chip("")
        set_chip(c, {"ok": "Running", "failed": "Failed", "off": "Off"}.get(status, "…"),
                 {"ok": "ok", "failed": "err"}.get(status, ""))
        row.addWidget(name)
        row.addWidget(desc, 1)
        if retry_key:
            b = QPushButton("Retry")
            b.setToolTip(f"Start {label} again")

            def again(_=False, key=retry_key):
                from core.startup import startup
                b.setEnabled(False)
                run_async(lambda: startup.retry(key), lambda _ok: self.refresh(), lambda _e: self.refresh())
            b.clicked.connect(again)
            row.addWidget(b)
        row.addWidget(c)
        return row

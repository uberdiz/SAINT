"""
ui/settings_ui.py

Category-based Settings. Every field is bound to a config key; Save writes
them all to data/config.json and emits SETTINGS_CHANGED, which the runtime
applies live (wake word reload, microphone restart, TTS re-init, log level,
appearance) — no restart required for most settings.
"""

import os
import threading
from typing import Callable, List, Tuple

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFontComboBox,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSlider, QSpinBox,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.config import config
from core.events import event_bus, EventType
from core.module_manager import module_manager
from core.paths import PROJECT_ROOT, data_dir, display_path, resolve_project_path
from core.permissions import permission_manager
from ui.theme import current_palette
from ui.widgets import LevelMeter, run_async

ACCENTS = ["#feaa34", "#f97316", "#2563eb", "#7c3aed", "#db2777", "#dc2626", "#ea580c", "#ca8a04", "#16a34a", "#0891b2", "#64748b"]


class SettingsUI(QWidget):
    CATEGORIES = ["General", "Voice", "AI", "Wake Word", "Spotify", "Memory", "Automation",
                  "Desktop Control", "Appearance", "Advanced"]

    def __init__(self, on_appearance_changed: Callable = None, on_theme_changed: Callable = None):
        super().__init__()
        self.on_appearance_changed = on_appearance_changed or on_theme_changed
        self._bindings: List[Tuple[str, Callable, Callable]] = []
        self._labels_by_page = {}
        self._page = None
        self._build()
        self.load()
        event_bus.event_occurred.connect(self._on_event)

    # ------------------------------------------------------------------ #
    # Binding helpers
    # ------------------------------------------------------------------ #
    def _bind(self, key, getter, setter):
        self._bindings.append((key, getter, setter))

    def _check(self, key, text):
        w = QCheckBox(text)
        self._bind(key, w.isChecked, lambda v: w.setChecked(bool(v)))
        return w

    def _combo(self, key, items, editable=False, data=None):
        w = QComboBox()
        w.setEditable(editable)
        if data is None:
            w.addItems(items)
            self._bind(key, w.currentText, lambda v: w.setCurrentText(str(v if v is not None else "")))
        else:
            for label, value in zip(items, data):
                w.addItem(label, value)
            self._bind(key, w.currentData,
                       lambda v: w.setCurrentIndex(max(0, w.findData(v))))
        return w

    def _spin(self, key, lo, hi, step=1.0, decimals=0, suffix=""):
        w = QDoubleSpinBox() if decimals else QSpinBox()
        w.setRange(lo, hi)
        w.setSingleStep(step)
        if decimals:
            w.setDecimals(decimals)
        if suffix:
            w.setSuffix(suffix)
        conv = float if decimals else int
        self._bind(key, lambda: conv(w.value()), lambda v: w.setValue(conv(v if v is not None else lo)))
        return w

    def _line(self, key, placeholder="", password=False):
        w = QLineEdit()
        w.setPlaceholderText(placeholder)
        if password:
            w.setEchoMode(QLineEdit.Password)
        self._bind(key, lambda: w.text().strip(), lambda v: w.setText("" if v is None else str(v)))
        return w

    def _section(self, title, desc=""):
        box = QGroupBox(title.replace("&", "&&"))
        form = QFormLayout(box)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignLeft)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        if desc:
            d = QLabel(desc)
            d.setObjectName("Muted")
            d.setWordWrap(True)
            form.addRow(d)
        return box, form

    def _row(self, form, label, widget, help_text=""):
        if type(widget) is QWidget:
            widget.setObjectName("FormRow")   # transparent container for composite rows
        form.addRow(label, widget)
        self._labels_by_page.setdefault(self._page, []).append(label.lower())
        if help_text:
            h = QLabel(help_text)
            h.setObjectName("Faint")
            h.setWordWrap(True)
            form.addRow("", h)

    def _new_page(self, name):
        self._page = name
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(12)
        lay.setContentsMargins(4, 0, 12, 12)
        return w, lay

    def _add_page(self, w, lay):
        lay.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(w)
        self.stack.addWidget(scroll)

    # ------------------------------------------------------------------ #
    # Build
    # ------------------------------------------------------------------ #
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(36, 28, 36, 24)
        root.setSpacing(16)
        head = QHBoxLayout()
        t = QLabel("Settings")
        t.setObjectName("PageTitle")
        head.addWidget(t)
        head.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search settings…")
        self.search.setMaximumWidth(260)
        self.search.textChanged.connect(self._filter)
        head.addWidget(self.search)
        root.addLayout(head)

        body = QHBoxLayout()
        self.nav = QListWidget()
        self.nav.setObjectName("SettingsNav")
        self.nav.setFixedWidth(180)
        for c in self.CATEGORIES:
            QListWidgetItem(c, self.nav)
        self.nav.currentRowChanged.connect(lambda i: i >= 0 and self.stack.setCurrentIndex(i))
        body.addWidget(self.nav)
        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        for builder in (self._build_general, self._build_voice, self._build_ai, self._build_wake,
                        self._build_spotify, self._build_memory, self._build_automation,
                        self._build_desktop, self._build_appearance, self._build_advanced):
            builder()
        self.nav.setCurrentRow(0)

        foot = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("Muted")
        foot.addWidget(self.status, 1)
        revert = QPushButton("Revert")
        revert.clicked.connect(self.load)
        save = QPushButton("Save settings")
        save.setObjectName("Primary")
        save.clicked.connect(self.save)
        foot.addWidget(revert)
        foot.addWidget(save)
        root.addLayout(foot)

    # ---- GENERAL -------------------------------------------------------
    def _build_general(self):
        w, lay = self._new_page("General")
        box, f = self._section("Startup & background",
                               "SAINT's voice loop, reminders and Spotify keep running when the window is "
                               "closed to the tray or not focused.")
        self._row(f, "Start listening on launch", self._check("voice.auto_start", "Enabled"))
        self._row(f, "Close button", self._check("notifications.close_to_tray", "Keep running in the tray"))
        self._row(f, "Start minimized", self._check("notifications.start_minimized", "Start hidden in the tray"))
        self._row(f, "Notifications", self._check("notifications.tray", "Show tray notifications (reminders, errors)"))
        lay.addWidget(box)
        box, f = self._section("Modules", "Turn whole capabilities on or off.")
        for key, label in (("modules.voice", "Voice"), ("modules.memory", "Memory"),
                           ("modules.automation", "Automations"), ("modules.desktop", "Desktop control"),
                           ("modules.vision", "Screen awareness"), ("modules.spotify", "Spotify")):
            self._row(f, label, self._check(key, "Enabled"))
        lay.addWidget(box)
        self._add_page(w, lay)

    # ---- VOICE ------------------------------------------------------------
    def _build_voice(self):
        w, lay = self._new_page("Voice")
        box, f = self._section("Microphone")
        self.mic_combo = QComboBox()
        self._bind("voice.mic_device", self.mic_combo.currentData,
                   lambda v: self.mic_combo.setCurrentIndex(max(0, self.mic_combo.findData(v))))
        self._populate_mics()
        mic_row = QHBoxLayout()
        mic_row.addWidget(self.mic_combo, 1)
        self.mic_test = QPushButton("Test (3 s)")
        self.mic_test.clicked.connect(self._test_mic)
        mic_row.addWidget(self.mic_test)
        mw = QWidget()
        mw.setLayout(mic_row)
        self._row(f, "Input device", mw)
        self._row(f, "Mode", self._combo("voice.mode", ["Always on", "Push to talk"], data=["always_on", "push_to_talk"]))
        self._row(f, "Speech threshold", self._spin("voice.vad_start_threshold", 0.001, 0.2, 0.001, 3),
                  "Raise it if background noise starts utterances; lower it if quiet speech is missed.")
        self._row(f, "End-of-speech silence", self._spin("voice.silence_duration_ms", 200, 3000, 50, 0, " ms"))
        self._row(f, "Noise suppression", self._check("voice.noise_suppression", "Subtract the rolling noise floor"))
        self._row(f, "Speech detector", self._combo("voice.vad_backend", ["Silero (speech vs. music/noise)", "Energy (RMS)"],
                                                    data=["silero", "rms"]),
                  "Silero tells speech from music and TV, so commands end cleanly while music plays.")
        self._row(f, "Longest utterance", self._spin("voice.max_utterance_sec", 5.0, 60.0, 1.0, 0, " s"),
                  "Cut an utterance that never pauses (music vocals, TV) and process what was heard.")
        lay.addWidget(box)

        box, f = self._section("Interruptions (barge-in)",
                               "The microphone stays on while SAINT speaks. SAINT compares what the mic hears "
                               "with what it is playing, so its own voice doesn't interrupt it but yours does.")
        self._row(f, "Allow interrupting", self._check("voice.barge_in_enabled", "Enabled"))
        self._row(f, "Speech needed to interrupt", self._spin("voice.barge_in_min_ms", 90, 1500, 30, 0, " ms"))
        self._row(f, "Echo margin", self._spin("voice.barge_in_echo_margin", 1.0, 8.0, 0.1, 1),
                  "Higher = harder to interrupt (use with loud speakers); lower = easier (headphones).")
        lay.addWidget(box)

        box, f = self._section("Speech recognition (STT)")
        self._row(f, "Engine", self._combo("voice.stt_backend", ["faster_whisper", "mock"]))
        self._row(f, "Model", self._combo("voice.stt_model", ["tiny.en", "base.en", "small.en", "medium.en",
                                                              "large-v3", "distil-large-v3"], editable=True))
        self._row(f, "Device", self._combo("voice.stt_device", ["cuda", "cpu"]))
        self._row(f, "Precision", self._combo("voice.stt_compute_type", ["float16", "int8_float16", "int8", "float32"]))
        self._row(f, "Language", self._line("voice.stt_language", "en or auto"))
        self._row(f, "Min confidence (no wake word)", self._spin("voice.min_stt_confidence", 0.0, 1.0, 0.05, 2))
        lay.addWidget(box)

        box, f = self._section("Speech output (TTS)")
        self._row(f, "Engine", self._combo("voice.tts_backend", ["kokoro", "qwen", "mock"]))
        self._row(f, "Voice", self._combo("voice.tts_voice", ["af_heart", "af_bella", "af_nicole", "af_sky",
                                                              "am_adam", "am_michael", "bf_emma", "bm_george"],
                                          editable=True))
        self._row(f, "Device", self._combo("voice.tts_device", ["cuda", "cpu", "auto"]))
        self._row(f, "Speed", self._spin("voice.tts_speed", 0.5, 2.0, 0.05, 2))
        self._row(f, "CPU fallback", self._check("voice.tts_allow_cpu_fallback", "Use CPU if CUDA is unavailable"))
        lay.addWidget(box)
        self._add_page(w, lay)

    def _populate_mics(self):
        self.mic_combo.clear()
        self.mic_combo.addItem("System default", None)
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            for i, dev in enumerate(devices):
                if dev["max_input_channels"] > 0 and dev.get("hostapi", 0) == 0:
                    self.mic_combo.addItem(f"{dev['name']}", i)
            current = config.get("voice.mic_device", None)
            if current is not None and self.mic_combo.findData(current) < 0 and 0 <= int(current) < len(devices):
                self.mic_combo.addItem(f"{devices[int(current)]['name']} (#{current})", int(current))
        except Exception as e:
            self.mic_combo.addItem(f"(couldn't list devices: {e})", None)

    def _test_mic(self):
        dev = self.mic_combo.currentData()
        self.mic_test.setEnabled(False)
        self.mic_test.setText("Recording…")

        def run():
            import sounddevice as sd
            info = sd.query_devices(dev, "input")
            fs = int(info["default_samplerate"])
            rec = sd.rec(int(3 * fs), samplerate=fs, channels=1, dtype="int16", device=dev)
            sd.wait()
            sd.play(rec, fs)
            sd.wait()
            import numpy as np
            return float(np.sqrt(np.mean((rec.astype("float32") / 32768) ** 2)))

        def done(rms):
            self.mic_test.setEnabled(True)
            self.mic_test.setText("Test (3 s)")
            self.status.setText(f"Mic test finished — average level {rms:.3f}"
                                + (" (very quiet — check the input device)" if rms < 0.003 else ""))

        def fail(e):
            self.mic_test.setEnabled(True)
            self.mic_test.setText("Test (3 s)")
            self.status.setText(f"Mic test failed: {e}")
        run_async(run, done, fail)

    # ---- AI --------------------------------------------------------------
    def _build_ai(self):
        w, lay = self._new_page("AI")
        box, f = self._section("Language model")
        self._row(f, "Provider", self._combo("ai.provider", ["ollama", "openai", "mock"]))
        self.model_combo = self._combo("ai.model", [config.get("ai.model", "llama3.1")], editable=True)
        mr = QHBoxLayout()
        mr.addWidget(self.model_combo, 1)
        refresh = QPushButton("Refresh list")
        refresh.clicked.connect(self._refresh_models)
        mr.addWidget(refresh)
        mw = QWidget()
        mw.setLayout(mr)
        self._row(f, "Model", mw, "Models with the “tools” capability (e.g. llama3.1, llama3.2) can call "
                                  "SAINT's tools for requests the fast command router doesn't cover.")
        self._row(f, "Base URL", self._line("ai.base_url", "http://localhost:11434"))
        self._row(f, "API key", self._line("ai.api_key", "only for OpenAI-compatible servers", password=True))
        self._row(f, "Temperature", self._spin("ai.temperature", 0.0, 2.0, 0.1, 2))
        self._row(f, "Timeout", self._spin("ai.timeout_seconds", 5, 300, 5, 0, " s"))
        test = QPushButton("Test connection")
        self.ai_status = QLabel("")
        test.clicked.connect(self._test_ai)
        tr = QHBoxLayout()
        tr.addWidget(test)
        tr.addWidget(self.ai_status, 1)
        tw = QWidget()
        tw.setObjectName("FormRow")
        tw.setLayout(tr)
        f.addRow("", tw)
        lay.addWidget(box)
        box, f = self._section("Agent")
        self._row(f, "Command router", self._check("agent.enabled", "Handle common commands directly with tools"))
        self._row(f, "LLM tool calling", self._check("ai.tool_calling", "Let the model call SAINT's tools"))
        self._row(f, "Max tool steps", self._spin("ai.max_tool_steps", 1, 8))
        self._row(f, "Confirmation timeout", self._spin("agent.confirm_timeout_sec", 5, 120, 5, 0, " s"))
        self._row(f, "Context turns", self._spin("voice.max_context_turns", 1, 20))
        self._row(f, "Max reply tokens", self._spin("ai.max_tokens", 0, 2048, 10, 0),
                  "Hard cap on generated tokens. 0 = no cap. Keep low (120–200) for a voice assistant.")
        self.system_prompt = QPlainTextEdit()
        self.system_prompt.setMaximumHeight(120)
        self._bind("voice.system_prompt", lambda: self.system_prompt.toPlainText().strip(),
                   lambda v: self.system_prompt.setPlainText(v or ""))
        self._row(f, "Personality / system prompt", self.system_prompt)
        lay.addWidget(box)

        box, f = self._section("Vision",
                               "SAINT can look at your screen with a local vision model. Windows still "
                               "provides the reliable structured window/UI info; vision fills in what "
                               "isn't exposed to accessibility APIs.")
        self._row(f, "Backend",
                  self._combo("vision.analyzer", ["none", "ollama", "flux"]),
                  "'flux' = FLUX.2 Klein 4B (local). 'ollama' = any vision-capable Ollama model.")
        self._row(f, "Ollama vision model", self._line("vision.model", "e.g. llama3.2-vision"))
        self._row(f, "FLUX model id", self._line("vision.flux_model_id", "black-forest-labs/FLUX.2-Klein-4B"))
        self._row(f, "FLUX weights dir", self._line("vision.flux_model_dir", "data/vision/flux2-klein-4b"),
                  "Download once: hf download black-forest-labs/FLUX.2-Klein-4B --local-dir <this dir>")
        self._row(f, "FLUX device", self._combo("vision.flux_device", ["auto", "cuda", "cpu"]))
        self._row(f, "FLUX dtype", self._combo("vision.flux_dtype", ["float16", "bfloat16", "float32"]))
        self._row(f, "Max image side", self._spin("vision.flux_max_image_side", 256, 2048, 64, 0, " px"))
        self._row(f, "Max reply tokens (vision)", self._spin("vision.flux_max_new_tokens", 32, 512, 16))
        self._row(f, "Allow screen context",
                  self._check("vision.allow_screen_context", "Read windows/controls/text via Windows accessibility APIs"))
        self._row(f, "Keep screenshots", self._spin("vision.keep_screenshots", 0, 200))
        lay.addWidget(box)

        box, f = self._section("Web / current information",
                               "Only used when the user asks about current facts or news. Explicit "
                               "'search Google for X' commands still use browser automation.")
        self._row(f, "Provider", self._combo(
            "web.provider", ["duckduckgo_browser", "duckduckgo", "tavily", "serpapi", "none"]),
                  "'duckduckgo_browser' opens DuckDuckGo in your browser (no key). The others "
                  "return a spoken summary via API.")
        self._row(f, "API key", self._line("web.api_key", "for tavily / serpapi", password=True))
        self._row(f, "Max results", self._spin("web.max_results", 1, 10))
        self._row(f, "Answer style", self._combo("web.answer_style", ["concise", "detailed"]))
        lay.addWidget(box)
        self._add_page(w, lay)

    def _refresh_models(self):
        ai = module_manager.get("ai")

        def done(models):
            cur = self.model_combo.currentText()
            self.model_combo.clear()
            names = [m.get("name") for m in models if m.get("name")]
            self.model_combo.addItems(names or [cur])
            self.model_combo.setCurrentText(cur)
            self.ai_status.setText(f"{len(names)} models installed" if names else "No models found")
        run_async(lambda: ai.list_models(force_refresh=True), done, lambda e: self.ai_status.setText(str(e)))

    def _test_ai(self):
        ai = module_manager.get("ai")
        self.ai_status.setText("Testing…")
        pal = current_palette()

        def done(r):
            ok = r.get("connected")
            self.ai_status.setText(f"Connected ({r.get('version', r.get('provider', ''))})" if ok
                                   else f"Not reachable: {r.get('error', '')}")
            self.ai_status.setStyleSheet(f"color:{pal.success if ok else pal.danger};")
        run_async(ai.test_connection, done, lambda e: self.ai_status.setText(str(e)))

    # ---- WAKE WORD ---------------------------------------------------------
    def _build_wake(self):
        w, lay = self._new_page("Wake Word")
        box, f = self._section("Wake word",
                               "SAINT listens locally for “Hey SAINT” with its own ONNX model (CPU, no "
                               "cloud). Only after hearing it does SAINT transcribe what you say.")
        self._row(f, "Wake word", self._check("voice.wake_word_enabled", "Require “Hey SAINT” before commands"))
        self.wake_status = QLabel("")
        self.wake_status.setWordWrap(True)
        self._row(f, "Status", self.wake_status)
        self.wake_meter = LevelMeter(show_threshold=True)
        self._row(f, "Live score", self.wake_meter)
        path = self._line("voice.wake_word_model_path", "data/wake/hey_saint.onnx")
        pr = QHBoxLayout()
        pr.addWidget(path, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: self._browse_model(path))
        pr.addWidget(browse)
        pw = QWidget()
        pw.setLayout(pr)
        self._row(f, "Model file", pw, "Relative paths are resolved from the SAINT folder.")
        lay.addWidget(box)

        box, f = self._section("Detection")
        self.sens = QSlider(Qt.Horizontal)
        self.sens.setRange(5, 95)
        self.sens_label = QLabel("")
        self.sens.valueChanged.connect(lambda v: self.sens_label.setText(
            f"{v}%  (score threshold {(100 - v) / 100:.2f})"))
        sr = QHBoxLayout()
        sr.addWidget(self.sens, 1)
        sr.addWidget(self.sens_label)
        sw = QWidget()
        sw.setLayout(sr)
        self._bind("voice.wake_word_threshold", lambda: round((100 - self.sens.value()) / 100, 2),
                   lambda v: self.sens.setValue(int(round(100 - float(v or 0.5) * 100))))
        self._row(f, "Sensitivity", sw, "Higher sensitivity triggers more easily (and more falsely).")
        self._row(f, "Confirmation frames", self._spin("voice.wake_word_trigger_frames", 1, 6),
                  "Frames above the threshold needed within the detection window (1 = most responsive).")
        self._row(f, "Detection window", self._spin("voice.wake_word_window_frames", 1, 12, 1, 0, " × 80 ms"),
                  "Scores are judged over this window, so a short “Hey SAINT” with a dip still counts.")
        self._row(f, "Transcript check", self._check("voice.wake_word_transcript_check",
                                                      "Also recognise “SAINT” from speech-to-text"),
                  "The wake model is strongest on “Hey SAINT”. With this on, a short utterance it missed is "
                  "transcribed once and accepted only if it starts with “SAINT” / “Hey SAINT”.")
        self._row(f, "Transcript check limit", self._spin("voice.wake_word_transcript_max_sec", 2.0, 15.0, 0.5, 1, " s"),
                  "Longer background speech is never transcribed.")
        self._row(f, "Cooldown", self._spin("voice.wake_word_refractory_sec", 0.5, 10.0, 0.5, 1, " s"))
        self._row(f, "Command timeout", self._spin("voice.wake_word_command_timeout_sec", 2.0, 20.0, 0.5, 1, " s"),
                  "How long SAINT waits for a command after hearing its name.")
        self._row(f, "Conversation window", self._spin("voice.wake_word_followup_sec", 0.0, 120.0, 1.0, 0, " s"),
                  "After SAINT answers, keep listening this long for follow-ups (“skip that”, “turn it "
                  "down”) without the wake word. Counted from when SAINT stops talking. 0 = off.")
        self._row(f, "Follow-up confidence", self._spin("voice.followup_min_confidence", 0.0, 1.0, 0.05, 2),
                  "Minimum speech-recognition confidence for follow-ups (short commands score low).")
        self._row(f, "Chime", self._check("voice.wake_word_chime", "Play a short tone when SAINT hears its name"))
        self._row(f, "Debug", self._check("voice.wake_word_debug_scores", "Log every score above 0.1"))
        lay.addWidget(box)
        self._add_page(w, lay)

    def _browse_model(self, line):
        path, _ = QFileDialog.getOpenFileName(self, "Wake-word model", str(PROJECT_ROOT / "data" / "wake"),
                                              "ONNX models (*.onnx)")
        if path:
            line.setText(display_path(path).replace("\\", "/"))

    def _render_wake_status(self):
        pal = current_palette()
        st = module_manager.get("voice").wake_status()
        if not st.get("enabled"):
            self.wake_status.setText("Off")
            self.wake_status.setStyleSheet(f"color:{pal.muted};")
        elif st.get("ready"):
            self.wake_status.setText(f"Ready · {st.get('model_path')} · {st.get('avg_infer_ms', 0)} ms per frame")
            self.wake_status.setStyleSheet(f"color:{pal.success};")
        else:
            self.wake_status.setText(st.get("error") or "Not loaded")
            self.wake_status.setStyleSheet(f"color:{pal.danger};")
        self.wake_meter.set_threshold(config.get("voice.wake_word_threshold", 0.5))

    # ---- SPOTIFY ---------------------------------------------------------------
    def _build_spotify(self):
        w, lay = self._new_page("Spotify")
        box, f = self._section("Account", "SAINT uses Spotify's Web API with your own developer app "
                                          "(Client ID) and a secure PKCE login; tokens are stored in the "
                                          "Windows Credential Manager, never in files.")
        self._row(f, "Spotify", self._check("modules.spotify", "Enable Spotify control"))
        self._row(f, "Client ID", self._line("spotify.client_id", "from developer.spotify.com/dashboard"))
        self._row(f, "Redirect URI", self._line("spotify.redirect_uri", "http://127.0.0.1:8888/callback"),
                  "Add exactly this URI to your Spotify app's Redirect URIs.")
        self.sp_status = QLabel("")
        self.sp_connect = QPushButton("Connect Spotify")
        self.sp_connect.setObjectName("Primary")
        self.sp_connect.clicked.connect(self._spotify_connect)
        self.sp_disconnect = QPushButton("Disconnect")
        self.sp_disconnect.clicked.connect(self._spotify_disconnect)
        r = QHBoxLayout()
        r.addWidget(self.sp_status, 1)
        r.addWidget(self.sp_disconnect)
        r.addWidget(self.sp_connect)
        rw = QWidget()
        rw.setObjectName("FormRow")
        rw.setLayout(r)
        f.addRow("", rw)
        lay.addWidget(box)

        box, f = self._section("Playback")
        self.sp_device = QComboBox()
        self.sp_device.setEditable(True)
        self._bind("spotify.preferred_device", lambda: self.sp_device.currentText().strip(),
                   lambda v: self.sp_device.setCurrentText(v or ""))
        dr = QHBoxLayout()
        dr.addWidget(self.sp_device, 1)
        ref = QPushButton("Find devices")
        ref.clicked.connect(self._spotify_devices)
        dr.addWidget(ref)
        dw = QWidget()
        dw.setLayout(dr)
        self._row(f, "Preferred device", dw, "Used when no device is active. Leave empty to prefer this PC.")
        self._row(f, "Wake a device", self._check("spotify.auto_device",
                                                   "If nothing is playing anywhere, activate a device (or open Spotify)"))
        self._row(f, "Volume step", self._spin("spotify.volume_step", 5, 50, 5, 0, " %"))
        self._row(f, "Hands-free", self._check("voice.music_hotwords",
                                               "While music plays, “skip”, “pause”, “louder”… need no wake word"),
                  "Only a short playback command on its own counts — lyrics and chatter are ignored.")
        self._row(f, "Mini player", self._check("widgets.spotify", "Show the floating always-on-top player"))
        lay.addWidget(box)

        box, f = self._section("Music memory", "Used for “play something I'd like” and “what did I listen to today”.")
        self._row(f, "Listening history", self._check("spotify.track_history",
                                                      "Remember what I play, skip and request"))
        self._row(f, "Sync interval", self._spin("spotify.poll_interval_sec", 5, 120, 5, 0, " s"))
        clear = QPushButton("Clear music memory…")
        clear.setObjectName("Danger")
        clear.clicked.connect(self._clear_music_memory)
        f.addRow("", clear)
        lay.addWidget(box)
        self._add_page(w, lay)

    def _refresh_spotify_status(self):
        sp = module_manager.get("spotify")
        pal = current_palette()
        ok, reason = sp.availability()
        self.sp_status.setText("Connected" if ok else reason)
        self.sp_status.setStyleSheet(f"color:{pal.success if ok else pal.muted};")
        self.sp_disconnect.setEnabled(sp.is_connected())

    def _spotify_connect(self):
        config.set("spotify.client_id", self._value("spotify.client_id"), persist=True)
        config.set("spotify.redirect_uri", self._value("spotify.redirect_uri"), persist=True)
        module_manager.set_enabled("spotify", True)
        sp = module_manager.get("spotify")
        self.sp_connect.setEnabled(False)
        self.sp_status.setText("Waiting for you to log in to Spotify in the browser…")

        def done(_):
            self.sp_connect.setEnabled(True)
            self._refresh_spotify_status()

        def fail(e):
            self.sp_connect.setEnabled(True)
            self._refresh_spotify_status()
            QMessageBox.warning(self, "Spotify", e)
        run_async(sp.connect, done, fail)

    def _spotify_disconnect(self):
        module_manager.get("spotify").disconnect()
        self._refresh_spotify_status()

    def _spotify_devices(self):
        sp = module_manager.get("spotify")

        def done(d):
            cur = self.sp_device.currentText()
            self.sp_device.clear()
            self.sp_device.addItem("")
            for dev in (d or {}).get("devices", []):
                self.sp_device.addItem(dev.get("name", ""))
            self.sp_device.setCurrentText(cur)
        run_async(sp.client.devices, done, lambda e: self.sp_status.setText(f"Couldn't list devices: {e}"))

    def _clear_music_memory(self):
        if QMessageBox.question(self, "Clear music memory",
                                "Delete SAINT's Spotify listening history, skips, requests and feedback?") \
                == QMessageBox.Yes:
            module_manager.get("spotify").tools.memory.clear()
            self.status.setText("Music memory cleared.")

    # ---- MEMORY ----------------------------------------------------------------
    def _build_memory(self):
        w, lay = self._new_page("Memory")
        box, f = self._section("Long-term memory",
                               "SAINT remembers facts you tell it (“my favorite language is Python”) in a local "
                               "database and answers only from what is stored. Manage entries on the Memory page.")
        self._row(f, "Memory", self._check("memory.enabled", "Enabled"))
        self._row(f, "Learn from statements", self._check("memory.auto_extract",
                                                          "Store clear personal statements automatically"))
        self._row(f, "Use in answers", self._check("memory.inject_context",
                                                   "Give the language model relevant memories"))
        self._row(f, "Memories per answer", self._spin("memory.max_context_items", 1, 20))
        wipe = QPushButton("Delete all long-term memories…")
        wipe.setObjectName("Danger")
        wipe.clicked.connect(self._wipe_memory)
        f.addRow("", wipe)
        lay.addWidget(box)
        box, f = self._section("History", "A private log of what you asked and what SAINT did, shown on the History "
                                          "page. It is stored only in data/history.jsonl on this PC and never uploaded.")
        self._row(f, "History", self._check("history.enabled", "Keep a local history of requests"))
        self._row(f, "Keep up to", self._spin("history.max_entries", 100, 100000, 500, 0, " entries"))
        lay.addWidget(box)
        self._add_page(w, lay)

    def _wipe_memory(self):
        if QMessageBox.question(self, "Delete memories", "Permanently delete everything SAINT remembers about you?") \
                == QMessageBox.Yes:
            from modules.memory.service import memory_service
            n = memory_service.forget_all()
            self.status.setText(f"Deleted {n} memories.")

    # ---- AUTOMATION -------------------------------------------------------------
    def _build_automation(self):
        w, lay = self._new_page("Automation")
        box, f = self._section("Reminders & automations")
        self._row(f, "Automations", self._check("automation.enabled", "Run scheduled reminders and commands"))
        self._row(f, "Speak reminders", self._check("automation.speak_reminders", "Say reminders out loud"))
        self._row(f, "Missed reminders", self._spin("automation.missed_grace_hours", 0, 72, 1, 0, " h"),
                  "One-time reminders missed while SAINT was closed are delivered on startup if they're "
                  "no older than this.")
        self._row(f, "“Every morning” means", self._line("automation.default_morning_time", "08:00"))
        lay.addWidget(box)
        box, f = self._section("Safety", "Controls how freely SAINT may act.")
        self._row(f, "Permission mode", self._combo(
            "automation.permission_mode", ["Safe — read-only actions only", "Confirm — ask before risky actions",
                                           "Autonomous — act without asking"], data=["safe", "confirm", "autonomous"]))
        self._row(f, "Dangerous actions", self._check("automation.confirm_dangerous",
                                                      "Always ask before high-risk actions"))
        lay.addWidget(box)
        self._add_page(w, lay)

    # ---- DESKTOP CONTROL -----------------------------------------------------------
    def _build_desktop(self):
        w, lay = self._new_page("Desktop Control")
        box, f = self._section("Permissions",
                               "SAINT controls the desktop only through explicit, validated tools — never "
                               "arbitrary commands. Emergency stop: slam the mouse into a screen corner.")
        self._row(f, "Desktop control", self._check("desktop.enabled", "Enabled"))
        self._row(f, "Open apps", self._check("desktop.allow_app_launch", "Allowed"))
        self._row(f, "Switch / move / close windows", self._check("desktop.allow_window_control", "Allowed"))
        self._row(f, "Keyboard", self._check("desktop.allow_keyboard", "Allowed"))
        self._row(f, "Mouse & clicking", self._check("desktop.allow_mouse", "Allowed"))
        self._row(f, "Closing apps", self._check("desktop.confirm_close_apps", "Ask before closing an app"))
        self._row(f, "Several matching windows", self._combo(
            "desktop.multi_window_policy", ["Ask which one", "Use the most recent"], data=["ask", "recent"]),
                  "e.g. three browser windows are open and none is clearly meant.")
        self._row(f, "Max typed text", self._spin("desktop.max_type_length", 20, 5000, 20, 0, " chars"))
        lay.addWidget(box)

        box = QGroupBox("App shortcuts")
        v = QVBoxLayout(box)
        d = QLabel("Extra names SAINT can open, e.g. “notes” → C:/Tools/Obsidian.exe or a URI like steam://.")
        d.setObjectName("Muted")
        d.setWordWrap(True)
        v.addWidget(d)
        self.apps_table = QTableWidget(0, 2)
        self.apps_table.setHorizontalHeaderLabels(["Name", "Path or URI"])
        self.apps_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.apps_table.verticalHeader().setVisible(False)
        self.apps_table.setMaximumHeight(150)
        v.addWidget(self.apps_table)
        br = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(lambda: self.apps_table.insertRow(self.apps_table.rowCount()))
        rem = QPushButton("Remove")
        rem.clicked.connect(lambda: self.apps_table.removeRow(self.apps_table.currentRow()))
        br.addWidget(add)
        br.addWidget(rem)
        br.addStretch()
        v.addLayout(br)
        self._bind("desktop.apps", self._apps_value, self._apps_load)
        lay.addWidget(box)

        box = QGroupBox("Per-tool access")
        v = QVBoxLayout(box)
        d = QLabel("Override the permission mode for individual tools.")
        d.setObjectName("Muted")
        v.addWidget(d)
        self.perm_table = QTableWidget(0, 3)
        self.perm_table.setHorizontalHeaderLabels(["Tool", "Risk", "Access"])
        self.perm_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.perm_table.verticalHeader().setVisible(False)
        self.perm_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.perm_table.setMinimumHeight(260)
        v.addWidget(self.perm_table)
        self._bind("permissions.overrides", self._perm_value, self._perm_load)
        lay.addWidget(box)
        self._add_page(w, lay)

    def _apps_value(self):
        out = {}
        for r in range(self.apps_table.rowCount()):
            k = self.apps_table.item(r, 0)
            v = self.apps_table.item(r, 1)
            if k and v and k.text().strip() and v.text().strip():
                out[k.text().strip()] = v.text().strip()
        return out

    def _apps_load(self, apps):
        self.apps_table.setRowCount(0)
        for k, v in (apps or {}).items():
            r = self.apps_table.rowCount()
            self.apps_table.insertRow(r)
            self.apps_table.setItem(r, 0, QTableWidgetItem(k))
            self.apps_table.setItem(r, 1, QTableWidgetItem(v))

    def _perm_load(self, overrides):
        from modules.automation.tools import get_tool_registry
        overrides = overrides or {}
        tools = sorted(get_tool_registry().list_tools(), key=lambda t: t.name)
        self.perm_table.setRowCount(0)
        for t in tools:
            r = self.perm_table.rowCount()
            self.perm_table.insertRow(r)
            self.perm_table.setItem(r, 0, QTableWidgetItem(t.name))
            self.perm_table.setItem(r, 1, QTableWidgetItem(t.permission.value))
            combo = QComboBox()
            combo.addItems(["default", "allow", "confirm", "deny"])
            combo.setCurrentText(overrides.get(t.name, "default"))
            self.perm_table.setCellWidget(r, 2, combo)

    def _perm_value(self):
        out = {k: v for k, v in (config.get("permissions.overrides", {}) or {}).items() if "." not in k}
        for r in range(self.perm_table.rowCount()):
            combo = self.perm_table.cellWidget(r, 2)
            if combo and combo.currentText() != "default":
                out[self.perm_table.item(r, 0).text()] = combo.currentText()
        return out

    # ---- APPEARANCE ------------------------------------------------------------------
    def _build_appearance(self):
        w, lay = self._new_page("Appearance")
        box, f = self._section("Theme")
        self._row(f, "Mode", self._combo("appearance.theme", ["Dark", "Light", "System"]))
        self.accent = "#feaa34"
        sw = QHBoxLayout()
        self._swatches = []
        for c in ACCENTS:
            b = QPushButton("")
            b.setObjectName("Swatch")
            b.setStyleSheet(f"background:{c}; border:2px solid transparent;")
            b.clicked.connect(lambda _=False, col=c: self._set_accent(col))
            sw.addWidget(b)
            self._swatches.append((c, b))
        custom = QPushButton("Custom…")
        custom.clicked.connect(self._pick_accent)
        sw.addWidget(custom)
        sw.addStretch()
        swd = QWidget()
        swd.setLayout(sw)
        self._bind("appearance.accent", lambda: self.accent, self._set_accent)
        self._row(f, "Accent colour", swd)
        self.font_combo = QFontComboBox()
        self._bind("appearance.font_family", lambda: self.font_combo.currentFont().family(),
                   lambda v: self.font_combo.setCurrentFont(__import__("PySide6.QtGui", fromlist=["QFont"]).QFont(v or "Segoe UI")))
        self._row(f, "Font", self.font_combo)
        self._row(f, "Font size", self._spin("appearance.font_size", 10, 20, 1, 0, " px"))
        self._row(f, "Density", self._check("appearance.compact", "Compact"))
        self._row(f, "Animations", self._check("appearance.animations", "Animate the orb, Halo and transitions"))
        lay.addWidget(box)

        box, f = self._section("Window")
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(60, 100)
        self._bind("appearance.opacity", lambda: self.opacity.value() / 100,
                   lambda v: self.opacity.setValue(int(round(float(v or 1.0) * 100))))
        self._row(f, "Opacity", self.opacity)
        self._row(f, "Always on top", self._check("appearance.always_on_top", "Keep SAINT above other windows"))
        self._row(f, "Sidebar", self._check("appearance.sidebar_labels", "Show labels"))
        lay.addWidget(box)

        box, f = self._section("Halo & overlay",
                               "The Halo is a soft light that travels around your screen edge while SAINT works in "
                               "the background. Rest the cursor at the top-centre edge, or press the hotkey, to open "
                               "the overlay.")
        self._row(f, "Halo", self._combo("overlay.halo", ["When SAINT is minimized", "Always", "Off"],
                                         data=["minimized", "always", "off"]))
        self._row(f, "Screens", self._check("overlay.halo_all_screens", "Show on every monitor"))
        self._row(f, "Edge tab", self._check("overlay.edge_tab", "Reveal a SAINT tab at the top edge"))
        self._row(f, "Overlay hotkey", self._line("overlay.hotkey", "alt+`"),
                  "Works from anywhere. Combine ctrl / alt / shift / win with a key, e.g. alt+` or ctrl+alt+s.")
        lay.addWidget(box)
        self._add_page(w, lay)

    def _set_accent(self, color):
        c = QColor(color or "#feaa34")
        self.accent = c.name() if c.isValid() else "#feaa34"
        for col, b in self._swatches:
            border = "#ffffff" if col.lower() == self.accent.lower() else "transparent"
            b.setStyleSheet(f"background:{col}; border:2px solid {border};")

    def _pick_accent(self):
        c = QColorDialog.getColor(QColor(self.accent), self, "Accent colour")
        if c.isValid():
            self._set_accent(c.name())

    # ---- ADVANCED ------------------------------------------------------------------
    def _build_advanced(self):
        w, lay = self._new_page("Advanced")
        box, f = self._section("Logging & diagnostics")
        self._row(f, "Log level", self._combo("logging.level", ["Verbose", "Normal", "Errors Only"]))
        self._row(f, "Debug mode", self._check("logging.debug", "Log detailed subsystem debug output"))
        self._row(f, "Analytics", self._check("analytics.enabled", "Record local latency statistics"))
        logs = QPushButton("Open log folder")
        logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(data_dir() / "logs"))))
        data = QPushButton("Open data folder")
        data.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(data_dir()))))
        r = QHBoxLayout()
        r.addWidget(logs)
        r.addWidget(data)
        r.addStretch()
        rw = QWidget()
        rw.setObjectName("FormRow")
        rw.setLayout(r)
        f.addRow("", rw)
        lay.addWidget(box)

        box, f = self._section("Screen awareness",
                               "SAINT reads the active window through Windows UI Automation. For image-level "
                               "understanding, pull a vision model in Ollama (e.g. llama3.2-vision) and set it here.")
        self._row(f, "Visual analyzer", self._combo("vision.analyzer", ["none", "ollama"]))
        self._row(f, "Vision model", self._line("vision.model", "e.g. llama3.2-vision"))
        self._row(f, "Keep screenshots", self._spin("vision.keep_screenshots", 0, 200))
        self._row(f, "Screen reading", self._check("vision.allow_screen_context",
                                                    "Let SAINT read windows, buttons and text on screen"),
                  "Uses Windows accessibility info on demand only — nothing is captured in the background.")
        lay.addWidget(box)

        box, f = self._section("Files")
        lbl = QLabel(f"Config: {display_path(config.path)}\nData: {display_path(data_dir())}")
        lbl.setObjectName("Muted")
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f.addRow(lbl)
        self._row(f, "Startup check", self._check("system.startup_check", "Run the environment check on launch"))
        lay.addWidget(box)
        self._add_page(w, lay)

    # ------------------------------------------------------------------ #
    # Load / save
    # ------------------------------------------------------------------ #
    def _value(self, key):
        for k, getter, _ in self._bindings:
            if k == key:
                return getter()
        return config.get(key)

    def load(self):
        for key, _getter, setter in self._bindings:
            try:
                setter(config.get(key))
            except Exception:
                pass
        self._render_wake_status()
        self._refresh_spotify_status()
        self.status.setText("")

    def save(self):
        before_modules = {k: config.get(f"modules.{k}", False) for k in
                          ("voice", "memory", "automation", "desktop", "vision", "spotify")}
        errors = []
        for key, getter, _ in self._bindings:
            try:
                config.set(key, getter(), persist=False)
            except Exception as e:
                errors.append(f"{key}: {e}")
        config.set("theme", config.get("appearance.theme", "Dark"), persist=False)
        try:
            config.save()
        except OSError as e:
            self.status.setText(f"Couldn't write settings: {e}")
            return
        for k, was in before_modules.items():
            now = config.get(f"modules.{k}", False)
            if now != was:
                module_manager.set_enabled(k, now)
        event_bus.emit_event(EventType.SETTINGS_CHANGED, {})
        if self.on_appearance_changed:
            self.on_appearance_changed()
        pal = current_palette()
        if errors:
            self.status.setText("Saved with problems: " + "; ".join(errors))
            self.status.setStyleSheet(f"color:{pal.warning};")
        else:
            self.status.setText("Settings saved and applied.")
            self.status.setStyleSheet(f"color:{pal.success};")
        QTimer.singleShot(1500, self._render_wake_status)

    # ------------------------------------------------------------------ #
    def _filter(self, text):
        q = text.strip().lower()
        first = None
        for i, cat in enumerate(self.CATEGORIES):
            match = not q or q in cat.lower() or any(q in l for l in self._labels_by_page.get(cat, []))
            self.nav.item(i).setHidden(not match)
            if match and first is None:
                first = i
        if q and first is not None:
            self.nav.setCurrentRow(first)

    def _on_event(self, ev):
        if ev.type in (EventType.WAKE_STATUS, EventType.WAKE_ERROR):
            self._render_wake_status()
        elif ev.type == EventType.VOICE_WAKE_SCORE and self.isVisible():
            self.wake_meter.set_value(ev.payload.get("score", 0.0))
        elif ev.type in (EventType.SPOTIFY_CONNECTED, EventType.SPOTIFY_DISCONNECTED):
            self._refresh_spotify_status()

    def showEvent(self, e):
        super().showEvent(e)
        self._render_wake_status()
        self._refresh_spotify_status()

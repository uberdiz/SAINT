"""
ui/voice_ui.py

Voice page — Milestone 1.

Shows:
  - Live audio waveform / level meter
  - Mode toggle (Always On / Push-to-Talk)
  - Mic selector
  - Real-time transcription (partial text fades in)
  - SAINT response area (tokens stream in live)
  - Interrupt button + status indicator
  - Sensitivity / silence duration sliders
"""

from PySide6.QtCore import QThread
import time

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QComboBox, QSlider, QFrame, QSizePolicy,
    QGraphicsOpacityEffect,
)
from PySide6.QtCore import Qt, QTimer, Signal, QPropertyAnimation
from PySide6.QtGui import QFont

from core.events import event_bus, EventType
from core.config import config
from core.module_manager import module_manager
from core.conversation import get_controller, init_controller, ConvState
from modules.voice.tts import make_tts


class MicTestWorker(QThread):
    status_update = Signal(str)
    finished_ok = Signal()
    finished_error = Signal(str)

    def __init__(self, device_idx, parent=None):
        super().__init__(parent)
        self.device_idx = device_idx

    def run(self):
        try:
            import sounddevice as sd
            import numpy as np

            # Record 3 seconds
            duration = 3.0
            fs = 16000
            self.status_update.emit("Recording...")
            recording = sd.rec(int(duration * fs), samplerate=fs, channels=1, dtype='int16', device=self.device_idx)
            sd.wait()

            # Playback on default device
            self.status_update.emit("Playing...")
            sd.play(recording, samplerate=fs)
            sd.wait()

            self.finished_ok.emit()
        except Exception as e:
            self.finished_error.emit(str(e))


# ---------------------------------------------------------------------------
# Waveform meter widget
# ---------------------------------------------------------------------------
class WaveformMeter(QFrame):
    """Animated horizontal bar that shows live audio level."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("WaveformMeter")
        self.setFixedHeight(48)
        self._level = 0.0
        self._bars = 32

    def set_level(self, level: float):
        self._level = max(0.0, min(1.0, level))
        self.update()

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QColor, QLinearGradient, QBrush
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        bar_w = max(2, w // self._bars - 1)
        spacing = max(1, (w - self._bars * bar_w) // self._bars)

        for i in range(self._bars):
            # Create a "spectrum" feel with slight variation per bar
            bar_level = self._level * (0.6 + 0.4 * abs(self._bars // 2 - i) / (self._bars // 2))
            bar_h = int(bar_level * h)

            x = i * (bar_w + spacing)
            y = (h - bar_h) // 2

            # Gradient: green → yellow → red
            hue = int(120 * (1.0 - self._level))
            color = QColor.fromHsl(hue, 200, 120)
            painter.fillRect(x, y, bar_w, bar_h, color)
        painter.end()


# ---------------------------------------------------------------------------
# Status pill widget
# ---------------------------------------------------------------------------
class StatusPill(QLabel):
    def __init__(self, text="Idle", parent=None):
        super().__init__(text, parent)
        self.setObjectName("StatusPill")
        self.setAlignment(Qt.AlignCenter)
        self.setFixedWidth(120)
        self.set_idle()

    def set_idle(self):
        self.setText("Idle")
        self.setStyleSheet(
            "background:#2a2e35; color:#9aa0a6; border-radius:12px; padding:4px 10px; font-weight:600;"
        )

    def set_listening(self):
        self.setText("Listening")
        self.setStyleSheet(
            "background:#1a4a2e; color:#3ddc84; border-radius:12px; padding:4px 10px; font-weight:600;"
        )

    def set_thinking(self):
        self.setText("Thinking")
        self.setStyleSheet(
            "background:#1a2e4a; color:#60a5fa; border-radius:12px; padding:4px 10px; font-weight:600;"
        )

    def set_speaking(self):
        self.setText("Speaking")
        self.setStyleSheet(
            "background:#4a2e1a; color:#fb923c; border-radius:12px; padding:4px 10px; font-weight:600;"
        )

    def set_interrupted(self):
        self.setText("Interrupted")
        self.setStyleSheet(
            "background:#4a1a2e; color:#f87171; border-radius:12px; padding:4px 10px; font-weight:600;"
        )


# ---------------------------------------------------------------------------
# Voice page
# ---------------------------------------------------------------------------
class VoiceUI(QWidget):
    def __init__(self):
        super().__init__()
        self._voice_module = module_manager.get("voice")
        self._ai_module = module_manager.get("ai")
        self._controller = None
        self._tts = None
        self._active = False

        self._build_ui()
        self._connect_events()

        # Level meter refresh
        self._level_timer = QTimer(self)
        self._level_timer.timeout.connect(self._refresh_level)
        self._level_timer.start(33)  # ~30 fps

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(18)

        # --- Header -------------------------------------------------------
        header_row = QHBoxLayout()
        title = QLabel("Voice Conversation")
        title.setObjectName("PageTitle")
        self._status_pill = StatusPill()
        header_row.addWidget(title)
        header_row.addStretch()
        header_row.addWidget(self._status_pill)
        root.addLayout(header_row)

        # --- Waveform + controls ------------------------------------------
        wave_card = QFrame()
        wave_card.setObjectName("StatCard")
        wave_layout = QVBoxLayout(wave_card)
        wave_layout.setContentsMargins(16, 12, 16, 12)
        wave_layout.setSpacing(10)

        self._waveform = WaveformMeter()
        wave_layout.addWidget(self._waveform)

        ctrl_row = QHBoxLayout()

        # Mode toggle
        self._mode_btn = QPushButton("● Always On")
        self._mode_btn.setObjectName("ModeButton")
        self._mode_btn.setCheckable(True)
        self._mode_btn.setChecked(config.get("voice.mode", "always_on") == "always_on")
        self._mode_btn.clicked.connect(self._toggle_mode)
        ctrl_row.addWidget(self._mode_btn)

        # PTT button
        self._ptt_btn = QPushButton("Hold to Talk")
        self._ptt_btn.setVisible(False)
        self._ptt_btn.pressed.connect(self._ptt_press)
        self._ptt_btn.released.connect(self._ptt_release)
        ctrl_row.addWidget(self._ptt_btn)

        ctrl_row.addStretch()

        # Mic selector
        ctrl_row.addWidget(QLabel("Mic:"))
        self._mic_combo = QComboBox()
        self._mic_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._populate_mics()
        self._mic_combo.currentIndexChanged.connect(self._on_mic_changed)
        ctrl_row.addWidget(self._mic_combo)

        self._test_mic_btn = QPushButton("Test Mic")
        self._test_mic_btn.clicked.connect(self._do_test_mic)
        ctrl_row.addWidget(self._test_mic_btn)

        wave_layout.addLayout(ctrl_row)
        root.addWidget(wave_card)

        # --- Start/Stop + Interrupt row -----------------------------------
        action_row = QHBoxLayout()

        self._start_btn = QPushButton("▶  Start Listening")
        self._start_btn.clicked.connect(self._toggle_listening)
        self._start_btn.setFixedHeight(40)
        action_row.addWidget(self._start_btn)

        self._interrupt_btn = QPushButton("✕  Interrupt")
        self._interrupt_btn.setEnabled(False)
        self._interrupt_btn.clicked.connect(self._do_interrupt)
        self._interrupt_btn.setFixedHeight(40)
        self._interrupt_btn.setStyleSheet(
            "QPushButton { background:#7f1d1d; color:#fca5a5; border-radius:6px; }"
            "QPushButton:hover { background:#991b1b; }"
            "QPushButton:disabled { background:#2a2e35; color:#6b7076; }"
        )
        action_row.addWidget(self._interrupt_btn)

        root.addLayout(action_row)

        # --- Transcript ---------------------------------------------------
        trans_label = QLabel("You said")
        trans_label.setObjectName("SectionTitle")
        root.addWidget(trans_label)

        self._transcript_box = QTextEdit()
        self._transcript_box.setReadOnly(True)
        self._transcript_box.setFixedHeight(100)
        self._transcript_box.setPlaceholderText("Your speech will appear here...")
        self._transcript_box.setObjectName("TranscriptBox")
        root.addWidget(self._transcript_box)

        # --- SAINT response -----------------------------------------------
        saint_label = QLabel("SAINT")
        saint_label.setObjectName("SectionTitle")
        root.addWidget(saint_label)

        self._response_box = QTextEdit()
        self._response_box.setReadOnly(True)
        self._response_box.setPlaceholderText("SAINT's response will stream here...")
        self._response_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self._response_box)

        root.addStretch()

        self._update_mode_ui()

    def _populate_mics(self):
        self._mic_combo.clear()
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            for i, dev in enumerate(devices):
                if dev["max_input_channels"] > 0:
                    self._mic_combo.addItem(f"{i}: {dev['name']}", i)
        except Exception:
            self._mic_combo.addItem("Default", 0)

        saved_idx = config.get("voice.mic_device", None)
        if saved_idx is not None:
            idx = self._mic_combo.findData(saved_idx)
            if idx >= 0:
                self._mic_combo.setCurrentIndex(idx)

    # ------------------------------------------------------------------ #
    # Event wiring
    # ------------------------------------------------------------------ #
    def _connect_events(self):
        event_bus.event_occurred.connect(self._on_event)

    def _on_event(self, ev):
        t = ev.type
        if t == EventType.VOICE_STT_PARTIAL:
            self._transcript_box.setPlainText(ev.payload.get("text", ""))
        elif t == EventType.VOICE_STT_FINAL:
            text = ev.payload.get("text", "")
            self._transcript_box.setPlainText(text)
            self._response_box.clear()
            self._response_box.setPlainText("")
        elif t == EventType.AI_STREAM_TOKEN:
            cursor = self._response_box.textCursor()
            from PySide6.QtGui import QTextCursor
            cursor.movePosition(QTextCursor.End)
            cursor.insertText(ev.payload.get("token", ""))
            self._response_box.setTextCursor(cursor)
        elif t == EventType.CONVERSATION_TURN_START:
            self._status_pill.set_thinking()
            self._interrupt_btn.setEnabled(True)
        elif t == EventType.CONVERSATION_TURN_END:
            self._status_pill.set_listening() if self._active else self._status_pill.set_idle()
            self._interrupt_btn.setEnabled(False)
        elif t == EventType.CONVERSATION_INTERRUPTED:
            self._status_pill.set_interrupted()
        elif t == EventType.TTS_SPEAK_CHUNK:
            self._status_pill.set_speaking()
        elif t == EventType.AI_CANCELLED:
            self._response_box.append("\n[interrupted]")

    def _refresh_level(self):
        if self._voice_module and self._active:
            self._waveform.set_level(self._voice_module.audio_level)

    # ------------------------------------------------------------------ #
    # Controls
    # ------------------------------------------------------------------ #
    def _toggle_listening(self):
        if not self._active:
            self._start_listening()
        else:
            self._stop_listening()

    def _start_listening(self):
        self._active = True
        self._start_btn.setText("■  Stop Listening")

        # Ensure voice module is enabled
        from core.module_manager import module_manager as mm
        mm.set_enabled("voice", True)

        # Build TTS + controller if needed
        if self._controller is None:
            tts_backend = config.get("voice.tts_backend", "kokoro")
            if tts_backend == "kokoro":
                tts_voice = config.get("voice.tts_voice", "af_heart")
                tts_device = config.get("voice.tts_device", "cuda")
                tts_speed = config.get("voice.tts_speed", 1.0)
                self._tts = make_tts(
                    "kokoro", voice=tts_voice, device=tts_device, speed=tts_speed
                )
            else:
                self._tts = make_tts("mock")

            self._controller = init_controller(
                ai_module=self._ai_module,
                voice_module=self._voice_module,
                tts_engine=self._tts,
            )

        self._voice_module.start_listening()
        self._status_pill.set_listening()

    def _stop_listening(self):
        self._active = False
        self._start_btn.setText("▶  Start Listening")
        if self._voice_module:
            self._voice_module.stop_listening()
        self._status_pill.set_idle()

    def _toggle_mode(self):
        if self._mode_btn.isChecked():
            config.set("voice.mode", "always_on")
            self._mode_btn.setText("● Always On")
        else:
            config.set("voice.mode", "push_to_talk")
            self._mode_btn.setText("⊙ Push-to-Talk")
        self._update_mode_ui()

    def _update_mode_ui(self):
        is_ptt = config.get("voice.mode", "always_on") == "push_to_talk"
        self._ptt_btn.setVisible(is_ptt)

    def _ptt_press(self):
        if self._voice_module:
            self._voice_module.ptt_press()

    def _ptt_release(self):
        if self._voice_module:
            self._voice_module.ptt_release()

    def _do_interrupt(self):
        if self._controller:
            self._controller._cancel_current()
        event_bus.emit_event(EventType.VOICE_INTERRUPT, {"source": "button"})

    def _on_mic_changed(self, idx):
        device_idx = self._mic_combo.itemData(idx)
        config.set("voice.mic_device", device_idx, persist=True)

    def _do_test_mic(self):
        if self._active:
            self._stop_listening()

        self._test_mic_btn.setEnabled(False)
        self._test_mic_btn.setText("Starting Test...")
        self._status_pill.setText("Mic Test")

        idx = self._mic_combo.itemData(self._mic_combo.currentIndex())
        self._test_worker = MicTestWorker(idx, self)
        self._test_worker.status_update.connect(self._on_test_status)
        self._test_worker.finished_ok.connect(self._on_test_done)
        self._test_worker.finished_error.connect(self._on_test_error)
        self._test_worker.start()

    def _on_test_status(self, text):
        self._test_mic_btn.setText(text)
        self._status_pill.setText(text)

    def _on_test_done(self):
        self._test_mic_btn.setEnabled(True)
        self._test_mic_btn.setText("Test Mic")
        self._status_pill.set_idle()

    def _on_test_error(self, err):
        from PySide6.QtWidgets import QMessageBox
        self._test_mic_btn.setEnabled(True)
        self._test_mic_btn.setText("Test Mic")
        self._status_pill.set_idle()
        QMessageBox.critical(self, "Mic Test Error", str(err))

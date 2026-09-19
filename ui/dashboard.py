"""
ui/dashboard.py

Home screen. A unified interface combining voice, text chat, and core system stats.
"""

from PySide6.QtCore import QThread, Qt, QTimer, Signal
import time

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QComboBox, QFrame, QSizePolicy, QLineEdit
)
from PySide6.QtGui import QColor, QPainter, QTextCursor

from core.events import event_bus, EventType
from core.config import config
from core.state import app_state
from core.module_manager import module_manager
from core.conversation import get_controller, init_controller, ConvState
from modules.voice.tts_service import get_tts_service
from ui.workers import StreamingAIWorker

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

            duration = 3.0
            device_info = sd.query_devices(self.device_idx, 'input')
            fs = int(device_info['default_samplerate'])
            self.status_update.emit("Recording...")
            recording = sd.rec(int(duration * fs), samplerate=fs, channels=1, dtype='int16', device=self.device_idx)
            sd.wait()

            self.status_update.emit("Playing...")
            sd.play(recording, samplerate=fs)
            sd.wait()

            self.finished_ok.emit()
        except Exception as e:
            self.finished_error.emit(str(e))


class WaveformMeter(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("WaveformMeter")
        self.setFixedHeight(48)
        self._level = 0.0
        self._bars = 48

    def set_level(self, level: float):
        self._level = max(0.0, min(1.0, level))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        bar_w = max(2, w // self._bars - 1)
        spacing = max(1, (w - self._bars * bar_w) // self._bars)

        for i in range(self._bars):
            bar_level = self._level * (0.6 + 0.4 * abs(self._bars // 2 - i) / (self._bars // 2))
            bar_h = int(bar_level * h)
            x = i * (bar_w + spacing)
            y = (h - bar_h) // 2

            hue = int(120 * (1.0 - self._level))
            color = QColor.fromHsl(hue, 200, 120)
            painter.fillRect(x, y, bar_w, bar_h, color)
        painter.end()


class StatusPill(QLabel):
    def __init__(self, text="Idle", parent=None):
        super().__init__(text, parent)
        self.setObjectName("StatusPill")
        self.setAlignment(Qt.AlignCenter)
        self.setFixedWidth(120)
        self.set_idle()

    def set_idle(self):
        self.setText("Idle")
        self.setStyleSheet("background:#2a2e35; color:#9aa0a6; border-radius:12px; padding:4px 10px; font-weight:600;")

    def set_listening(self):
        self.setText("Listening")
        self.setStyleSheet("background:#1a4a2e; color:#3ddc84; border-radius:12px; padding:4px 10px; font-weight:600;")

    def set_thinking(self):
        self.setText("Thinking")
        self.setStyleSheet("background:#1a2e4a; color:#60a5fa; border-radius:12px; padding:4px 10px; font-weight:600;")

    def set_speaking(self):
        self.setText("Speaking")
        self.setStyleSheet("background:#4a2e1a; color:#fb923c; border-radius:12px; padding:4px 10px; font-weight:600;")

    def set_interrupted(self):
        self.setText("Interrupted")
        self.setStyleSheet("background:#4a1a2e; color:#f87171; border-radius:12px; padding:4px 10px; font-weight:600;")


class StatCardCompact(QFrame):
    def __init__(self, title, value="--"):
        super().__init__()
        self.setObjectName("StatCardCompact")
        self.setStyleSheet("QFrame#StatCardCompact { background: #1e2126; border-radius: 8px; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("color: #9aa0a6; font-size: 11px; font-weight: 500;")
        
        self.value_label = QLabel(value)
        self.value_label.setStyleSheet("color: #e8eaed; font-size: 14px; font-weight: bold;")

        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)

    def set_value(self, value):
        self.value_label.setText(str(value))


class Dashboard(QWidget):
    def __init__(self):
        super().__init__()
        self._voice_module = module_manager.get("voice")
        self._ai_module = module_manager.get("ai")
        self._controller = None
        self._tts = None
        self._active = False
        self._worker = None
        self._current_stream_id: str = ""
        self._current_turn_id: int = -1
        self._last_turn_id: int = -1

        self._build_ui()
        self._connect_events()

        self._level_timer = QTimer(self)
        self._level_timer.timeout.connect(self._refresh_level)
        self._level_timer.start(33)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_stats)
        self.timer.start(2000)
        self.refresh_stats()

        # Initialize TTS service in the background at startup
        QTimer.singleShot(100, self._init_services)

    def _init_services(self):
        if self._controller is not None:
            return

        tts_service = get_tts_service()
        tts_backend = config.get("voice.tts_backend", "kokoro")
        tts_config = {}

        if tts_backend == "qwen":
            tts_config = {
                "model_name": config.get("voice.tts_qwen_model", "Qwen/Qwen3-TTS"),
                "model_type": config.get("voice.tts_qwen_type", "custom_voice"),
                "speaker": config.get("voice.tts_qwen_speaker", "eric"),
                "language": config.get("voice.tts_qwen_language", "Auto"),
                "device": config.get("voice.tts_device", "cuda"),
                "dtype": config.get("voice.tts_qwen_dtype", "bfloat16"),
                "voice_clone_audio": config.get("voice.tts_qwen_voice_clone_audio", "") or None,
                "voice_clone_text": config.get("voice.tts_qwen_voice_clone_text", "") or None,
                "x_vector_only": config.get("voice.tts_qwen_x_vector_only", False),
                "instruct": config.get("voice.tts_qwen_instruct", "") or None,
                "speed": config.get("voice.tts_speed", 1.0),
                "flash_attention": config.get("voice.tts_qwen_flash_attention", "Auto"),
            }
        elif tts_backend == "kokoro":
            tts_config = {
                "voice": config.get("voice.tts_voice", "af_heart"),
                "device": config.get("voice.tts_device", "cuda"),
                "speed": config.get("voice.tts_speed", 1.0),
            }

        # Initialize TTS service in background
        tts_service.initialize(backend=tts_backend, blocking=False, **tts_config)
        self._tts = tts_service

        self._controller = init_controller(
            ai_module=self._ai_module,
            voice_module=self._voice_module,
            tts_engine=tts_service,
        )

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(20)

        # Header
        header_row = QHBoxLayout()
        title = QLabel("SAINT Interface")
        title.setObjectName("PageTitle")
        self._status_pill = StatusPill()
        
        # Local time display
        self._time_label = QLabel()
        self._time_label.setObjectName("TimeLabel")
        self._time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._time_label.setStyleSheet("color: #9aa0a6; font-size: 13px; font-family: monospace;")
        self._update_time()
        
        self._time_timer = QTimer(self)
        self._time_timer.timeout.connect(self._update_time)
        self._time_timer.start(1000)  # Update every second
        
        header_row.addWidget(title)
        header_row.addStretch()
        header_row.addWidget(self._time_label)
        header_row.addWidget(self._status_pill)
        root.addLayout(header_row)

        # Waveform + Controls
        wave_card = QFrame()
        wave_card.setObjectName("StatCard")
        wave_layout = QVBoxLayout(wave_card)
        wave_layout.setContentsMargins(16, 16, 16, 16)
        wave_layout.setSpacing(12)

        self._waveform = WaveformMeter()
        wave_layout.addWidget(self._waveform)

        ctrl_row = QHBoxLayout()
        self._mode_btn = QPushButton("● Always On")
        self._mode_btn.setCheckable(True)
        self._mode_btn.setChecked(config.get("voice.mode", "always_on") == "always_on")
        self._mode_btn.clicked.connect(self._toggle_mode)
        self._mode_btn.setFixedHeight(30)
        ctrl_row.addWidget(self._mode_btn)

        self._ptt_btn = QPushButton("🎤 PTT")
        self._ptt_btn.setVisible(False)
        self._ptt_btn.setCheckable(True)
        self._ptt_btn.setChecked(False)
        self._ptt_btn.clicked.connect(self._toggle_ptt)
        self._ptt_btn.setFixedHeight(30)
        ctrl_row.addWidget(self._ptt_btn)
        
        ctrl_row.addStretch()

        self._start_btn = QPushButton("▶  Start Listening")
        self._start_btn.clicked.connect(self._toggle_listening)
        self._start_btn.setFixedHeight(30)
        self._start_btn.setStyleSheet("background: #1a4a2e; color: #3ddc84; font-weight: bold;")
        ctrl_row.addWidget(self._start_btn)

        self._interrupt_btn = QPushButton("✕  Interrupt")
        self._interrupt_btn.setEnabled(False)
        self._interrupt_btn.clicked.connect(self._do_interrupt)
        self._interrupt_btn.setFixedHeight(30)
        self._interrupt_btn.setStyleSheet(
            "QPushButton { background:#7f1d1d; color:#fca5a5; }"
            "QPushButton:hover { background:#991b1b; }"
            "QPushButton:disabled { background:#2a2e35; color:#6b7076; }"
        )
        ctrl_row.addWidget(self._interrupt_btn)

        ctrl_row.addStretch()

        ctrl_row.addWidget(QLabel("Mic:"))
        self._mic_combo = QComboBox()
        self._populate_mics()
        self._mic_combo.currentIndexChanged.connect(self._on_mic_changed)
        ctrl_row.addWidget(self._mic_combo)

        self._test_mic_btn = QPushButton("Test Mic")
        self._test_mic_btn.clicked.connect(self._do_test_mic)
        ctrl_row.addWidget(self._test_mic_btn)

        wave_layout.addLayout(ctrl_row)
        root.addWidget(wave_card)

        # Transcripts & Chat Area
        chat_layout = QHBoxLayout()
        chat_layout.setSpacing(16)
        
        # User side (STT + text input history)
        user_col = QVBoxLayout()
        user_label = QLabel("You")
        user_label.setStyleSheet("color: #9aa0a6; font-weight: bold;")
        user_col.addWidget(user_label)
        self._transcript_box = QTextEdit()
        self._transcript_box.setReadOnly(True)
        self._transcript_box.setPlaceholderText("Your speech or typed prompts will appear here...")
        self._transcript_box.setStyleSheet("background: #1e2126; border: 1px solid #2a2e35; border-radius: 6px; padding: 8px;")
        user_col.addWidget(self._transcript_box)
        chat_layout.addLayout(user_col, 1)

        # SAINT side
        agent_col = QVBoxLayout()
        agent_label = QLabel("SAINT")
        agent_label.setStyleSheet("color: #60a5fa; font-weight: bold;")
        agent_col.addWidget(agent_label)
        self._response_box = QTextEdit()
        self._response_box.setReadOnly(True)
        self._response_box.setPlaceholderText("SAINT's responses will stream here...")
        self._response_box.setStyleSheet("background: #1a202c; border: 1px solid #2d3748; border-radius: 6px; padding: 8px; font-size: 14px;")
        agent_col.addWidget(self._response_box)
        chat_layout.addLayout(agent_col, 2)
        
        root.addLayout(chat_layout)

        # Text Input
        input_row = QHBoxLayout()
        self.prompt_input = QLineEdit()
        self.prompt_input.setPlaceholderText("Type a prompt and press Send...")
        self.prompt_input.returnPressed.connect(self.send_prompt)
        self.prompt_input.setFixedHeight(40)
        self.prompt_input.setStyleSheet("border-radius: 20px; padding-left: 15px; background: #2a2e35; border: 1px solid #3d424b;")
        
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self.send_prompt)
        self.send_button.setFixedHeight(40)
        self.send_button.setFixedWidth(80)
        self.send_button.setStyleSheet("border-radius: 20px; background: #3b82f6; color: white; font-weight: bold;")
        
        input_row.addWidget(self.prompt_input)
        input_row.addWidget(self.send_button)
        root.addLayout(input_row)

        # Compact Stats
        stats_row = QHBoxLayout()
        stats_row.setSpacing(10)
        self.card_cpu = StatCardCompact("CPU", "0%")
        self.card_ram = StatCardCompact("RAM", "0 MB")
        self.card_uptime = StatCardCompact("Uptime", "00:00:00")
        stats_row.addWidget(self.card_cpu)
        stats_row.addWidget(self.card_ram)
        stats_row.addWidget(self.card_uptime)
        stats_row.addStretch()
        root.addLayout(stats_row)

        self._update_mode_ui()

    def _update_time(self):
        """Update the local time display."""
        from datetime import datetime
        self._time_label.setText(datetime.now().strftime("%H:%M:%S"))

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
        elif t == EventType.AI_STREAM_START:
            self._current_stream_id = ev.payload.get("stream_id", ev.payload.get("request_id", ""))
            self._current_turn_id = ev.payload.get("turn_id", -1)
            self._last_turn_id = self._current_turn_id
            self._response_box.clear()
            self._response_box.setPlainText("")
        elif t == EventType.AI_STREAM_TOKEN:
            token_stream_id = ev.payload.get("stream_id", "")
            token_turn_id = ev.payload.get("turn_id", -1)
            # Match by turn_id primarily; stream_id is secondary check
            if token_turn_id != self._current_turn_id:
                return
            if token_stream_id and token_stream_id != self._current_stream_id:
                # Allow if stream_id is empty (backward compat) or matches
                return
            cursor = self._response_box.textCursor()
            cursor.movePosition(QTextCursor.End)
            cursor.insertText(ev.payload.get("token", ""))
            self._response_box.setTextCursor(cursor)
            self._response_box.ensureCursorVisible()
        elif t == EventType.AI_STREAM_END:
            self._current_stream_id = ""
            self._current_turn_id = -1
        elif t == EventType.CONVERSATION_TURN_START:
            self._status_pill.set_thinking()
            self._interrupt_btn.setEnabled(True)
        elif t == EventType.CONVERSATION_TURN_END:
            self._status_pill.set_listening() if self._active else self._status_pill.set_idle()
            self._interrupt_btn.setEnabled(False)
            # Fallback: ensure response is displayed even if chat message events missed
            response = ev.payload.get("response", "")
            if response and self._last_turn_id >= 0:
                self._response_box.setPlainText(response)
        elif t == EventType.CONVERSATION_INTERRUPTED:
            self._status_pill.set_interrupted()
        elif t == EventType.TTS_SPEAK_CHUNK:
            self._status_pill.set_speaking()
        elif t == EventType.TTS_SPEAK_START:
            self._status_pill.set_speaking()
        elif t == EventType.TTS_SPEAK_DONE:
            self._status_pill.set_listening() if self._active else self._status_pill.set_idle()
        elif t == EventType.TTS_ERROR:
            self._response_box.append(f"\n[TTS Error] {ev.payload.get('error', 'Unknown error')}")
        elif t == EventType.AI_CANCELLED:
            self._response_box.append("\n[interrupted]")
        elif t == EventType.CHAT_MESSAGE_START:
            # Chat message start logging handled by console
            pass
        elif t == EventType.CHAT_MESSAGE_FINAL:
            # Final chat message - ensure it's rendered
            turn_id = ev.payload.get("turn_id")
            text = ev.payload.get("text", "")
            if turn_id == self._last_turn_id and text:
                self._response_box.setPlainText(text)
        elif t == EventType.UI_CHAT_RENDER:
            # Explicit UI render event
            turn_id = ev.payload.get("turn_id")
            role = ev.payload.get("role", "")
            text = ev.payload.get("text", "")
            if role == "assistant" and turn_id == self._last_turn_id and text:
                self._response_box.setPlainText(text)

    def _refresh_level(self):
        if self._voice_module and self._voice_module.voice_active:
            self._waveform.set_level(self._voice_module.audio_level)

    def _refresh_diagnostics(self):
        if self._voice_module:
            diag = self._voice_module.diagnostics
            if diag.get("stt_loaded"):
                self._tts_label.setText(f"STT: {diag.get('stt_device', 'N/A')}")
            if diag.get('active_workers'):
                self._worker_label.setText(f"Workers: {', '.join(diag['active_workers'])}")
            else:
                self._worker_label.setText("Workers: idle")

    def refresh_stats(self):
        self.card_cpu.set_value(f"{app_state.cpu_percent():.0f}%")
        self.card_ram.set_value(f"{app_state.ram_mb():.0f} MB")
        self.card_uptime.set_value(app_state.uptime_formatted())

    # ------------------------------------------------------------------ #
    # Controls
    # ------------------------------------------------------------------ #
    def _toggle_listening(self):
        if not self._active:
            self._start_listening()
        else:
            self._stop_listening()

    def _start_listening(self):
        if self._active:
            return

        self._active = True
        self._start_btn.setText("■  Stop Listening")
        self._start_btn.setStyleSheet("background: #991b1b; color: white; font-weight: bold;")

        from core.module_manager import module_manager as mm
        mm.set_enabled("voice", True)

        # Ensure services are initialized
        self._init_services()

        self._voice_module.start_listening()
        self._status_pill.set_listening()

    def _stop_listening(self):
        self._active = False
        self._start_btn.setText("▶  Start Listening")
        self._start_btn.setStyleSheet("background: #1a4a2e; color: #3ddc84; font-weight: bold;")
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
        if not self._active:
            self._start_listening()

    def _update_mode_ui(self):
        is_ptt = config.get("voice.mode", "always_on") == "push_to_talk"
        self._ptt_btn.setVisible(is_ptt)

    def _toggle_ptt(self):
        if self._voice_module:
            if self._ptt_btn.isChecked():
                self._voice_module.ptt_toggle(True)
                self._start_listening()
            else:
                self._voice_module.ptt_toggle(False)
                self._voice_module.stop_listening()
                self._stop_listening()

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

    # ------------------------------------------------------------------ #
    # Manual Prompt via Text Input
    # ------------------------------------------------------------------ #
    def send_prompt(self):
        prompt = self.prompt_input.text().strip()
        if not prompt:
            return
        if self._worker is not None and self._worker.isRunning():
            return  # a request is already in flight

        self.send_button.setEnabled(False)
        self._transcript_box.setPlainText(f"> {prompt}")
        self.prompt_input.clear()
        
        self._response_box.clear()
        self._status_pill.set_thinking()

        self._worker = StreamingAIWorker(self._ai_module, prompt)
        self._worker.token_received.connect(self._on_text_token)
        self._worker.finished_ok.connect(self._on_text_response)
        self._worker.finished_error.connect(self._on_text_error)
        self._worker.finished.connect(lambda: self.send_button.setEnabled(True))
        self._worker.stream_start.connect(self._on_stream_start)
        self._worker.stream_end.connect(self._on_stream_end)
        self._worker.start()

    def _on_stream_start(self, stream_id, turn_id, request_id):
        self._current_stream_id = stream_id
        self._current_turn_id = turn_id
        self._last_turn_id = turn_id
        event_bus.emit_event(EventType.AI_STREAM_START, {
            "stream_id": stream_id,
            "turn_id": turn_id,
            "request_id": request_id,
        })

    def _on_stream_end(self, stream_id, turn_id, request_id):
        self._current_stream_id = ""
        self._current_turn_id = -1
        event_bus.emit_event(EventType.AI_STREAM_END, {
            "stream_id": stream_id,
            "turn_id": turn_id,
            "request_id": request_id,
        })

    def _on_text_token(self, token):
        cursor = self._response_box.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._response_box.setTextCursor(cursor)
        self._response_box.insertPlainText(token)
        self._response_box.ensureCursorVisible()

    def _on_text_response(self, text):
        self._status_pill.set_idle()

    def _on_text_error(self, err):
        self._status_pill.set_idle()
        self._response_box.append(f"\n[error] {err}")

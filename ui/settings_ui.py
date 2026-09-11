"""
ui/settings_ui.py

Settings page — extended for Milestone 1 with a full Voice section.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QComboBox, QLineEdit, QCheckBox,
    QPushButton, QLabel, QDoubleSpinBox, QMessageBox, QScrollArea, QFrame,
    QSlider, QHBoxLayout,
)
from PySide6.QtCore import Qt

from core.config import config
from core.events import event_bus, EventType


def _section(title: str) -> QLabel:
    lbl = QLabel(title)
    lbl.setObjectName("SectionTitle")
    return lbl


class SettingsUI(QWidget):
    def __init__(self, on_theme_changed=None):
        super().__init__()
        self.on_theme_changed = on_theme_changed

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        title = QLabel("Settings")
        title.setObjectName("PageTitle")
        root.addWidget(title)

        # ==================================================================
        # App
        # ==================================================================
        root.addWidget(_section("App"))
        form_app = QFormLayout()
        form_app.setSpacing(10)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Dark", "Light"])
        self.theme_combo.setCurrentText(config.get("theme", "Dark"))
        form_app.addRow("Theme", self.theme_combo)

        self.autosave_checkbox = QCheckBox("On")
        self.autosave_checkbox.setChecked(config.get("auto_save", True))
        form_app.addRow("Auto Save", self.autosave_checkbox)

        root.addLayout(form_app)

        # ==================================================================
        # AI / LLM
        # ==================================================================
        root.addWidget(_section("AI / LLM"))
        form_ai = QFormLayout()
        form_ai.setSpacing(10)

        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["mock", "openai", "ollama"])
        self.provider_combo.setCurrentText(config.get("ai.provider", "ollama"))
        form_ai.addRow("Provider", self.provider_combo)

        self.model_input = QLineEdit(config.get("ai.model", "llama3"))
        form_ai.addRow("Model", self.model_input)

        self.base_url_input = QLineEdit(config.get("ai.base_url", "http://localhost:11434"))
        form_ai.addRow("Base URL", self.base_url_input)

        self.api_key_input = QLineEdit(config.get("ai.api_key", ""))
        self.api_key_input.setEchoMode(QLineEdit.Password)
        form_ai.addRow("API Key", self.api_key_input)

        self.temperature_input = QDoubleSpinBox()
        self.temperature_input.setRange(0.0, 2.0)
        self.temperature_input.setSingleStep(0.1)
        self.temperature_input.setValue(config.get("ai.temperature", 0.7))
        form_ai.addRow("Temperature", self.temperature_input)

        root.addLayout(form_ai)

        # ==================================================================
        # Voice — Input
        # ==================================================================
        root.addWidget(_section("Voice — Input"))
        form_voice_in = QFormLayout()
        form_voice_in.setSpacing(10)

        self.voice_mode_combo = QComboBox()
        self.voice_mode_combo.addItems(["always_on", "push_to_talk"])
        self.voice_mode_combo.setCurrentText(config.get("voice.mode", "always_on"))
        form_voice_in.addRow("Mode", self.voice_mode_combo)

        # Sensitivity slider
        sens_row = QHBoxLayout()
        self.sensitivity_slider = QSlider(Qt.Horizontal)
        self.sensitivity_slider.setRange(1, 100)
        self.sensitivity_slider.setValue(int(config.get("voice.mic_sensitivity", 0.015) * 1000))
        self.sensitivity_label = QLabel(f"{config.get('voice.mic_sensitivity', 0.015):.3f}")
        self.sensitivity_slider.valueChanged.connect(
            lambda v: self.sensitivity_label.setText(f"{v / 1000:.3f}")
        )
        sens_row.addWidget(self.sensitivity_slider)
        sens_row.addWidget(self.sensitivity_label)
        form_voice_in.addRow("Mic Sensitivity", sens_row)

        # Silence duration
        self.silence_dur_spin = QDoubleSpinBox()
        self.silence_dur_spin.setRange(200, 3000)
        self.silence_dur_spin.setSingleStep(50)
        self.silence_dur_spin.setSuffix(" ms")
        self.silence_dur_spin.setValue(config.get("voice.silence_duration_ms", 400))
        form_voice_in.addRow("Silence Duration", self.silence_dur_spin)

        self.noise_sup_checkbox = QCheckBox("Enabled")
        self.noise_sup_checkbox.setChecked(config.get("voice.noise_suppression", True))
        form_voice_in.addRow("Noise Suppression", self.noise_sup_checkbox)

        root.addLayout(form_voice_in)

        # ==================================================================
        # Voice — STT
        # ==================================================================
        root.addWidget(_section("Voice — Speech Recognition (STT)"))
        form_stt = QFormLayout()
        form_stt.setSpacing(10)

        self.stt_backend_combo = QComboBox()
        self.stt_backend_combo.addItems(["faster_whisper", "mock"])
        self.stt_backend_combo.setCurrentText(config.get("voice.stt_backend", "faster_whisper"))
        form_stt.addRow("STT Backend", self.stt_backend_combo)

        self.stt_model_combo = QComboBox()
        self.stt_model_combo.addItems(["tiny.en", "base.en", "small.en", "medium.en", "large-v3"])
        self.stt_model_combo.setCurrentText(config.get("voice.stt_model", "tiny.en"))
        form_stt.addRow("Whisper Model", self.stt_model_combo)

        self.stt_device_combo = QComboBox()
        self.stt_device_combo.addItems(["cuda", "cpu"])
        self.stt_device_combo.setCurrentText(config.get("voice.stt_device", "cuda"))
        form_stt.addRow("Device", self.stt_device_combo)

        self.stt_compute_combo = QComboBox()
        self.stt_compute_combo.addItems(["float16", "int8", "float32"])
        self.stt_compute_combo.setCurrentText(config.get("voice.stt_compute_type", "float16"))
        form_stt.addRow("Compute Type", self.stt_compute_combo)

        self.stt_lang_input = QLineEdit(config.get("voice.stt_language", "en"))
        form_stt.addRow("Language", self.stt_lang_input)

        root.addLayout(form_stt)

        # ==================================================================
        # Voice — TTS
        # ==================================================================
        root.addWidget(_section("Voice — Speech Output (TTS)"))
        form_tts = QFormLayout()
        form_tts.setSpacing(10)

        self.tts_backend_combo = QComboBox()
        self.tts_backend_combo.addItems(["kokoro", "mock"])
        self.tts_backend_combo.setCurrentText(config.get("voice.tts_backend", "kokoro"))
        form_tts.addRow("TTS Backend", self.tts_backend_combo)

        self.tts_voice_input = QLineEdit(config.get("voice.tts_voice", "af_heart"))
        form_tts.addRow("Voice", self.tts_voice_input)

        self.tts_device_combo = QComboBox()
        self.tts_device_combo.addItems(["cuda", "cpu"])
        self.tts_device_combo.setCurrentText(config.get("voice.tts_device", "cuda"))
        form_tts.addRow("Device", self.tts_device_combo)

        self.tts_speed_spin = QDoubleSpinBox()
        self.tts_speed_spin.setRange(0.5, 2.0)
        self.tts_speed_spin.setSingleStep(0.1)
        self.tts_speed_spin.setValue(config.get("voice.tts_speed", 1.0))
        form_tts.addRow("Speed", self.tts_speed_spin)

        root.addLayout(form_tts)

        # ==================================================================
        # Conversation
        # ==================================================================
        root.addWidget(_section("Conversation"))
        form_conv = QFormLayout()
        form_conv.setSpacing(10)

        self.max_turns_spin = QDoubleSpinBox()
        self.max_turns_spin.setRange(1, 20)
        self.max_turns_spin.setDecimals(0)
        self.max_turns_spin.setValue(config.get("voice.max_context_turns", 6))
        form_conv.addRow("Context Turns", self.max_turns_spin)

        self.system_prompt_input = QLineEdit(
            config.get("voice.system_prompt",
                       "You are SAINT, a helpful AI assistant. Be concise.")
        )
        form_conv.addRow("System Prompt", self.system_prompt_input)

        root.addLayout(form_conv)

        # ==================================================================
        # Logging / Analytics
        # ==================================================================
        root.addWidget(_section("Logging & Analytics"))
        form_log = QFormLayout()
        form_log.setSpacing(10)

        self.logging_combo = QComboBox()
        self.logging_combo.addItems(["Verbose", "Normal", "Errors Only"])
        self.logging_combo.setCurrentText(config.get("logging.level", "Verbose"))
        form_log.addRow("Logging Level", self.logging_combo)

        self.analytics_checkbox = QCheckBox("Enabled")
        self.analytics_checkbox.setChecked(config.get("analytics.enabled", True))
        form_log.addRow("Analytics", self.analytics_checkbox)

        root.addLayout(form_log)

        # ==================================================================
        # Save button
        # ==================================================================
        save_btn = QPushButton("Save Settings")
        save_btn.clicked.connect(self.save)
        root.addWidget(save_btn)
        root.addStretch()

    def save(self):
        # App
        config.set("theme", self.theme_combo.currentText(), persist=False)
        config.set("auto_save", self.autosave_checkbox.isChecked(), persist=False)

        # AI
        config.set("ai.provider", self.provider_combo.currentText(), persist=False)
        config.set("ai.model", self.model_input.text(), persist=False)
        config.set("ai.base_url", self.base_url_input.text(), persist=False)
        config.set("ai.api_key", self.api_key_input.text(), persist=False)
        config.set("ai.temperature", self.temperature_input.value(), persist=False)

        # Voice Input
        config.set("voice.mode", self.voice_mode_combo.currentText(), persist=False)
        config.set("voice.mic_sensitivity",
                   self.sensitivity_slider.value() / 1000, persist=False)
        config.set("voice.silence_duration_ms",
                   int(self.silence_dur_spin.value()), persist=False)
        config.set("voice.noise_suppression",
                   self.noise_sup_checkbox.isChecked(), persist=False)

        # STT
        config.set("voice.stt_backend", self.stt_backend_combo.currentText(), persist=False)
        config.set("voice.stt_model", self.stt_model_combo.currentText(), persist=False)
        config.set("voice.stt_device", self.stt_device_combo.currentText(), persist=False)
        config.set("voice.stt_compute_type",
                   self.stt_compute_combo.currentText(), persist=False)
        config.set("voice.stt_language", self.stt_lang_input.text(), persist=False)

        # TTS
        config.set("voice.tts_backend", self.tts_backend_combo.currentText(), persist=False)
        config.set("voice.tts_voice", self.tts_voice_input.text(), persist=False)
        config.set("voice.tts_device", self.tts_device_combo.currentText(), persist=False)
        config.set("voice.tts_speed", self.tts_speed_spin.value(), persist=False)

        # Conversation
        config.set("voice.max_context_turns", int(self.max_turns_spin.value()), persist=False)
        config.set("voice.system_prompt", self.system_prompt_input.text(), persist=False)

        # Logging / Analytics
        config.set("logging.level", self.logging_combo.currentText(), persist=False)
        config.set("analytics.enabled", self.analytics_checkbox.isChecked(), persist=False)

        config.save()
        event_bus.emit_event(EventType.SETTINGS_CHANGED, {})

        if self.on_theme_changed:
            self.on_theme_changed(self.theme_combo.currentText())

        QMessageBox.information(self, "Settings", "Settings saved.")

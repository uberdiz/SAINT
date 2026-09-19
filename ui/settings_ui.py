"""Scalable, category-based Settings UI for SAINT 0.2."""
import threading
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLineEdit,
    QCheckBox, QPushButton, QLabel, QDoubleSpinBox, QMessageBox, QFrame,
    QSlider, QStackedWidget, QListWidget, QListWidgetItem, QScrollArea, QGroupBox
)
from PySide6.QtCore import Qt, QTimer

from core.config import config
from core.events import event_bus, EventType
from core.module_manager import module_manager
from core.permissions import permission_manager


class SettingsUI(QWidget):
    """Settings use persistent category navigation instead of one long page."""

    CATEGORIES = ["General", "AI", "Voice", "Integrations", "Permissions", "Analytics", "Memory", "Advanced"]

    def __init__(self, on_theme_changed=None):
        super().__init__()
        self.on_theme_changed = on_theme_changed
        self._spotify_thread = None
        self._build()

    def _scroll_page(self, widget):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(widget)
        return scroll

    def _section(self, title, description="", form=True):
        box = QGroupBox(title)
        layout = QFormLayout(box) if form else QVBoxLayout(box)
        if description:
            label = QLabel(description)
            label.setObjectName("Subtitle")
            label.setWordWrap(True)
            layout.addWidget(label)
        return box, layout

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Settings")
        title.setObjectName("PageTitle")
        header.addWidget(title)
        header.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search settings...")
        self.search.setMaximumWidth(280)
        self.search.textChanged.connect(self._filter_categories)
        header.addWidget(self.search)
        root.addLayout(header)

        body = QHBoxLayout()
        self.nav = QListWidget()
        self.nav.setObjectName("SettingsNav")
        self.nav.setFixedWidth(170)
        for category in self.CATEGORIES:
            QListWidgetItem(category, self.nav)
        self.nav.currentRowChanged.connect(self.stack_set)
        body.addWidget(self.nav)

        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        self._build_general()
        self._build_ai()
        self._build_voice()
        self._build_integrations()
        self._build_permissions()
        self._build_analytics()
        self._build_memory()
        self._build_advanced()
        self.nav.setCurrentRow(0)

        footer = QHBoxLayout()
        self.status = QLabel("Changes are saved together.")
        self.status.setObjectName("Subtitle")
        footer.addWidget(self.status)
        footer.addStretch()
        save = QPushButton("Save Settings")
        save.clicked.connect(self.save)
        footer.addWidget(save)
        root.addLayout(footer)

    def stack_set(self, index):
        if index >= 0:
            self.stack.setCurrentIndex(index)

    def _add_page(self, widget):
        self.stack.addWidget(self._scroll_page(widget))

    def _filter_categories(self, text):
        query = text.strip().lower()
        for i in range(self.nav.count()):
            item = self.nav.item(i)
            item.setHidden(bool(query and query not in item.text().lower()))
        if query:
            for i, category in enumerate(self.CATEGORIES):
                if query in category.lower():
                    self.nav.setCurrentRow(i)
                    break

    def _form(self):
        f = QFormLayout()
        f.setSpacing(10)
        f.setLabelAlignment(Qt.AlignLeft)
        return f

    def _build_general(self):
        w = QWidget(); root = QVBoxLayout(w)
        box, f = self._section("Appearance", "Core application appearance and persistence.")
        self.theme_combo = QComboBox(); self.theme_combo.addItems(["Dark", "Light"]); self.theme_combo.setCurrentText(config.get("theme", "Dark"))
        self.autosave = QCheckBox("On"); self.autosave.setChecked(config.get("auto_save", True))
        f.addRow("Theme", self.theme_combo); f.addRow("Auto Save", self.autosave)
        root.addWidget(box)
        box, f = self._section("Dashboard", "Control how much information the dashboard displays.")
        self.dash_complexity = QComboBox(); self.dash_complexity.addItems(["Simple","Standard","Advanced","Developer"]); self.dash_complexity.setCurrentText(config.get("dashboard.complexity","Standard"))
        f.addRow("Complexity", self.dash_complexity); root.addWidget(box); root.addStretch()
        self._add_page(w)

    def _build_ai(self):
        w = QWidget(); root = QVBoxLayout(w)
        box, f = self._section("AI Provider", "Configure the local or remote model used by SAINT.")
        self.provider = QComboBox(); self.provider.addItems(["mock","openai","ollama"]); self.provider.setCurrentText(config.get("ai.provider","ollama"))
        self.model = QLineEdit(config.get("ai.model","llama3"))
        self.base_url = QLineEdit(config.get("ai.base_url","http://localhost:11434"))
        self.api_key = QLineEdit(config.get("ai.api_key","")); self.api_key.setEchoMode(QLineEdit.Password)
        self.temperature = QDoubleSpinBox(); self.temperature.setRange(0,2); self.temperature.setSingleStep(.1); self.temperature.setValue(config.get("ai.temperature",.7))
        f.addRow("Provider",self.provider); f.addRow("Model",self.model); f.addRow("Base URL",self.base_url); f.addRow("API Key",self.api_key); f.addRow("Temperature",self.temperature)
        root.addWidget(box)
        box, f = self._section("Conversation", "Context controls used by the voice/conversation controller.")
        self.max_turns = QDoubleSpinBox(); self.max_turns.setRange(1,20); self.max_turns.setDecimals(0); self.max_turns.setValue(config.get("voice.max_context_turns",6))
        self.system_prompt = QLineEdit(config.get("voice.system_prompt","You are SAINT, a helpful AI assistant. Be concise."))
        f.addRow("Context Turns",self.max_turns); f.addRow("System Prompt",self.system_prompt); root.addWidget(box); root.addStretch()
        self._add_page(w)

    def _build_voice(self):
        w = QWidget(); root = QVBoxLayout(w)
        box, f = self._section("Input")
        self.voice_mode=QComboBox(); self.voice_mode.addItems(["always_on","push_to_talk"]); self.voice_mode.setCurrentText(config.get("voice.mode","always_on"))
        self.mic_sensitivity=QDoubleSpinBox(); self.mic_sensitivity.setRange(.001,.1); self.mic_sensitivity.setDecimals(3); self.mic_sensitivity.setSingleStep(.001); self.mic_sensitivity.setValue(config.get("voice.mic_sensitivity",.015))
        self.silence=QDoubleSpinBox(); self.silence.setRange(50,3000); self.silence.setDecimals(0); self.silence.setSuffix(" ms"); self.silence.setValue(config.get("voice.silence_duration_ms",700))
        self.noise=QCheckBox("Enabled"); self.noise.setChecked(config.get("voice.noise_suppression",True))
        f.addRow("Mode",self.voice_mode); f.addRow("Mic Sensitivity",self.mic_sensitivity); f.addRow("Silence Duration",self.silence); f.addRow("Noise Suppression",self.noise); root.addWidget(box)
        box, f = self._section("Speech Recognition (STT)")
        self.stt_backend=QComboBox(); self.stt_backend.addItems(["faster_whisper","mock"]); self.stt_backend.setCurrentText(config.get("voice.stt_backend","faster_whisper"))
        self.stt_model=QComboBox(); self.stt_model.addItems(["tiny.en","base.en","small.en","medium.en","large-v3"]); self.stt_model.setCurrentText(config.get("voice.stt_model","base.en"))
        self.stt_device=QComboBox(); self.stt_device.addItems(["cuda","cpu"]); self.stt_device.setCurrentText(config.get("voice.stt_device","cuda"))
        self.stt_compute=QComboBox(); self.stt_compute.addItems(["float16","int8","float32"]); self.stt_compute.setCurrentText(config.get("voice.stt_compute_type","float16"))
        self.stt_lang=QLineEdit(config.get("voice.stt_language","en"))
        f.addRow("Backend",self.stt_backend); f.addRow("Model",self.stt_model); f.addRow("Device",self.stt_device); f.addRow("Compute",self.stt_compute); f.addRow("Language",self.stt_lang); root.addWidget(box)
        box, f = self._section("Speech Output (TTS)")
        self.tts_backend=QComboBox(); self.tts_backend.addItems(["kokoro","qwen","mock"]); self.tts_backend.setCurrentText(config.get("voice.tts_backend","kokoro"))
        self.tts_voice=QLineEdit(config.get("voice.tts_voice","af_heart")); self.tts_device=QComboBox(); self.tts_device.addItems(["cuda","cpu"]); self.tts_device.setCurrentText(config.get("voice.tts_device","cuda"))
        self.tts_speed=QDoubleSpinBox(); self.tts_speed.setRange(.5,2); self.tts_speed.setSingleStep(.1); self.tts_speed.setValue(config.get("voice.tts_speed",1.0))
        f.addRow("Backend",self.tts_backend); f.addRow("Voice",self.tts_voice); f.addRow("Device",self.tts_device); f.addRow("Speed",self.tts_speed); root.addWidget(box); root.addStretch()
        self._add_page(w)

    def _build_integrations(self):
        w = QWidget(); root = QVBoxLayout(w)
        box, layout = self._section("Spotify", "Connect Spotify and configure its access without leaving Settings.", form=False)
        status_row=QHBoxLayout(); self.spotify_status=QLabel(); status_row.addWidget(self.spotify_status); status_row.addStretch()
        self.spotify_connect=QPushButton("Connect Spotify"); self.spotify_connect.clicked.connect(self._spotify_connect); status_row.addWidget(self.spotify_connect)
        self.spotify_disconnect=QPushButton("Disconnect"); self.spotify_disconnect.clicked.connect(self._spotify_disconnect); status_row.addWidget(self.spotify_disconnect)
        layout.addLayout(status_row)
        f=self._form()
        self.spotify_client_id=QLineEdit(config.get("spotify.client_id","")); self.spotify_client_id.setPlaceholderText("Spotify Developer Client ID")
        self.spotify_redirect=QLineEdit(config.get("spotify.redirect_uri","http://127.0.0.1:8888/callback"))
        self.spotify_device=QLineEdit(config.get("spotify.preferred_device","")); self.spotify_device.setPlaceholderText("Optional device ID")
        self.spotify_enabled=QCheckBox("Enable Spotify module"); self.spotify_enabled.setChecked(config.get("modules.spotify",False))
        f.addRow("Client ID",self.spotify_client_id); f.addRow("Redirect URI",self.spotify_redirect); f.addRow("Preferred Device",self.spotify_device); f.addRow("",self.spotify_enabled)
        layout.addLayout(f); root.addWidget(box)
        box, layout = self._section("Available modules", "Integrations are independently enabled so adding future modules stays clean.")
        self.integration_summary=QLabel()
        self.integration_summary.setWordWrap(True)
        layout.addWidget(self.integration_summary); root.addWidget(box); root.addStretch()
        self._refresh_spotify_status()
        self._add_page(w)

    def _refresh_spotify_status(self):
        m=module_manager.get("spotify")
        connected=bool(m and m.is_connected())
        self.spotify_status.setText("Connected" if connected else "Not connected")
        self.spotify_disconnect.setEnabled(connected)
        self.integration_summary.setText("\n".join(f"• {m.name}: {'Enabled' if m.enabled else 'Disabled'}" for m in module_manager.all_modules()))

    def _spotify_connect(self):
        if self._spotify_thread and self._spotify_thread.is_alive():
            return
        config.set("spotify.client_id",self.spotify_client_id.text().strip(),persist=True)
        config.set("spotify.redirect_uri",self.spotify_redirect.text().strip(),persist=True)
        m=module_manager.get("spotify"); m.enable()
        self.spotify_connect.setEnabled(False); self.spotify_status.setText("Waiting for Spotify login...")
        def run():
            try:
                m.connect()
            except Exception as exc:
                self._spotify_error=str(exc)
            else:
                self._spotify_error=""
            self._spotify_thread=None
        self._spotify_error=""
        self._spotify_thread=threading.Thread(target=run,daemon=True); self._spotify_thread.start()
        QTimer.singleShot(250,self._poll_spotify)

    def _poll_spotify(self):
        if self._spotify_thread and self._spotify_thread.is_alive():
            QTimer.singleShot(250,self._poll_spotify); return
        self.spotify_connect.setEnabled(True); self._refresh_spotify_status()
        if getattr(self,"_spotify_error",""):
            QMessageBox.warning(self,"Spotify",self._spotify_error)

    def _spotify_disconnect(self):
        m=module_manager.get("spotify"); m.disconnect(); self._refresh_spotify_status()

    def _build_permissions(self):
        w=QWidget(); root=QVBoxLayout(w)
        box,layout=self._section("Tool Access","Set access for individual capabilities. Allow runs automatically, Confirm asks before execution, Deny blocks it.", form=False)
        self.permission_rows={}
        tools=[
            ("spotify.play","Spotify Playback"),("spotify.pause","Spotify Pause"),("spotify.next","Spotify Skip"),
            ("spotify.search","Spotify Search"),("spotify.queue","Spotify Queue"),("spotify.add_to_playlist","Spotify Playlist Changes"),
        ]
        for key,label in tools:
            row=QHBoxLayout(); name=QLabel(label); row.addWidget(name); row.addStretch()
            combo=QComboBox(); combo.addItems(["allow","confirm","deny"]); combo.setCurrentText(permission_manager.get_policy(key,"allow"))
            combo.currentTextChanged.connect(lambda value,k=key: permission_manager.set_policy(k,value))
            row.addWidget(combo); layout.addLayout(row); self.permission_rows[key]=combo
        root.addWidget(box)
        box,layout=self._section("Automation","The existing automation safety mode remains available as a global control.", form=False)
        f=self._form(); self.auto_enabled=QCheckBox("Enabled"); self.auto_enabled.setChecked(config.get("automation.enabled",False))
        self.auto_mode=QComboBox(); self.auto_mode.addItems(["safe","confirm","autonomous"]); self.auto_mode.setCurrentText(config.get("automation.permission_mode","confirm"))
        self.auto_danger=QCheckBox("Confirm dangerous actions"); self.auto_danger.setChecked(config.get("automation.confirm_dangerous",True))
        self.auto_timeout=QDoubleSpinBox(); self.auto_timeout.setRange(5,300); self.auto_timeout.setDecimals(0); self.auto_timeout.setSuffix(" sec"); self.auto_timeout.setValue(config.get("automation.command_timeout",30))
        f.addRow("Automation",self.auto_enabled); f.addRow("Permission Mode",self.auto_mode); f.addRow("Dangerous Actions",self.auto_danger); f.addRow("Command Timeout",self.auto_timeout); layout.addLayout(f); root.addWidget(box); root.addStretch()
        self._add_page(w)

    def _build_analytics(self):
        w=QWidget(); root=QVBoxLayout(w)
        box,f=self._section("Analytics & Logging")
        self.analytics=QCheckBox("Enabled"); self.analytics.setChecked(config.get("analytics.enabled",True))
        self.logging=QComboBox(); self.logging.addItems(["Verbose","Normal","Errors Only"]); self.logging.setCurrentText(config.get("logging.level","Verbose"))
        f.addRow("Analytics",self.analytics); f.addRow("Logging Level",self.logging); root.addWidget(box); root.addStretch(); self._add_page(w)

    def _build_memory(self):
        w=QWidget(); root=QVBoxLayout(w)
        box,f=self._section("Memory")
        self.memory=QCheckBox("Enabled"); self.memory.setChecked(config.get("memory.enabled",False))
        self.retention=QDoubleSpinBox(); self.retention.setRange(1,365); self.retention.setDecimals(0); self.retention.setSuffix(" days"); self.retention.setValue(config.get("memory.retention_days",30))
        self.memory_turns=QDoubleSpinBox(); self.memory_turns.setRange(10,1000); self.memory_turns.setDecimals(0); self.memory_turns.setValue(config.get("memory.max_conversation_turns",100))
        f.addRow("Memory",self.memory); f.addRow("Retention",self.retention); f.addRow("Max Conversation Turns",self.memory_turns); root.addWidget(box); root.addStretch(); self._add_page(w)

    def _build_advanced(self):
        w=QWidget(); root=QVBoxLayout(w)
        box,f=self._section("System / Developer")
        self.startup=QCheckBox("Run startup system check"); self.startup.setChecked(config.get("system.startup_check",True))
        self.syslog=QComboBox(); self.syslog.addItems(["Verbose","Normal","Errors Only"]); self.syslog.setCurrentText(config.get("system.log_level","Verbose"))
        f.addRow("Startup Check",self.startup); f.addRow("System Log Level",self.syslog); root.addWidget(box); root.addStretch(); self._add_page(w)

    def save(self):
        config.set("theme",self.theme_combo.currentText(),persist=False); config.set("auto_save",self.autosave.isChecked(),persist=False)
        config.set("dashboard.complexity",self.dash_complexity.currentText(),persist=False)
        config.set("ai.provider",self.provider.currentText(),persist=False); config.set("ai.model",self.model.text(),persist=False)
        config.set("ai.base_url",self.base_url.text(),persist=False); config.set("ai.api_key",self.api_key.text(),persist=False); config.set("ai.temperature",self.temperature.value(),persist=False)
        config.set("voice.mode",self.voice_mode.currentText(),persist=False); config.set("voice.mic_sensitivity",self.mic_sensitivity.value(),persist=False)
        config.set("voice.silence_duration_ms",int(self.silence.value()),persist=False); config.set("voice.noise_suppression",self.noise.isChecked(),persist=False)
        config.set("voice.stt_backend",self.stt_backend.currentText(),persist=False); config.set("voice.stt_model",self.stt_model.currentText(),persist=False)
        config.set("voice.stt_device",self.stt_device.currentText(),persist=False); config.set("voice.stt_compute_type",self.stt_compute.currentText(),persist=False); config.set("voice.stt_language",self.stt_lang.text(),persist=False)
        config.set("voice.tts_backend",self.tts_backend.currentText(),persist=False); config.set("voice.tts_voice",self.tts_voice.text(),persist=False); config.set("voice.tts_device",self.tts_device.currentText(),persist=False); config.set("voice.tts_speed",self.tts_speed.value(),persist=False)
        config.set("voice.max_context_turns",int(self.max_turns.value()),persist=False); config.set("voice.system_prompt",self.system_prompt.text(),persist=False)
        config.set("spotify.client_id",self.spotify_client_id.text().strip(),persist=False); config.set("spotify.redirect_uri",self.spotify_redirect.text().strip(),persist=False); config.set("spotify.preferred_device",self.spotify_device.text().strip(),persist=False)
        config.set("modules.spotify",self.spotify_enabled.isChecked(),persist=False)
        config.set("analytics.enabled",self.analytics.isChecked(),persist=False); config.set("logging.level",self.logging.currentText(),persist=False)
        config.set("automation.enabled",self.auto_enabled.isChecked(),persist=False); config.set("automation.permission_mode",self.auto_mode.currentText(),persist=False)
        config.set("automation.confirm_dangerous",self.auto_danger.isChecked(),persist=False); config.set("automation.command_timeout",int(self.auto_timeout.value()),persist=False)
        config.set("memory.enabled",self.memory.isChecked(),persist=False); config.set("memory.retention_days",int(self.retention.value()),persist=False); config.set("memory.max_conversation_turns",int(self.memory_turns.value()),persist=False)
        config.set("system.startup_check",self.startup.isChecked(),persist=False); config.set("system.log_level",self.syslog.currentText(),persist=False)
        config.save(); module_manager.set_enabled("spotify",self.spotify_enabled.isChecked())
        event_bus.emit_event(EventType.SETTINGS_CHANGED,{})
        if self.on_theme_changed: self.on_theme_changed(self.theme_combo.currentText())
        self.status.setText("Settings saved.")

"""
ui/health_ui.py

Comprehensive health monitor with detailed diagnostics for all subsystems.
"""

import platform
import sys
import psutil
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QGridLayout, QLabel, QFrame, QScrollArea,
)
from PySide6.QtCore import QTimer, QThread, Signal

class OllamaPingWorker(QThread):
    result = Signal(bool)
    
    def __init__(self, base_url, parent=None):
        super().__init__(parent)
        self.base_url = base_url
        
    def run(self):
        try:
            import requests
            resp = requests.get(self.base_url.rstrip("/") + "/api/version", timeout=2)
            self.result.emit(resp.status_code == 200)
        except Exception:
            self.result.emit(False)


from core.state import app_state
from core.analytics import analytics
from core.config import config
from core.module_manager import module_manager
from modules.ai.module import AIModule
from modules.memory.module import MemoryModule
from modules.automation.module import AutomationModule


class HealthTile(QFrame):
    def __init__(self, title, value="--"):
        super().__init__()
        self.setObjectName("StatCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("StatTitle")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("StatValue")
        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)

    def set_value(self, value, warn=False, status: str = "ok"):
        """Set value with status: ok, warning, error, disabled, not_installed"""
        self.value_label.setText(str(value))
        if status == "warning":
            self.value_label.setObjectName("StatValueWarn")
        elif status == "error":
            self.value_label.setObjectName("StatValueWarn")
        elif status == "disabled":
            self.value_label.setObjectName("StatValueWarn")
        elif status == "not_installed":
            self.value_label.setObjectName("StatValueWarn")
        else:
            self.value_label.setObjectName("StatValue")
        self.value_label.setStyleSheet("")


class HealthUI(QWidget):
    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(20)

        title = QLabel("Health Monitor")
        title.setObjectName("PageTitle")
        root.addWidget(title)

        # Scrollable content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setSpacing(20)
        scroll.setWidget(content)
        root.addWidget(scroll)

        # --- System Health ---
        self._add_section("System")
        sys_grid = QGridLayout()
        sys_grid.setSpacing(14)

        self.tile_python = HealthTile("Python Version", platform.python_version())
        self.tile_os = HealthTile("OS", f"{platform.system()} {platform.release()}")
        self.tile_cpu = HealthTile("CPU", f"{psutil.cpu_count()} cores")
        self.tile_ram = HealthTile("RAM", f"{psutil.virtual_memory().total / (1024**3):.1f} GB")
        self.tile_disk = HealthTile("Disk", "N/A")
        self.tile_uptime = HealthTile("App Uptime", "00:00:00")

        sys_tiles = [self.tile_python, self.tile_os, self.tile_cpu, self.tile_ram, self.tile_disk, self.tile_uptime]
        for i, tile in enumerate(sys_tiles):
            sys_grid.addWidget(tile, i // 3, i % 3)

        self._content_layout.addLayout(sys_grid)

        # --- Subsystem Health ---
        self._add_section("Subsystems")
        sub_grid = QGridLayout()
        sub_grid.setSpacing(14)

        self.tile_ollama = HealthTile("Ollama", "Not Configured")
        self.tile_model = HealthTile("AI Model", "Not Selected")
        self.tile_voice = HealthTile("Voice Input", "Disabled")
        self.tile_tts = HealthTile("TTS", "Disabled")
        self.tile_memory = HealthTile("Memory", "Disabled")
        self.tile_automation = HealthTile("Automation", "Disabled")
        self.tile_vision = HealthTile("Vision", "Disabled")

        sub_tiles = [self.tile_ollama, self.tile_model, self.tile_voice, self.tile_tts, self.tile_memory, self.tile_automation, self.tile_vision]
        for i, tile in enumerate(sub_tiles):
            sub_grid.addWidget(tile, i // 4, i % 4)

        self._content_layout.addLayout(sub_grid)

        # --- Performance & Errors ---
        self._add_section("Performance & Errors")
        perf_grid = QGridLayout()
        perf_grid.setSpacing(14)

        self.tile_status = HealthTile("Overall Status", "Unknown")
        self.tile_errors = HealthTile("Errors", "0")
        self.tile_crashes = HealthTile("Module Crashes", "0")
        self.tile_avg_response = HealthTile("Avg Response", "0 sec")
        self.tile_app_ram = HealthTile("App RAM", "0 MB")
        self.tile_cpu_pct = HealthTile("CPU Usage", "0%")

        perf_tiles = [self.tile_status, self.tile_errors, self.tile_crashes, self.tile_avg_response, self.tile_app_ram, self.tile_cpu_pct]
        for i, tile in enumerate(perf_tiles):
            perf_grid.addWidget(tile, i // 3, i % 3)

        self._content_layout.addLayout(perf_grid)

        self._content_layout.addStretch()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(5000)  # Reduced from 2000ms to 5000ms
        self.refresh()

    def _add_section(self, title: str):
        label = QLabel(title)
        label.setObjectName("SectionTitle")
        self._content_layout.addWidget(label)

    def refresh(self):
        # System
        self.tile_uptime.set_value(app_state.uptime_formatted())
        self.tile_cpu_pct.set_value(f"{app_state.cpu_percent():.0f}%")
        self.tile_app_ram.set_value(f"{app_state.ram_mb():.0f} MB")

        try:
            disk = psutil.disk_usage(".")
            self.tile_disk.set_value(f"{disk.free / (1024**3):.1f} GB free")
        except Exception:
            self.tile_disk.set_value("N/A")

        # Analytics
        errors = app_state.error_count
        crashes = app_state.module_crash_count
        snap = analytics.snapshot()

        healthy = errors == 0 and crashes == 0
        self.tile_status.set_value("Healthy" if healthy else "Degraded", status="ok" if healthy else "warning")
        self.tile_errors.set_value(errors, status="error" if errors > 0 else "ok")
        self.tile_crashes.set_value(crashes, status="error" if crashes > 0 else "ok")
        self.tile_avg_response.set_value(f'{snap["average_response"]:.2f} sec')

        # AI / Ollama
        provider = config.get("ai.provider", "mock")
        model = config.get("ai.model", "llama3")
        base_url = config.get("ai.base_url", "http://localhost:11434")

        if provider == "ollama":
            if not hasattr(self, '_ollama_worker') or not self._ollama_worker.isRunning():
                self._ollama_worker = OllamaPingWorker(base_url, self)
                def on_result(ok):
                    if ok:
                        self.tile_ollama.set_value("Connected", status="ok")
                        self.tile_model.set_value(model, status="ok")
                    else:
                        self.tile_ollama.set_value("Error/Offline", status="error")
                        self.tile_model.set_value("N/A", status="error")
                self._ollama_worker.result.connect(on_result)
                self._ollama_worker.start()
            
            # Use cached state from last poll while worker is running
            if not hasattr(self, '_ollama_worker'):
                self.tile_ollama.set_value("Checking...", status="warning")
                self.tile_model.set_value(model, status="ok")
        elif provider == "openai":
            self.tile_ollama.set_value("OpenAI Compatible", status="ok")
            self.tile_model.set_value(model, status="ok")
        else:
            self.tile_ollama.set_value("Mock Provider", status="ok")
            self.tile_model.set_value("Mock", status="ok")

        # Voice
        voice_enabled = config.get("modules.voice", False)
        if voice_enabled:
            stt_backend = config.get("voice.stt_backend", "faster_whisper")
            self.tile_voice.set_value(f"Enabled ({stt_backend})", status="ok")
        else:
            self.tile_voice.set_value("Disabled", status="disabled")

        # TTS
        tts_backend = config.get("voice.tts_backend", "kokoro")
        if voice_enabled:
            self.tile_tts.set_value(f"Enabled ({tts_backend})", status="ok")
        else:
            self.tile_tts.set_value("Disabled", status="disabled")

        # Memory
        mem_enabled = config.get("modules.memory", False)
        if mem_enabled:
            self.tile_memory.set_value("Enabled", status="ok")
        else:
            self.tile_memory.set_value("Disabled", status="disabled")

        # Automation
        auto_enabled = config.get("modules.automation", False)
        if auto_enabled:
            self.tile_automation.set_value("Enabled", status="ok")
        else:
            self.tile_automation.set_value("Disabled", status="disabled")

        # Vision
        vision_enabled = config.get("modules.vision", False)
        if vision_enabled:
            self.tile_vision.set_value("Enabled", status="ok")
        else:
            self.tile_vision.set_value("Disabled", status="disabled")
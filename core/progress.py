"""
core/progress.py

Project progress tracking system for SAINT Revitalized.
Tracks completion of major components based on explicit criteria.
"""

import json
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from enum import Enum


class ComponentStatus(Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PARTIAL = "partial"
    COMPLETE = "complete"
    BROKEN = "broken"


@dataclass
class Criterion:
    name: str
    description: str
    met: bool = False
    evidence: str = ""


@dataclass
class ComponentProgress:
    name: str
    display_name: str
    criteria: List[Criterion] = field(default_factory=list)
    status: ComponentStatus = ComponentStatus.NOT_STARTED
    last_updated: float = field(default_factory=time.time)

    def completion_percentage(self) -> int:
        if not self.criteria:
            return 0
        met = sum(1 for c in self.criteria if c.met)
        return int(round(met / len(self.criteria) * 100))

    def update_status(self):
        pct = self.completion_percentage()
        if pct == 100:
            self.status = ComponentStatus.COMPLETE
        elif pct > 0:
            self.status = ComponentStatus.PARTIAL
        else:
            self.status = ComponentStatus.NOT_STARTED
        self.last_updated = time.time()

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "completion": self.completion_percentage(),
            "status": self.status.value,
            "criteria": [
                {"name": c.name, "description": c.description, "met": c.met, "evidence": c.evidence}
                for c in self.criteria
            ],
            "last_updated": self.last_updated,
        }


class ProgressTracker:
    """Tracks project progress across all components."""

    # Component definitions with explicit criteria
    COMPONENTS = {
        "core": ComponentProgress(
            name="core",
            display_name="Core",
            criteria=[
                Criterion("event_bus", "Event Bus routes all module communication"),
                Criterion("config", "JSON-backed configuration with persistence"),
                Criterion("logging", "Rotating file logs + console mirror"),
                Criterion("state", "Runtime state: uptime, CPU, RAM, event/error counters"),
                Criterion("analytics", "Persistent analytics with latency tracking"),
                Criterion("module_manager", "Module lifecycle: load/enable/disable/completion%"),
            ],
        ),
        "ai": ComponentProgress(
            name="ai",
            display_name="AI",
            criteria=[
                Criterion("mock_provider", "Mock provider works for testing"),
                Criterion("openai_provider", "OpenAI-compatible provider (SSE streaming)"),
                Criterion("ollama_provider", "Ollama provider with NDJSON streaming"),
                Criterion("connection_test", "Provider connection testing"),
                Criterion("model_discovery", "Model listing and discovery"),
                Criterion("streaming", "Token-by-token streaming with callbacks"),
                Criterion("context_window", "Conversation context with interruption handling"),
                Criterion("cancellation", "In-flight request cancellation"),
            ],
        ),
        "gui": ComponentProgress(
            name="gui",
            display_name="GUI",
            criteria=[
                Criterion("main_window", "Sidebar navigation + stacked pages"),
                Criterion("dashboard", "Status cards, AI test panel, live refresh"),
                Criterion("module_manager_ui", "Module list with toggles and subtasks"),
                Criterion("analytics_ui", "Latency dashboard with goals"),
                Criterion("health_ui", "Health monitor tiles"),
                Criterion("console_ui", "Live event stream with backfill"),
                Criterion("settings_ui", "All settings sections with persistence"),
                Criterion("voice_ui", "Waveform, transcript, response streaming"),
                Criterion("theming", "Dark/Light theme switching"),
            ],
        ),
        "voice": ComponentProgress(
            name="voice",
            display_name="Voice Input",
            criteria=[
                Criterion("audio_capture", "Microphone capture via sounddevice"),
                Criterion("vad", "Voice Activity Detection (RMS + Silero)"),
                Criterion("stt_faster_whisper", "faster-whisper STT with CUDA fallback"),
                Criterion("stt_mock", "Mock STT for testing"),
                Criterion("always_on_mode", "Always-on listening mode"),
                Criterion("push_to_talk", "Push-to-talk mode"),
                Criterion("wake_word", "Wake word detection (optional)"),
                Criterion("interrupt_detection", "User interruption while speaking"),
                Criterion("mic_selection", "Microphone device selection"),
                Criterion("noise_suppression", "Rolling noise floor subtraction"),
            ],
        ),
        "tts": ComponentProgress(
            name="tts",
            display_name="Text-to-Speech",
            criteria=[
                Criterion("kokoro_tts", "kokoro-onnx TTS with GPU acceleration"),
                Criterion("mock_tts", "Mock TTS for testing"),
                Criterion("sentence_streaming", "Sentence-by-sentence streaming"),
                Criterion("voice_selection", "Configurable voice selection"),
                Criterion("speed_control", "Adjustable speech rate"),
                Criterion("interruptible", "TTS interruption support"),
                Criterion("device_selection", "CUDA/CPU device selection"),
            ],
        ),
        "memory": ComponentProgress(
            name="memory",
            display_name="Memory",
            criteria=[
                Criterion("sqlite_backend", "SQLite persistent storage"),
                Criterion("conversation_history", "Conversation turn storage"),
                Criterion("long_term_memory", "Explicit fact storage with confidence"),
                Criterion("preferences", "User preference persistence"),
                Criterion("tasks", "Task CRUD with status/priority"),
                Criterion("project_state", "SAINT project state tracking"),
                Criterion("search", "Text-based memory search"),
                Criterion("management", "Forget/retention policies"),
                Criterion("persistence", "Survives application restarts"),
            ],
        ),
        "automation": ComponentProgress(
            name="automation",
            display_name="Automation",
            criteria=[
                Criterion("tool_registry", "Extensible tool registry with permissions"),
                Criterion("mouse_control", "Move, click, scroll"),
                Criterion("keyboard_control", "Type, press key, hotkeys"),
                Criterion("window_mgmt", "List, focus, close windows"),
                Criterion("screenshots", "Full/region screenshot capture"),
                Criterion("file_ops", "Read/write/list/search files"),
                Criterion("shell_commands", "Command execution with timeout"),
                Criterion("permission_system", "LOW/MEDIUM/HIGH with SAFE/CONFIRM/AUTONOMOUS modes"),
            ],
        ),
        "vision": ComponentProgress(
            name="vision",
            display_name="Vision",
            criteria=[
                Criterion("screen_capture", "Screenshot capture"),
                Criterion("ocr", "Optical Character Recognition"),
                Criterion("object_detection", "Object detection on screen"),
                Criterion("vision_model", "Multimodal model integration"),
            ],
        ),
        "analytics": ComponentProgress(
            name="analytics",
            display_name="Analytics",
            criteria=[
                Criterion("session_tracking", "Session duration and counts"),
                Criterion("latency_metrics", "STT/Intent/Model/TTS/Overall latency"),
                Criterion("conversation_counters", "Interruptions, cancellations, false wakes"),
                Criterion("recognition_accuracy", "STT confidence tracking"),
                Criterion("persistence", "Analytics survive restarts"),
                Criterion("dashboard", "Live dashboard with goals"),
            ],
        ),
    }

    def __init__(self, path: str = "data/progress.json"):
        self._path = path
        self._components = dict(self.COMPONENTS)
        self._load()

    def _load(self):
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for name, comp_data in data.items():
                    if name in self._components:
                        comp = self._components[name]
                        comp.status = ComponentStatus(comp_data.get("status", "not_started"))
                        for c_data in comp_data.get("criteria", []):
                            for c in comp.criteria:
                                if c.name == c_data["name"]:
                                    c.met = c_data.get("met", False)
                                    c.evidence = c_data.get("evidence", "")
                        comp.update_status()
            except (json.JSONDecodeError, OSError):
                pass

    def save(self):
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        data = {name: comp.to_dict() for name, comp in self._components.items()}
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def get_component(self, name: str) -> Optional[ComponentProgress]:
        return self._components.get(name)

    def get_all(self) -> Dict[str, ComponentProgress]:
        return dict(self._components)

    def set_criterion(self, component: str, criterion: str, met: bool, evidence: str = ""):
        comp = self._components.get(component)
        if comp:
            for c in comp.criteria:
                if c.name == criterion:
                    c.met = met
                    c.evidence = evidence
                    break
            comp.update_status()
            self.save()

    def get_overall_progress(self) -> Dict:
        total_criteria = sum(len(c.criteria) for c in self._components.values())
        met_criteria = sum(
            sum(1 for c in comp.criteria if c.met)
            for comp in self._components.values()
        )
        overall_pct = int(round(met_criteria / total_criteria * 100)) if total_criteria else 0
        
        return {
            "overall_percentage": overall_pct,
            "components": {name: comp.to_dict() for name, comp in self._components.items()},
        }

    def format_report(self) -> str:
        """Generate a formatted progress report."""
        lines = ["SAINT REVITALIZED", "=" * 40]
        overall = self.get_overall_progress()
        
        for name, comp in self._components.items():
            pct = comp.completion_percentage()
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            status = comp.status.value.upper()
            lines.append(f"{comp.display_name:12} {bar} {pct:3d}%  [{status}]")
        
        lines.append("-" * 40)
        overall_pct = overall["overall_percentage"]
        bar = "█" * (overall_pct // 10) + "░" * (10 - overall_pct // 10)
        lines.append(f"{'Overall':12} {bar} {overall_pct:3d}%")
        
        return "\n".join(lines)


# Singleton
_progress_tracker: Optional[ProgressTracker] = None


def get_progress_tracker() -> ProgressTracker:
    global _progress_tracker
    if _progress_tracker is None:
        _progress_tracker = ProgressTracker()
    return _progress_tracker
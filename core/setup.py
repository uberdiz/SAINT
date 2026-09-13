"""
core/setup.py

First-run setup experience for SAINT.
"""

import platform
import sys
import subprocess
import os
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from core.config import config
from core.events import event_bus, EventType
from modules.ai.providers import get_provider


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


class SetupWizard:
    """Runs system checks and guides user through first-time setup."""

    def __init__(self):
        self.results: List[CheckResult] = []
        self._first_run = not os.path.exists("data/config.json")

    def is_first_run(self) -> bool:
        return self._first_run

    def run_checks(self) -> List[CheckResult]:
        """Run all system checks."""
        self.results = []

        # 1. Python version
        self._check_python()

        # 2. OS
        self._check_os()

        # 3. Dependencies
        self._check_dependencies()

        # 4. Ollama
        self._check_ollama()

        # 5. Microphone
        self._check_microphone()

        # 6. TTS
        self._check_tts()

        # 7. Automation
        self._check_automation()

        # 8. Database
        self._check_database()

        # 9. Data directories
        self._check_directories()

        return self.results

    def _check_python(self):
        version = sys.version_info
        passed = version.major == 3 and version.minor >= 10
        self.results.append(CheckResult(
            name="Python",
            passed=passed,
            message=f"Python {version.major}.{version.minor}.{version.micro}",
            details={"version": f"{version.major}.{version.minor}.{version.micro}", "required": ">=3.10"}
        ))

    def _check_os(self):
        system = platform.system()
        release = platform.release()
        passed = system == "Windows" and release in ("10", "11")
        self.results.append(CheckResult(
            name="Operating System",
            passed=passed,
            message=f"{system} {release}",
            details={"system": system, "release": release, "supported": passed}
        ))

    def _check_dependencies(self):
        # This is a simplified check - in reality, imports would happen at startup
        try:
            import PySide6
            import requests
            import psutil
            import numpy
            import sounddevice
            import soundfile
            passed = True
            msg = "All core dependencies available"
        except ImportError as e:
            passed = False
            msg = f"Missing dependency: {e}"

        self.results.append(CheckResult(
            name="Dependencies",
            passed=passed,
            message=msg,
            details={}
        ))

    def _check_ollama(self):
        provider_name = config.get("ai.provider", "ollama")
        if provider_name != "ollama":
            self.results.append(CheckResult(
                name="Ollama",
                passed=True,
                message=f"Using {provider_name} provider (Ollama not required)",
                details={"provider": provider_name}
            ))
            return

        base_url = config.get("ai.base_url", "http://localhost:11434")
        try:
            import requests
            resp = requests.get(base_url.rstrip("/") + "/api/version", timeout=3)
            if resp.status_code == 200:
                version = resp.json().get("version", "unknown")
                self.results.append(CheckResult(
                    name="Ollama",
                    passed=True,
                    message=f"Connected (v{version})",
                    details={"version": version, "url": base_url}
                ))

                # Check for models
                try:
                    models_resp = requests.get(base_url.rstrip("/") + "/api/tags", timeout=3)
                    if models_resp.status_code == 200:
                        models = models_resp.json().get("models", [])
                        model_names = [m["name"] for m in models]
                        configured_model = config.get("ai.model", "llama3")
                        model_found = any(configured_model in m for m in model_names)
                        if model_found:
                            self.results.append(CheckResult(
                                name="AI Model",
                                passed=True,
                                message=f"Model '{configured_model}' found",
                                details={"available": model_names, "selected": configured_model}
                            ))
                        else:
                            self.results.append(CheckResult(
                                name="AI Model",
                                passed=False,
                                message=f"Model '{configured_model}' not found. Run: ollama pull {configured_model}",
                                details={"available": model_names, "selected": configured_model}
                            ))
                except Exception:
                    pass
            else:
                self.results.append(CheckResult(
                    name="Ollama",
                    passed=False,
                    message=f"Ollama responded with HTTP {resp.status_code}",
                    details={"status": resp.status_code}
                ))
        except Exception as e:
            self.results.append(CheckResult(
                name="Ollama",
                passed=False,
                message=f"Cannot connect: {e}",
                details={"error": str(e), "url": base_url}
            ))

    def _check_microphone(self):
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            input_devices = [d for d in devices if d["max_input_channels"] > 0]
            if input_devices:
                self.results.append(CheckResult(
                    name="Microphone",
                    passed=True,
                    message=f"{len(input_devices)} input device(s) found",
                    details={"devices": [{"index": i, "name": d["name"]} for i, d in enumerate(input_devices)]}
                ))
            else:
                self.results.append(CheckResult(
                    name="Microphone",
                    passed=False,
                    message="No input devices found",
                    details={}
                ))
        except Exception as e:
            self.results.append(CheckResult(
                name="Microphone",
                passed=False,
                message=f"Cannot query audio devices: {e}",
                details={"error": str(e)}
            ))

    def _check_tts(self):
        tts_backend = config.get("voice.tts_backend", "kokoro")
        if tts_backend == "kokoro":
            # Try to import the kokoro package to verify it's available
            try:
                import kokoro
                self.results.append(CheckResult(
                    name="TTS (Kokoro)",
                    passed=True,
                    message="Kokoro package available (models download on first use)",
                    details={"package": "kokoro"}
                ))
            except ImportError:
                self.results.append(CheckResult(
                    name="TTS (Kokoro)",
                    passed=False,
                    message="kokoro package not installed. Run: pip install kokoro",
                    details={}
                ))
        elif tts_backend == "qwen":
            # Qwen requires torch + transformers
            try:
                import torch
                from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
                self.results.append(CheckResult(
                    name="TTS (Qwen)",
                    passed=True,
                    message="Qwen TTS available (models download on first use)",
                    details={"package": "qwen_tts"}
                ))
            except ImportError as e:
                self.results.append(CheckResult(
                    name="TTS (Qwen)",
                    passed=False,
                    message=f"Qwen TTS not available: {e}",
                    details={}
                ))
        else:
            self.results.append(CheckResult(
                name="TTS",
                passed=True,
                message=f"Using {tts_backend} backend",
                details={}
            ))

    def _check_automation(self):
        try:
            import pyautogui
            import pygetwindow
            self.results.append(CheckResult(
                name="Automation",
                passed=True,
                message="pyautogui and pygetwindow available",
                details={}
            ))
        except ImportError:
            self.results.append(CheckResult(
                name="Automation",
                passed=False,
                message="pyautogui or pygetwindow not installed",
                details={}
            ))

    def _check_database(self):
        try:
            import sqlite3
            conn = sqlite3.connect(":memory:")
            conn.execute("SELECT 1")
            conn.close()
            self.results.append(CheckResult(
                name="Database (SQLite)",
                passed=True,
                message="SQLite available",
                details={"version": sqlite3.sqlite_version}
            ))
        except Exception as e:
            self.results.append(CheckResult(
                name="Database (SQLite)",
                passed=False,
                message=f"SQLite error: {e}",
                details={"error": str(e)}
            ))

    def _check_directories(self):
        dirs = ["data", "data/logs", "data/memory"]
        missing = [d for d in dirs if not os.path.exists(d)]
        if not missing:
            self.results.append(CheckResult(
                name="Data Directories",
                passed=True,
                message="All directories exist",
                details={}
            ))
        else:
            # Create them
            for d in missing:
                os.makedirs(d, exist_ok=True)
            self.results.append(CheckResult(
                name="Data Directories",
                passed=True,
                message=f"Created missing directories: {', '.join(missing)}",
                details={"created": missing}
            ))

    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of check results."""
        passed = sum(1 for r in self.results if r.passed)
        total = len(self.results)
        return {
            "first_run": self._first_run,
            "passed": passed,
            "total": total,
            "all_passed": passed == total,
            "results": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "message": r.message,
                    "details": r.details,
                }
                for r in self.results
            ]
        }

    def format_report(self) -> str:
        """Format a human-readable report."""
        lines = ["SAINT SYSTEM CHECK", "=" * 40]
        for r in self.results:
            status = "✓" if r.passed else "✗"
            lines.append(f"{status} {r.name:20} {r.message}")
        lines.append("=" * 40)

        passed = sum(1 for r in self.results if r.passed)
        total = len(self.results)

        if passed == total:
            lines.append("SAINT is ready.")
        else:
            lines.append(f"{total - passed} check(s) need attention.")

        return "\n".join(lines)


def run_first_run_setup() -> Dict[str, Any]:
    """Run the complete first-run setup and return results."""
    wizard = SetupWizard()
    if not wizard.is_first_run():
        return {"first_run": False, "skipped": True}

    wizard.run_checks()
    return wizard.get_summary()

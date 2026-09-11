"""
ui/analytics_ui.py

Latency Dashboard — Milestone 1.

Displays the full set of voice pipeline metrics as described in the spec:
each tile shows the current average and a goal target so you immediately
know what needs work.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QGridLayout, QLabel, QFrame, QScrollArea,
)
from PySide6.QtCore import QTimer, Qt

from core.analytics import analytics


# ---------------------------------------------------------------------------
# Latency tile with goal indicator
# ---------------------------------------------------------------------------
class LatencyTile(QFrame):
    """
    Shows:
        TITLE
        342 ms        ← current value
        ↓ Goal 250 ms ← goal (green if beating it, amber/red if not)
    """

    def __init__(self, title: str, goal_ms: float = 0, unit: str = "ms"):
        super().__init__()
        self.setObjectName("StatCard")
        self._goal_ms = goal_ms
        self._unit = unit

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)

        self._title = QLabel(title)
        self._title.setObjectName("StatTitle")

        self._value = QLabel("--")
        self._value.setObjectName("StatValue")
        self._value.setMinimumHeight(32)

        self._goal_label = QLabel(f"Goal  {goal_ms:.0f} {unit}" if goal_ms else "")
        self._goal_label.setObjectName("TileGoal")

        layout.addWidget(self._title)
        layout.addWidget(self._value)
        if goal_ms:
            layout.addWidget(self._goal_label)

    def set_value(self, val):
        """val can be float (ms) or str."""
        if isinstance(val, float):
            self._value.setText(f"{val:.0f} {self._unit}" if val > 0 else "--")
            if self._goal_ms and val > 0:
                if val <= self._goal_ms:
                    self._value.setObjectName("StatValueGood")
                    self._goal_label.setObjectName("TileGoalMet")
                else:
                    overshoot = (val - self._goal_ms) / self._goal_ms
                    if overshoot < 0.3:
                        self._value.setObjectName("StatValue")
                        self._goal_label.setObjectName("TileGoal")
                    else:
                        self._value.setObjectName("StatValueWarn")
                        self._goal_label.setObjectName("TileGoalMiss")
                self._value.style().unpolish(self._value)
                self._value.style().polish(self._value)
                self._goal_label.style().unpolish(self._goal_label)
                self._goal_label.style().polish(self._goal_label)
        else:
            self._value.setText(str(val) if val not in (0, "0", "") else "--")


class CounterTile(QFrame):
    """Simple counter tile (no goal bar)."""

    def __init__(self, title: str, unit: str = ""):
        super().__init__()
        self.setObjectName("StatCard")
        self._unit = unit
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)
        self._title = QLabel(title)
        self._title.setObjectName("StatTitle")
        self._value = QLabel("0")
        self._value.setObjectName("StatValue")
        layout.addWidget(self._title)
        layout.addWidget(self._value)

    def set_value(self, val):
        text = f"{val} {self._unit}".strip() if self._unit else str(val)
        self._value.setText(text)


# ---------------------------------------------------------------------------
# Analytics page
# ---------------------------------------------------------------------------
class AnalyticsUI(QWidget):
    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(20)

        title = QLabel("Analytics & Latency Dashboard")
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

        # --- Latency section (voice pipeline) ----------------------------
        lat_label = QLabel("Voice Pipeline Latency")
        lat_label.setObjectName("SectionTitle")
        self._content_layout.addWidget(lat_label)

        lat_grid = QGridLayout()
        lat_grid.setSpacing(12)

        self.t_overall   = LatencyTile("Overall Response",  goal_ms=250)
        self.t_stt       = LatencyTile("STT Latency",       goal_ms=100)
        self.t_model     = LatencyTile("Model Latency",     goal_ms=800)
        self.t_tts       = LatencyTile("TTS Latency",       goal_ms=60)
        self.t_intent    = LatencyTile("Intent Latency",    goal_ms=50)
        self.t_wake_word = LatencyTile("Wake Word Latency", goal_ms=200)

        lat_tiles = [
            self.t_overall, self.t_stt, self.t_model,
            self.t_tts, self.t_intent, self.t_wake_word,
        ]
        for i, tile in enumerate(lat_tiles):
            lat_grid.addWidget(tile, i // 3, i % 3)

        self._content_layout.addLayout(lat_grid)

        # --- Conversation counters ----------------------------------------
        conv_label = QLabel("Conversation")
        conv_label.setObjectName("SectionTitle")
        self._content_layout.addWidget(conv_label)

        conv_grid = QGridLayout()
        conv_grid.setSpacing(12)

        self.t_interruptions   = CounterTile("Interruptions")
        self.t_cancelled       = CounterTile("Cancelled Tasks")
        self.t_false_wake      = CounterTile("False Wake Words")
        self.t_accuracy        = LatencyTile("Recognition Accuracy", unit="%")

        conv_tiles = [
            self.t_interruptions, self.t_cancelled,
            self.t_false_wake, self.t_accuracy,
        ]
        for i, tile in enumerate(conv_tiles):
            conv_grid.addWidget(tile, 0, i)

        self._content_layout.addLayout(conv_grid)

        # --- Legacy session stats ----------------------------------------
        sess_label = QLabel("Session")
        sess_label.setObjectName("SectionTitle")
        self._content_layout.addWidget(sess_label)

        sess_grid = QGridLayout()
        sess_grid.setSpacing(12)

        self.t_runtime   = CounterTile("Runtime")
        self.t_commands  = CounterTile("Commands")
        self.t_llm_errs  = CounterTile("LLM Errors")
        self.t_crashes   = CounterTile("Module Crashes")
        self.t_events    = CounterTile("Total Events")

        sess_tiles = [
            self.t_runtime, self.t_commands, self.t_llm_errs,
            self.t_crashes, self.t_events,
        ]
        for i, tile in enumerate(sess_tiles):
            sess_grid.addWidget(tile, 0, i)

        self._content_layout.addLayout(sess_grid)
        self._content_layout.addStretch()

        # Refresh timer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)
        self.refresh()

    def refresh(self):
        snap = analytics.snapshot()

        self.t_overall.set_value(snap.get("avg_overall_ms", 0.0))
        self.t_stt.set_value(snap.get("avg_stt_ms", 0.0))
        self.t_model.set_value(snap.get("avg_model_ms", 0.0))
        self.t_tts.set_value(snap.get("avg_tts_ms", 0.0))
        self.t_intent.set_value(snap.get("avg_intent_ms", 0.0))
        self.t_wake_word.set_value(snap.get("avg_wake_word_ms", 0.0))

        self.t_interruptions.set_value(snap.get("interruptions", 0))
        self.t_cancelled.set_value(snap.get("cancelled_tasks", 0))
        self.t_false_wake.set_value(snap.get("false_wake_words", 0))
        acc = snap.get("recognition_accuracy", 0.0)
        self.t_accuracy.set_value(f"{acc:.1f}%")

        self.t_runtime.set_value(snap["runtime"])
        self.t_commands.set_value(snap["commands"])
        self.t_llm_errs.set_value(snap["llm_errors"])
        self.t_crashes.set_value(snap["module_crashes"])
        self.t_events.set_value(snap["events_total"])

"""
ui/components/attention.py

What needs you, in one place, each with the button that deals with it:

    SAINT is waiting for your answer: "...Go ahead?"         [Yes] [No]
    Speech output didn't start — CUDA unavailable              [Retry] [Use CPU]
    Gian's phone wants to send you a file                      [Allow] [Deny]
    Spotify isn't connected                                    [Connect]

Quiet when there's nothing: "Nothing needs you."
"""

from typing import Dict, List

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from core.config import config
from core.events import EventType
from ui import actions
from ui.components.health_panel import gpu_failure
from ui.components.panel import Panel
from ui.reactive import ui_bus
from ui.widgets import clear_layout, run_async


class AttentionPanel(Panel):
    def __init__(self, shell=None, parent=None):
        super().__init__("Needs attention", parent=parent)
        self.shell = shell
        self.rows = QVBoxLayout()
        self.rows.setSpacing(8)
        self.body.addLayout(self.rows)
        self._sig = None
        ui_bus.event.connect(self._on_event)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(4000)
        self.refresh()

    def _on_event(self, ev):
        if ev.type in (EventType.AGENT_TASK, EventType.STARTUP_STATUS, EventType.LINK, EventType.SPOTIFY_CONNECTED,
                       EventType.SPOTIFY_DISCONNECTED, EventType.AGENT_CONFIRM_REQUIRED,
                       EventType.AGENT_CONFIRM_RESOLVED):
            self.refresh()

    def items(self) -> List[Dict]:
        out: List[Dict] = []
        task = actions.current_task()
        if task and task.get("status") == "waiting_for_user":
            out.append({"key": f"task:{task['id']}", "text": f"Waiting for your answer: {task.get('reason', '')}",
                        "buttons": [("Yes", "Primary", lambda: actions.answer("yes")),
                                    ("No", "", lambda: actions.answer("no"))]})
        elif task and task.get("status") == "failed" and task.get("result"):
            out.append({"key": f"failed:{task['id']}", "text": task["result"],
                        "buttons": [("Try again", "", lambda: actions.task_control("resume"))]})
        try:
            from core.startup import startup
            for s in startup.failed():
                buttons = []
                if s.get("retryable"):
                    buttons.append(("Retry", "", lambda k=s["key"]: run_async(lambda: startup.retry(k))))
                if s["key"] == "tts" and gpu_failure(s.get("detail", "")):
                    buttons.append(("Use CPU", "", self._tts_cpu))
                out.append({"key": f"startup:{s['key']}:{s.get('detail', '')}",
                            "text": f"{s['label']} didn't start — {s.get('detail', '')}", "buttons": buttons})
        except Exception:
            pass
        try:
            if config.get("link.enabled", False):
                from modules.link.service import get_link
                link = get_link()
                for a in link.approvals.pending():
                    out.append({"key": f"approval:{a['id']}", "text": f"{a['peer']} wants to {a['description']}",
                                "buttons": [("Allow", "Primary", lambda i=a["id"]: link.approvals.resolve(i, True)),
                                            ("Deny", "Danger", lambda i=a["id"]: link.approvals.resolve(i, False))]})
        except Exception:
            pass
        return out

    def refresh(self):
        items = self.items()
        sig = tuple(i["key"] + i["text"] for i in items)
        if sig == self._sig:
            return
        self._sig = sig
        clear_layout(self.rows)
        self.set_active(bool(items))
        self.set_hint(f"{len(items)}" if items else "")
        if not items:
            ok = QLabel("Nothing needs you.")
            ok.setObjectName("Faint")
            self.rows.addWidget(ok)
            self.relayout()
            return
        for it in items[:6]:
            holder = QWidget()
            row = QVBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            text = QLabel(it["text"])
            text.setWordWrap(True)
            row.addWidget(text)
            if it["buttons"]:
                bl = QHBoxLayout()
                bl.setSpacing(6)
                for label, name, fn in it["buttons"]:
                    b = QPushButton(label)
                    if name:
                        b.setObjectName(name)
                    b.clicked.connect(lambda _=False, f=fn: (f(), QTimer.singleShot(600, self._force)))
                    bl.addWidget(b)
                bl.addStretch()
                row.addLayout(bl)
            self.rows.addWidget(holder)
        self.relayout()

    def _force(self):
        self._sig = None
        self.refresh()

    def _tts_cpu(self):
        def go():
            from core.startup import startup
            config.set("voice.tts_device", "cpu")
            return startup.retry("tts")
        run_async(go)

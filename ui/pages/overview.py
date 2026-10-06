"""
ui/pages/overview.py

The main screen: what SAINT is doing, what needs you, and the state of everything around it.

    ┌ Current task ──────────────┐ ┌ Needs attention ┐
    │ Setting up coding workspace│ │ Now playing     │
    │ ✓ … ● … ○ …   [Pause][Stop]│ │ Devices         │
    ├ Conversation ──────────────┤ │ System health   │
    ├ Recent activity ───────────┤ │ Up next         │
    └────────────────────────────┘ └─────────────────┘

Two columns when there's room, one when there isn't. Which panels show and in what order is
yours (Settings › Appearance › Layout: ``layout.overview_hidden`` / ``layout.overview_order``).
"""

from datetime import datetime
from typing import Dict, List

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from core.config import config
from core.events import EventType
from ui import actions, icons
from ui.components.activity_item import ToolRow, TrailView, drop_demo_rows
from ui.components.attention import AttentionPanel
from ui.components.device_card import DevicesPanel
from ui.components.health_panel import HealthPanel
from ui.components.panel import Panel
from ui.components.task_card import TaskCard
from ui.design import tokens
from ui.reactive import ui_bus
from ui.theme import current_palette
from ui.widgets import ChatView, ElidedLabel, Page, clear_layout, run_async

# key, title, column ("main" / "side")
PANELS = [("task", "Current task", "main"), ("attention", "Needs attention", "side"),
          ("conversation", "Conversation", "main"), ("media", "Now playing", "side"),
          ("activity", "Recent activity", "main"), ("devices", "Devices", "side"),
          ("health", "System health", "side"), ("next", "Up next", "side")]
TWO_COLUMNS_FROM = 980


def panel_order() -> List[str]:
    order = [k for k in (config.get("layout.overview_order", []) or []) if any(k == p[0] for p in PANELS)]
    keys = order + [p[0] for p in PANELS if p[0] not in order]
    hidden = set(config.get("layout.overview_hidden", []) or [])
    return [k for k in keys if k not in hidden]


class ActivityPanel(Panel):
    """The current (or last) task's trail, newest first; the live tool feed when there's no task."""

    def __init__(self, parent=None):
        super().__init__("Recent activity", parent=parent)
        self.trail = TrailView(newest_first=True, limit=10)
        self.body.addWidget(self.trail)
        self.live = QVBoxLayout()
        self.live.setSpacing(0)
        self.body.addLayout(self.live)
        self._rows: List[ToolRow] = []
        self._task_id = None
        ui_bus.event.connect(self._on_event)
        ui_bus.demo_changed.connect(lambda on: None if on else drop_demo_rows(self._rows, QLabel()))
        self.refresh()

    def refresh(self):
        t = actions.current_task()
        if t and t.get("trail"):
            self._task_id = t["id"]
            self.set_hint(t.get("goal", "")[:48])
            self.trail.set_entries(t["trail"])
            self.trail.show()
            self.relayout()
        else:
            self.trail.set_entries([], "Tools SAINT uses and the steps of its tasks appear here as they happen.")
            self.trail.setVisible(not self._rows)

    def _on_event(self, ev):
        t, p = ev.type, ev.payload or {}
        if t == EventType.AGENT_TASK:
            self._task_id = p.get("id")
            self.set_hint(p.get("goal", "")[:48])
            self.trail.set_entries(p.get("trail") or [])
            self.trail.show()
            self.relayout()
        elif t == EventType.TOOL_STARTED:
            row = ToolRow(p.get("tool", ""))
            self._rows.insert(0, row)
            self.live.insertWidget(0, row)
            while len(self._rows) > 4:
                self._rows.pop().deleteLater()
        elif t in (EventType.TOOL_COMPLETED, EventType.TOOL_FAILED):
            row = next((r for r in self._rows if r.tool == p.get("tool") and r.running), None)
            if row is not None:
                row.done(t == EventType.TOOL_COMPLETED, p.get("duration_ms", 0), p.get("error", ""))


class ConversationPanel(Panel):
    def __init__(self, parent=None):
        super().__init__("Conversation", parent=parent)
        clear = QPushButton("Clear")
        clear.setObjectName("Ghost")
        self.add_action(clear)
        self.chat = ChatView(max_messages=40)
        self.chat.setMinimumHeight(150)
        self.chat.setMaximumHeight(260)
        clear.clicked.connect(lambda: self.chat.clear_chat())
        self.body.addWidget(self.chat)
        actions.ChatBinder(self.chat, self)
        if self.chat.is_empty():
            self.chat.add("system", "Say “Hey SAINT” — or type in the box at the bottom.")


class MediaPanel(Panel):
    def __init__(self, parent=None):
        super().__init__("Now playing", parent=parent)
        from ui.pages.music import NowPlaying
        self.player = NowPlaying(cover=64, show_hint=False, any_media=True, volume=True)
        self.body.addWidget(self.player)


class NextPanel(Panel):
    def __init__(self, parent=None):
        super().__init__("Up next", parent=parent)
        self.next_list = QVBoxLayout()
        self.next_list.setSpacing(6)
        self.body.addLayout(self.next_list)
        self.scene_row = QHBoxLayout()
        self.scene_row.setSpacing(6)
        self.body.addLayout(self.scene_row)
        ui_bus.event.connect(self._on_event)
        QTimer.singleShot(400, self.refresh)

    def _on_event(self, ev):
        if ev.type in (EventType.AUTOMATION_CREATED, EventType.AUTOMATION_UPDATED, EventType.AUTOMATION_CANCELLED,
                       EventType.AUTOMATION_TRIGGERED):
            self.refresh()

    def refresh(self):
        from modules.automation.scenes import scenes
        from modules.automation.scheduler import scheduler

        def load():
            return [a for a in scheduler.list(active_only=True) if a.status == "active"][:4], scenes.all()[:3]

        def show(data):
            autos, scs = data
            clear_layout(self.next_list)
            if not autos:
                lab = QLabel("Nothing scheduled. Try “remind me at 5 to stretch”.")
                lab.setObjectName("Faint")
                lab.setWordWrap(True)
                self.next_list.addWidget(lab)
            for a in autos:
                row = QHBoxLayout()
                when = QLabel(datetime.fromtimestamp(a.next_run).strftime("%a %H:%M") if a.next_run else "—")
                when.setObjectName("Faint")
                when.setFixedWidth(70)
                row.addWidget(when)
                row.addWidget(ElidedLabel(a.title + ("  ↻" if a.recurring else "")), 1)
                self.next_list.addLayout(row)
            clear_layout(self.scene_row)
            for s in scs:
                b = QPushButton(s.name)
                b.setObjectName("SceneButton")
                b.setIcon(icons.icon("zap", current_palette().accent, 14))
                b.setToolTip("Run: " + " → ".join(s.steps))
                b.clicked.connect(lambda _=False, sc=s: actions.run_scene(sc))
                self.scene_row.addWidget(b)
            self.scene_row.addStretch()
        run_async(load, show, lambda _e: None)


class OverviewPage(Page):
    def __init__(self, shell):
        super().__init__("Overview", "", scroll=True)
        self.shell = shell
        customize = QPushButton("Customize")
        customize.setObjectName("Ghost")
        customize.setToolTip("Choose which panels show here, and their order")
        customize.clicked.connect(lambda: shell.open_settings("Appearance"))
        self.actions.addWidget(customize)
        self.panels: Dict[str, QWidget] = {
            "task": TaskCard(), "attention": AttentionPanel(shell), "conversation": ConversationPanel(),
            "media": MediaPanel(), "activity": ActivityPanel(), "devices": DevicesPanel(shell),
            "health": HealthPanel(shell, compact=True), "next": NextPanel(),
        }
        for panel in self.panels.values():                 # owned by the page even while hidden
            panel.setParent(self.scroll.widget())
            panel.hide()
        self.cols = QHBoxLayout()
        self.cols.setSpacing(tokens.SECTION_GAP)
        self.main = QVBoxLayout()
        self.main.setSpacing(tokens.SECTION_GAP)
        self.side = QVBoxLayout()
        self.side.setSpacing(tokens.SECTION_GAP)
        side_w = QWidget()
        side_w.setLayout(self.side)
        side_w.setMinimumWidth(300)
        side_w.setMaximumWidth(420)
        self.side_w = side_w
        self.cols.addLayout(self.main, 3)
        self.cols.addWidget(side_w, 2)
        self.root.addLayout(self.cols)
        self.root.addStretch()
        self._two = None
        self._order = None
        self.arrange()
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(30000)
        self._tick()

    def _tick(self):
        now = datetime.now()
        part = "morning" if now.hour < 12 else "afternoon" if now.hour < 18 else "evening"
        self.title.setText(f"Good {part}")
        self.subtitle.setText(now.strftime("%A, %B %d").replace(" 0", " "))
        self.subtitle.show()

    def arrange(self, force: bool = False):
        """Place the visible panels: two columns when wide, one when narrow."""
        width = self.scroll.viewport().width() if self.scroll is not None else self.width()
        two = width >= TWO_COLUMNS_FROM
        order = panel_order()
        if not force and two == self._two and order == self._order:
            return
        self._two, self._order = two, order
        for lay in (self.main, self.side):
            # removeWidget() deletes the layout item. takeAt() left it alive, and Qt kept routing the
            # panel's size changes to that orphan item, so the new one never re-measured (rebuilt rows
            # were squeezed to a few pixels). Panels stay parented: never shown as loose windows.
            for panel in self.panels.values():
                lay.removeWidget(panel)
            while lay.count():
                item = lay.takeAt(0)
                if item is not None and item.spacerItem() is not None:
                    import shiboken6
                    shiboken6.delete(item)
        column = {k: c for k, _t, c in PANELS}
        for key in order:
            target = self.side if (two and column.get(key) == "side") else self.main
            target.addWidget(self.panels[key])
        self.main.addStretch()
        self.side.addStretch()
        self.side_w.setVisible(two)
        for key, panel in self.panels.items():
            panel.setVisible(key in order)
        # A panel that moved column has a new width: re-measure once it's laid out there.
        QTimer.singleShot(0, self._remeasure)

    def _remeasure(self):
        for key in self._order or []:
            panel = self.panels.get(key)
            if panel is not None and panel.isVisible() and hasattr(panel, "relayout"):
                panel.relayout()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.arrange()

    def showEvent(self, e):
        super().showEvent(e)
        self.arrange(force=False)

    def apply_theme(self):
        self.arrange(force=True)
        self.panels["task"].render()

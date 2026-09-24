"""
ui/pages/history.py

Your past usage of SAINT: totals, a 30-day chart, most-used tools, and a
searchable timeline. Read from data/history.jsonl, which never leaves this PC.
"""

import json
import time
from collections import Counter
from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from core.events import EventType
from ui import icons
from ui.pages.home import tool_label
from ui.reactive import ui_bus
from ui.theme import current_palette
from ui.widgets import Bars, Card, ElidedLabel, IconButton, Page, Segmented, chip, clear_layout, run_async

SOURCES = {"voice": ("mic", "Voice"), "typed": ("keyboard", "Typed"), "hotword": ("radio", "Hot-word"),
           "scene": ("zap", "Scene"), "automation": ("clock", "Scheduled")}
FILTERS = [None, {"voice"}, {"typed"}, {"hotword"}, {"scene", "automation"}]
PAGE_SIZE = 120


class Stat(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setObjectName("StatCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(2)
        t = QLabel(title.upper())
        t.setObjectName("CardTitle")
        self.value = QLabel("0")
        self.value.setStyleSheet("font-size: 26px; font-weight: 600;")
        self.sub = QLabel("")
        self.sub.setObjectName("Faint")
        for w in (t, self.value, self.sub):
            lay.addWidget(w)

    def set(self, value, sub=""):
        self.value.setText(str(value))
        self.sub.setText(sub)


class EntryRow(QFrame):
    def __init__(self, rec):
        super().__init__()
        self.setObjectName("Row")
        p = current_palette()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 9, 6, 9)
        lay.setSpacing(12)
        when = QLabel(datetime.fromtimestamp(rec.get("ts", 0)).strftime("%H:%M"))
        when.setObjectName("Faint")
        when.setFixedWidth(40)
        lay.addWidget(when, 0, Qt.AlignTop)
        icon_name, src_label = SOURCES.get(rec.get("source"), ("sparkles", rec.get("source", "")))
        ic = QLabel()
        ic.setPixmap(icons.pixmap(icon_name, p.muted, 15))
        ic.setToolTip(src_label)
        lay.addWidget(ic, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        user = ElidedLabel(rec.get("user", "") or "—")
        user.setStyleSheet("font-weight: 600;")
        col.addWidget(user)
        if rec.get("reply"):
            reply = ElidedLabel(rec["reply"])
            reply.setObjectName("Muted")
            col.addWidget(reply)
        lay.addLayout(col, 1)
        for t in (rec.get("tools") or [])[:3]:
            lay.addWidget(chip(tool_label(t.get("tool", "")), "" if t.get("ok") else "err"), 0, Qt.AlignTop)
        if rec.get("ms"):
            ms = QLabel(f"{rec['ms'] / 1000:.1f}s")
            ms.setObjectName("Faint")
            lay.addWidget(ms, 0, Qt.AlignTop)


class HistoryPage(Page):
    def __init__(self):
        super().__init__("History", "Everything you've asked SAINT — stored only on this PC, never uploaded.")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search history…")
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(lambda: self._debounce.start(180))
        self.export_btn = IconButton("download", "Export to a file", 18)
        self.export_btn.clicked.connect(self._export)
        clear = QPushButton("Clear")
        clear.setObjectName("Danger")
        clear.clicked.connect(self._clear)
        for w in (self.search, self.export_btn, clear):
            self.actions.addWidget(w)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        body = QVBoxLayout(inner)
        body.setContentsMargins(0, 0, 8, 0)
        body.setSpacing(16)
        scroll.setWidget(inner)
        self.root.addWidget(scroll, 1)

        stats = QGridLayout()
        stats.setSpacing(12)
        self.s_total, self.s_today, self.s_week, self.s_free = (Stat("Requests"), Stat("Today"), Stat("Last 7 days"),
                                                                Stat("Hands-free"))
        for i, s in enumerate((self.s_total, self.s_today, self.s_week, self.s_free)):
            stats.addWidget(s, 0, i)
        body.addLayout(stats)

        charts = QHBoxLayout()
        charts.setSpacing(16)
        chart = Card("Last 30 days")
        self.bars = Bars(96)
        chart.body.addWidget(self.bars)
        self.chart_note = QLabel("")
        self.chart_note.setObjectName("Faint")
        chart.body.addWidget(self.chart_note)
        charts.addWidget(chart, 3)
        top = Card("Most used")
        self.top_list = QVBoxLayout()
        self.top_list.setSpacing(8)
        top.body.addLayout(self.top_list)
        top.body.addStretch()
        charts.addWidget(top, 2)
        body.addLayout(charts)

        frow = QHBoxLayout()
        self.filter = Segmented(["All", "Voice", "Typed", "Hot-words", "Scenes & schedules"])
        self.filter.changed.connect(lambda _i: self._render_list(reset=True))
        frow.addWidget(self.filter)
        frow.addStretch()
        self.local = chip("Local only · data/history.jsonl")
        frow.addWidget(self.local)
        body.addLayout(frow)

        self.list_card = Card()
        self.list_layout = QVBoxLayout()
        self.list_layout.setSpacing(0)
        self.list_card.body.addLayout(self.list_layout)
        self.more = QPushButton("Show more")
        self.more.setObjectName("Ghost")
        self.more.clicked.connect(lambda: self._render_list(reset=False))
        self.list_card.body.addWidget(self.more, 0, Qt.AlignHCenter)
        body.addWidget(self.list_card)
        body.addStretch()

        self._rows, self._shown, self._dirty, self._last_day = [], 0, True, None
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(lambda: self._render_list(reset=True))
        ui_bus.event.connect(self._on_event)

    # ------------------------------------------------------------------ #
    def _on_event(self, ev):
        if ev.type == EventType.HISTORY_APPENDED:
            self._dirty = True
            if self.isVisible():
                QTimer.singleShot(250, self.reload)

    def showEvent(self, e):
        super().showEvent(e)
        if self._dirty:
            self.reload()

    def reload(self):
        from core.history import history
        self._dirty = False
        run_async(history.read, self.set_rows)

    def set_rows(self, rows):
        self._rows = sorted(rows, key=lambda r: r.get("ts", 0))
        self._render_stats()
        self._render_list(reset=True)

    # ------------------------------------------------------------------ #
    def _render_stats(self):
        rows, now = self._rows, time.time()
        midnight = datetime.combine(datetime.now().date(), datetime.min.time()).timestamp()
        today = sum(1 for r in rows if r.get("ts", 0) >= midnight)
        week = sum(1 for r in rows if r.get("ts", 0) >= now - 7 * 86400)
        free = sum(1 for r in rows if r.get("source") in ("voice", "hotword"))
        first = datetime.fromtimestamp(rows[0]["ts"]).strftime("since %b %d") if rows else ""
        self.s_total.set(f"{len(rows):,}", first)
        self.s_today.set(today, datetime.now().strftime("%A"))
        self.s_week.set(week, f"{week / 7:.1f} a day")
        self.s_free.set(f"{round(100 * free / len(rows)) if rows else 0}%", "by voice or hot-word")

        days = [(datetime.now().date() - timedelta(days=29 - i)) for i in range(30)]
        counts = Counter(datetime.fromtimestamp(r.get("ts", 0)).date() for r in rows)
        self.bars.set_data([counts.get(d, 0) for d in days], [d.strftime("%a %b %d") for d in days], highlight=29)
        hours = Counter(datetime.fromtimestamp(r.get("ts", 0)).hour for r in rows)
        if hours:
            h = hours.most_common(1)[0][0]
            self.chart_note.setText(f"Most active around {datetime(2000, 1, 1, h).strftime('%I %p').lstrip('0')}")
        else:
            self.chart_note.setText("No activity yet.")

        clear_layout(self.top_list)
        tools = Counter(t.get("tool", "") for r in rows for t in (r.get("tools") or []))
        top = tools.most_common(5)
        if not top:
            lab = QLabel("Tools SAINT uses for you will rank here.")
            lab.setObjectName("Faint")
            lab.setWordWrap(True)
            self.top_list.addWidget(lab)
        for name, n in top:
            row = QVBoxLayout()
            row.setSpacing(3)
            head = QHBoxLayout()
            head.addWidget(ElidedLabel(tool_label(name)), 1)
            cnt = QLabel(str(n))
            cnt.setObjectName("Faint")
            head.addWidget(cnt)
            bar = QProgressBar()
            bar.setRange(0, top[0][1])
            bar.setValue(n)
            bar.setFixedHeight(4)
            bar.setTextVisible(False)
            row.addLayout(head)
            row.addWidget(bar)
            self.top_list.addLayout(row)

    def _filtered(self):
        allowed = FILTERS[max(0, self.filter.index())]
        q = self.search.text().strip().lower()
        out = []
        for r in reversed(self._rows):
            if allowed and r.get("source") not in allowed:
                continue
            if q and q not in " ".join([r.get("user", ""), r.get("reply", "")] +
                                       [t.get("tool", "") for t in r.get("tools") or []]).lower():
                continue
            out.append(r)
        return out

    def _render_list(self, reset: bool):
        rows = self._filtered()
        if reset:
            clear_layout(self.list_layout)
            self._shown, self._last_day = 0, None
        if not rows and reset:
            empty = QLabel("Nothing here yet. Say “Hey SAINT” or type on Home — requests show up here.\n"
                           "History is stored only on this PC. Turn it off in Settings › Memory.")
            empty.setObjectName("Muted")
            empty.setAlignment(Qt.AlignCenter)
            empty.setMinimumHeight(120)
            self.list_layout.addWidget(empty)
        today = datetime.now().date()
        for r in rows[self._shown:self._shown + PAGE_SIZE]:
            d = datetime.fromtimestamp(r.get("ts", 0)).date()
            if d != self._last_day:
                self._last_day = d
                label = "Today" if d == today else "Yesterday" if d == today - timedelta(days=1) else \
                    d.strftime("%A, %B %d")
                head = QLabel(label.upper())
                head.setObjectName("CardTitle")
                head.setContentsMargins(6, 14, 0, 4)
                self.list_layout.addWidget(head)
            self.list_layout.addWidget(EntryRow(r))
        self._shown = min(len(rows), self._shown + PAGE_SIZE)
        self.more.setVisible(self._shown < len(rows))

    # ------------------------------------------------------------------ #
    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export history", "saint-history.json", "JSON (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._rows, f, indent=2, ensure_ascii=False)

    def _clear(self):
        if QMessageBox.question(self, "Clear history", "Delete your whole SAINT history from this PC?") \
                == QMessageBox.Yes:
            from core.history import history
            history.clear()
            self.set_rows([])

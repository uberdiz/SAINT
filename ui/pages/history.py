"""
ui/pages/history.py

Your past usage of SAINT: a year of activity as day squares (like GitHub /
the Claude app), streaks, success rate and speed, when and how you ask, the
tools used most, and a searchable timeline. Read from data/history.jsonl and
its per-day summary, which never leave this PC.
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
from ui.components.activity_item import tool_label
from ui.reactive import ui_bus
from ui.theme import current_palette
from ui.widgets import (Bars, Card, ElidedLabel, Heatmap, IconButton, Page, Segmented, StackBar, chip, clear_layout,
                        run_async)

SOURCES = {"voice": ("mic", "Voice"), "typed": ("keyboard", "Typed"), "hotword": ("radio", "Hot-word"),
           "scene": ("zap", "Scene"), "automation": ("clock", "Scheduled"), "iphone": ("smartphone", "iPhone"),
           "remote": ("cpu", "Other PC")}
FILTERS = [None, {"voice"}, {"typed"}, {"hotword"}, {"scene", "automation"}, {"iphone", "remote"}]
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
        self.s_total, self.s_today, self.s_streak, self.s_success, self.s_free = (
            Stat("Requests"), Stat("Today"), Stat("Streak"), Stat("Success"), Stat("Hands-free"))
        for i, s in enumerate((self.s_total, self.s_today, self.s_streak, self.s_success, self.s_free)):
            stats.addWidget(s, 0, i)
        body.addLayout(stats)

        # A year of activity, one square per day
        heat = Card("Activity")
        self.heatmap = Heatmap()
        heat.body.addWidget(self.heatmap)
        self.heat_summary = ElidedLabel("")
        self.heat_summary.setObjectName("Faint")
        heat.body.addWidget(self.heat_summary)
        body.addWidget(heat)

        charts = QHBoxLayout()
        charts.setSpacing(16)
        chart = Card("Last 30 days")
        self.bars = Bars(96)
        chart.body.addWidget(self.bars)
        self.chart_note = QLabel("")
        self.chart_note.setObjectName("Faint")
        chart.body.addWidget(self.chart_note)
        charts.addWidget(chart, 3)
        hours = Card("Time of day")
        self.hour_bars = Bars(96)
        hours.body.addWidget(self.hour_bars)
        hour_axis = QHBoxLayout()
        for label in ("12 AM", "6 AM", "12 PM", "6 PM", "11 PM"):
            lab = QLabel(label)
            lab.setObjectName("Faint")
            hour_axis.addWidget(lab)
            if label != "11 PM":
                hour_axis.addStretch()
        hours.body.addLayout(hour_axis)
        charts.addWidget(hours, 2)
        body.addLayout(charts)

        lower = QHBoxLayout()
        lower.setSpacing(16)
        top = Card("Most used")
        self.top_list = QVBoxLayout()
        self.top_list.setSpacing(8)
        top.body.addLayout(self.top_list)
        top.body.addStretch()
        lower.addWidget(top, 3)
        how = Card("How you ask")
        self.source_bar = StackBar(10)
        how.body.addWidget(self.source_bar)
        self.source_list = QVBoxLayout()
        self.source_list.setSpacing(6)
        how.body.addLayout(self.source_list)
        self.week_note = QLabel("")
        self.week_note.setObjectName("Faint")
        self.week_note.setWordWrap(True)
        how.body.addWidget(self.week_note)
        how.body.addStretch()
        lower.addWidget(how, 2)
        body.addLayout(lower)

        frow = QHBoxLayout()
        self.filter = Segmented(["All", "Voice", "Typed", "Hot-words", "Scenes & schedules", "Other devices"])
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
        self._daily = {}
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
        run_async(lambda: (history.read(), history.daily()), lambda res: self.set_rows(*res))

    def set_rows(self, rows, daily=None):
        from core.history import add_to_day
        self._rows = sorted(rows, key=lambda r: r.get("ts", 0))
        if daily is None:                      # e.g. tests / export: build it from the rows
            daily = {}
            for r in self._rows:
                add_to_day(daily, r)
        self._daily = daily
        self._render_stats()
        self._render_list(reset=True)

    # ------------------------------------------------------------------ #
    def _render_stats(self):
        from core import history_stats as hs
        rows, daily = self._rows, self._daily
        today = datetime.now().date()
        tot = hs.totals(daily)
        n_today = (daily.get(today.isoformat()) or {}).get("n", 0)
        week = sum((daily.get((today - timedelta(days=i)).isoformat()) or {}).get("n", 0) for i in range(7))
        first = min(daily) if daily else ""
        since = datetime.strptime(first, "%Y-%m-%d").strftime("since %b %d, %Y") if first else ""
        self.s_total.set(f"{tot['n']:,}", since)
        self.s_today.set(n_today, f"{week} this week \u00b7 {week / 7:.1f} a day")
        cur, longest = hs.streaks(daily)
        self.s_streak.set(f"{cur} day{'s' if cur != 1 else ''}", f"longest {longest} day{'s' if longest != 1 else ''}")
        lat = hs.percentile([r.get("ms") for r in rows if r.get("source") in ("voice", "typed", "hotword")], 0.5)
        if tot["success"] is None:
            self.s_success.set("\u2014", "no actions yet")
        else:
            self.s_success.set(f"{round(100 * tot['success'])}%",
                               f"typical reply {lat / 1000:.1f}s" if lat else "of actions worked")
        free = tot["by_source"]["voice"] + tot["by_source"]["hotword"]
        self.s_free.set(f"{round(100 * free / tot['n']) if tot['n'] else 0}%", "by voice or hot-word")

        # heatmap: the last year
        days = hs.calendar(daily, weeks=53, today=today)
        self.heatmap.set_data(days, hs.level_thresholds([c for _, c in days]), today)
        active = sum(1 for _, c in days if c)
        year = sum(c for _, c in days)
        best = hs.busiest_day(daily)
        best_txt = f" \u00b7 busiest {best[0].strftime('%b')} {best[0].day} ({best[1]})" if best else ""
        self.heat_summary.setText(f"{year:,} requests on {active} days in the last year{best_txt}")

        days30 = [(today - timedelta(days=29 - i)) for i in range(30)]
        self.bars.set_data([(daily.get(d.isoformat()) or {}).get("n", 0) for d in days30],
                           [d.strftime("%a %b %d") for d in days30], highlight=29)
        hours = tot["hours"]
        self.hour_bars.set_data(hours, [datetime(2000, 1, 1, h).strftime("%I %p").lstrip("0") for h in range(24)],
                                highlight=max(range(24), key=lambda h: hours[h]) if any(hours) else -1)
        if any(hours):
            h = max(range(24), key=lambda i: hours[i])
            wd = tot["weekdays"]
            busiest_wd = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[
                max(range(7), key=lambda i: wd[i])]
            self.chart_note.setText(f"Most active around {datetime(2000, 1, 1, h).strftime('%I %p').lstrip('0')}"
                                    f" \u00b7 busiest on {busiest_wd}s")
        else:
            self.chart_note.setText("No activity yet.")

        # how you ask
        p = current_palette()
        from ui.theme import _mix
        colors = {"voice": p.accent, "hotword": _mix(p.info, p.danger, 0.55), "typed": p.info,
                  "scene": p.success, "automation": p.muted, "iphone": _mix(p.accent, p.success, 0.5),
                  "remote": _mix(p.accent, p.muted, 0.5)}
        names = {"voice": "Voice", "hotword": "Hot-words", "typed": "Typed", "scene": "Scenes",
                 "automation": "Scheduled", "iphone": "iPhone", "remote": "Other PC"}
        parts = [(names[s], tot["by_source"][s], colors[s]) for s in names]
        self.source_bar.set_data(parts)
        clear_layout(self.source_list)
        total = sum(v for _, v, _ in parts) or 1
        for label, v, color in parts:
            if not v:
                continue
            row = QHBoxLayout()
            dot = QLabel()
            dot.setFixedSize(8, 8)
            dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
            row.addWidget(dot)
            row.addWidget(QLabel(label), 1)
            pct = QLabel(f"{round(100 * v / total)}%  \u00b7  {v:,}")
            pct.setObjectName("Faint")
            row.addWidget(pct)
            self.source_list.addLayout(row)
        fails = tot["fail"]
        self.week_note.setText(f"{fails:,} request{'s' if fails != 1 else ''} hit a problem \u2014 search the "
                               f"timeline below to see which." if fails else "")

        clear_layout(self.top_list)
        # Counted by what the tool does ("Working with Spotify"), so related tools share a row.
        tools = Counter(tool_label(t.get("tool", "")) for r in rows for t in (r.get("tools") or []))
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
            head.addWidget(ElidedLabel(name), 1)
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

"""
ui/pages/storage.py

The Storage page:
* every drive's used / free space, a scan of what's using it (background,
  cached) and the biggest folders with an Open button;
* "What you can clear": a junk check across your drives shown as a chart by
  category (caches, temp files, duplicates, old installers, Windows Update
  leftovers...) and a list you tick. Only ticked items move, only after you
  confirm, and only to the Recycle Bin.
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from core.events import EventType
from ui.reactive import ui_bus
from ui.theme import _mix, current_palette
from ui.widgets import Card, ElidedLabel, Page, Segmented, StackBar, clear_layout, run_async


def _human(n):
    from modules.files.scan import human
    return human(n)


class StorageCard(Card):
    def __init__(self, junk_button: bool = True):
        super().__init__("Drives")
        self._drives = []
        self._task_ids = {}            # task id -> ("scan", drive) | ("junk", "")
        self._selected = 0

        self.junk_btn = QPushButton("Check for junk")
        self.junk_btn.setObjectName("Ghost")
        self.junk_btn.clicked.connect(self._check_junk)
        if junk_button:
            self.header.addWidget(self.junk_btn)

        self.drive_rows = QVBoxLayout()
        self.drive_rows.setSpacing(10)
        self.body.addLayout(self.drive_rows)

        pick = QHBoxLayout()
        self.picker = Segmented(["C:"])
        self.picker.changed.connect(self._pick)
        pick.addWidget(self.picker)
        pick.addStretch()
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(lambda: self._scan(refresh=True))
        pick.addWidget(self.scan_btn)
        self.body.addLayout(pick)

        self.status = QLabel("")
        self.status.setObjectName("Faint")
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)
        self.scan_bar = QProgressBar()
        self.scan_bar.setRange(0, 100)
        self.scan_bar.setTextVisible(False)
        self.scan_bar.setFixedHeight(4)
        self.scan_bar.hide()
        self.body.addWidget(self.scan_bar)
        self.top_rows = QVBoxLayout()
        self.top_rows.setSpacing(6)
        self.body.addLayout(self.top_rows)

        self.junk_note = QLabel("")
        self.junk_note.setWordWrap(True)
        self.junk_note.setObjectName("Muted")
        self.junk_note.hide()
        self.body.addWidget(self.junk_note)
        ui_bus.event.connect(self._on_event)

    # ------------------------------------------------------------------ #
    def refresh(self):
        from modules.files.scan import drive_overview
        run_async(drive_overview, self._set_drives)

    def _set_drives(self, drives):
        p = current_palette()
        self._drives = drives
        clear_layout(self.drive_rows)
        for d in drives:
            row = QHBoxLayout()
            name = QLabel(f"{d['letter']}:" + (f"  {d['label']}" if d.get("label") else ""))
            name.setStyleSheet("font-weight: 600;")
            name.setFixedWidth(150)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(int(d["percent"] * 10))
            bar.setTextVisible(False)
            bar.setFixedHeight(8)
            tone = p.danger if d["percent"] >= 95 else p.warning if d["percent"] >= 85 else p.accent
            bar.setStyleSheet(f"QProgressBar::chunk {{ background: {tone}; border-radius: 4px; }}")
            text = QLabel(f"{_human(d['free'])} free of {_human(d['total'])}")
            text.setObjectName("Faint")
            text.setFixedWidth(170)
            text.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(name)
            row.addWidget(bar, 1)
            row.addWidget(text)
            self.drive_rows.addLayout(row)
        labels = [f"{d['letter']}:" for d in drives] or ["C:"]
        if labels != getattr(self, "_labels", None):
            self._labels = labels
            new = Segmented(labels)
            new.changed.connect(self._pick)
            self._replace_picker(new)
        self._show_cached()

    def _replace_picker(self, new):
        for i in range(self.body.count()):
            item = self.body.itemAt(i)
            lay = item.layout() if item else None
            if lay is not None and lay.indexOf(self.picker) >= 0:
                lay.replaceWidget(self.picker, new)
                self.picker.deleteLater()
                self.picker = new
                self.picker.set_index(min(self._selected, len(self._labels) - 1))
                return

    def _drive_root(self):
        if not self._drives:
            return "C:\\"
        return self._drives[min(self._selected, len(self._drives) - 1)]["drive"]

    def _pick(self, i):
        self._selected = i
        self._show_cached()

    def _show_cached(self):
        from modules.files.scan import cached, top_folders
        root = self._drive_root()
        hit = cached(root)
        if hit:
            self._show_top(root, top_folders(hit, 8), hit.get("at"))
        else:
            clear_layout(self.top_rows)
            self.status.setText(f"Scan {root.rstrip(chr(92))} to see what's using the space.")

    def _show_top(self, root, top, at=None):
        import time
        clear_layout(self.top_rows)
        when = ""
        if at:
            mins = int((time.time() - at) / 60)
            when = " · scanned just now" if mins < 2 else f" · scanned {mins} min ago" if mins < 120 else \
                f" · scanned {mins // 60} h ago"
        self.status.setText(f"Biggest on {root.rstrip(chr(92))}{when}")
        biggest = max([f["size"] for f in top] or [1])
        for f in top:
            row = QHBoxLayout()
            rel = os.path.relpath(f["path"], root)
            label = ElidedLabel(rel)
            label.setToolTip(f["path"])
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(int(1000 * f["size"] / biggest))
            bar.setTextVisible(False)
            bar.setFixedHeight(4)
            bar.setFixedWidth(120)
            size = QLabel(_human(f["size"]))
            size.setObjectName("Faint")
            size.setFixedWidth(64)
            size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            open_btn = QPushButton("Open")
            open_btn.setObjectName("Ghost")
            open_btn.clicked.connect(lambda _=False, p=f["path"]: self._open(p))
            row.addWidget(label, 1)
            row.addWidget(bar)
            row.addWidget(size)
            row.addWidget(open_btn)
            self.top_rows.addLayout(row)

    def _open(self, path):
        from modules.files.ops import open_in_explorer
        run_async(lambda: open_in_explorer(path), None)

    # ------------------------------------------------------------------ #
    def _scan(self, refresh=False):
        from modules.automation.tools import get_tool_registry
        root = self._drive_root()
        letter = root[0]

        def go():
            return get_tool_registry().execute("files.biggest", target=letter, refresh=refresh)

        def done(res):
            if not res.success:
                self.status.setText(res.error or "The scan couldn't start.")
                return
            r = res.result
            if r.get("started"):
                self._task_ids[r["started"]] = ("scan", root)
                self.status.setText(f"Scanning {root.rstrip(chr(92))}…")
                self.scan_bar.setValue(0)
                self.scan_bar.show()
                self.scan_btn.setEnabled(False)
            else:
                self._show_cached()
        run_async(go, done)

    def _check_junk(self):
        from modules.automation.tools import get_tool_registry

        def done(res):
            if not res.success:
                self.junk_note.setText(res.error or "The check couldn't start.")
            elif res.result.get("started"):
                self._task_ids[res.result["started"]] = ("junk", "")
                self.junk_note.setText("Looking for things you can clear…")
                self.junk_btn.setEnabled(False)
            self.junk_note.show()
        run_async(lambda: get_tool_registry().execute("files.junk_report", scope="all"), done)

    def _on_event(self, ev):
        p = ev.payload or {}
        kind = self._task_ids.get(p.get("id"))
        if not kind:
            return
        if ev.type == EventType.TASK_PROGRESS and kind[0] == "scan":
            self.scan_bar.setValue(int(100 * p.get("progress", 0)))
            if p.get("text"):
                self.status.setText(f"Scanning {kind[1].rstrip(chr(92))} — {p['text']}")
        elif ev.type == EventType.TASK_DONE:
            self._task_ids.pop(p.get("id"), None)
            if kind[0] == "scan":
                self.scan_bar.hide()
                self.scan_btn.setEnabled(True)
                r = p.get("result") or {}
                if r.get("top") is not None:
                    import time
                    self._show_top(kind[1], r["top"], time.time())
                else:
                    self.status.setText(p.get("summary", ""))
                self.refresh()
            else:
                self.junk_btn.setEnabled(True)
                text = (p.get("result") or {}).get("summary") or p.get("summary", "")
                if (p.get("result") or {}).get("recyclable"):
                    text += "\nSay \u201cyes\u201d (or type it on Home) within 30 seconds, or later say " \
                            "\u201cclean those up\u201d. Nothing is removed without your OK."
                self.junk_note.setText(text)
                self.junk_note.show()


# ====================================================================== #
# The Storage page: drives + a picture of what can be cleared
# ====================================================================== #
# Colour per kind of finding, and the order they're listed in.
_KIND_TONE = {"recycle": "success", "review": "warning", "cleanmgr": "info", "info": "faint"}
_KIND_LABEL = {"recycle": "Safe to clear", "review": "Your call", "cleanmgr": "Needs Disk Cleanup",
               "info": "Already in the Recycle Bin"}


def _cap(s):
    return s[:1].upper() + s[1:]


class CleanupCard(Card):
    """What SAINT found that could go, as a bar chart by category and a list
    you tick. Nothing is removed until you confirm; everything goes to the
    Recycle Bin (never deleted outright)."""

    def __init__(self):
        super().__init__("What you can clear")
        self._report = None
        self._task_id = None

        self.check_btn = QPushButton("Check for junk")
        self.check_btn.setObjectName("Primary")
        self.check_btn.clicked.connect(self.check)
        self.header.addWidget(self.check_btn)

        self.summary = QLabel("Check your drives for caches, temp files, duplicate downloads and old installers. "
                              "You pick what goes — it all goes to the Recycle Bin.")
        self.summary.setWordWrap(True)
        self.summary.setObjectName("Muted")
        self.body.addWidget(self.summary)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        self.body.addWidget(self.progress)

        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.tiles = {}
        for kind in ("recycle", "review", "cleanmgr"):
            t = QFrame()
            t.setObjectName("StatCard")
            tl = QVBoxLayout(t)
            tl.setContentsMargins(14, 10, 14, 10)
            tl.setSpacing(2)
            cap = QLabel(_KIND_LABEL[kind].upper())
            cap.setObjectName("CardTitle")
            val = QLabel("—")
            val.setStyleSheet("font-size: 18px; font-weight: 600;")
            tl.addWidget(cap)
            tl.addWidget(val)
            self.tiles[kind] = val
            tiles.addWidget(t)
        self.body.addLayout(tiles)

        self.chart = StackBar(height=14)
        self.body.addWidget(self.chart)
        self.legend = QVBoxLayout()
        self.legend.setSpacing(6)
        self.body.addLayout(self.legend)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["What", "Size", "Why"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setMinimumHeight(260)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemDoubleClicked.connect(self._open_item)
        self.tree.itemChanged.connect(lambda *_: self._update_selection())
        self.tree.hide()
        self.body.addWidget(self.tree, 1)

        row = QHBoxLayout()
        self.recycle_btn = QPushButton("Move ticked to Recycle Bin")
        self.recycle_btn.setObjectName("Danger")
        self.recycle_btn.clicked.connect(self._recycle)
        self.recycle_btn.setEnabled(False)
        self.cleanmgr_btn = QPushButton("Open Disk Cleanup")
        self.cleanmgr_btn.setObjectName("Ghost")
        self.cleanmgr_btn.clicked.connect(self._disk_cleanup)
        self.cleanmgr_btn.hide()
        self.bin_btn = QPushButton("Open Recycle Bin")
        self.bin_btn.setObjectName("Ghost")
        self.bin_btn.clicked.connect(lambda: run_async(lambda: os.startfile("shell:RecycleBinFolder"), None))
        self.bin_btn.hide()
        row.addWidget(self.recycle_btn)
        row.addWidget(self.cleanmgr_btn)
        row.addWidget(self.bin_btn)
        row.addStretch()
        self.status = QLabel("")
        self.status.setObjectName("Faint")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.body.addLayout(row)
        ui_bus.event.connect(self._on_event)

    # ------------------------------------------------------------------ #
    def show_latest(self):
        """Show the last check, even one started by voice ("what can I delete?")."""
        from modules.files.tools import last_plan
        rep = last_plan(max_age=6 * 3600)
        if rep and rep is not self._report and rep.get("findings") is not None and \
                (self._report is None or rep.get("at", 0) > self._report.get("at", 0)):
            self.show_report(rep)

    def check(self):
        from modules.automation.tools import get_tool_registry

        def done(res):
            if not res.success:
                self.status.setText(res.error or "The check couldn't start.")
                return
            if res.result.get("started"):
                self._task_id = res.result["started"]
                self.check_btn.setEnabled(False)
                self.progress.setValue(0)
                self.progress.show()
                self.summary.setText("Looking through your drives…")
            else:
                self.show_report(res.result)
        run_async(lambda: get_tool_registry().execute("files.junk_report", scope="all"), done)

    def show_report(self, rep):
        from modules.files.junk import CATEGORY_NAMES
        p = current_palette()
        self._report = rep
        findings = [f for f in rep.get("findings") or []
                    if f.get("category") == "recycle_bin" or os.path.exists(f.get("path", ""))]
        by_kind = {k: sum(f["size"] for f in findings if f["action"] == k) for k in _KIND_LABEL}
        for kind, lbl in self.tiles.items():
            lbl.setText(_human(by_kind.get(kind, 0)) if by_kind.get(kind) else "—")
            lbl.setStyleSheet(f"font-size: 18px; font-weight: 600; color: {getattr(p, _KIND_TONE[kind])};")
        total = sum(f["size"] for f in findings)
        self.summary.setText(f"Found {_human(total)} you could get back." if findings else
                             "Nothing worth clearing right now.")

        cats = {}
        for f in findings:
            c = cats.setdefault(f["category"], {"size": 0, "items": [], "action": f["action"]})
            c["size"] += f["size"]
            c["items"].append(f)
        ordered = sorted(cats.items(), key=lambda kv: -kv[1]["size"])
        palette = [p.accent, p.info, p.success, p.danger, p.warning, _mix(p.accent, p.success, 0.5),
                   _mix(p.warning, p.danger, 0.5), _mix(p.info, p.accent, 0.5), p.muted, p.faint]
        colors = {cat: palette[i % len(palette)] for i, (cat, _) in enumerate(ordered)}
        self.chart.set_data([(CATEGORY_NAMES.get(cat, cat), c["size"], colors[cat]) for cat, c in ordered])

        clear_layout(self.legend)
        biggest = max([c["size"] for _, c in ordered] or [1])
        for cat, c in ordered:
            row = QHBoxLayout()
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {colors[cat]};")
            name = QLabel(_cap(CATEGORY_NAMES.get(cat, cat)) +
                          (f"  ({len(c['items'])})" if len(c["items"]) > 1 else ""))
            name.setFixedWidth(260)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(int(1000 * c["size"] / biggest))
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            bar.setStyleSheet(f"QProgressBar::chunk {{ background: {colors[cat]}; border-radius: 3px; }}")
            size = QLabel(_human(c["size"]))
            size.setFixedWidth(70)
            size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            kind = QLabel(_KIND_LABEL.get(c["action"], ""))
            kind.setObjectName("Faint")
            kind.setFixedWidth(170)
            for w in (dot, name):
                row.addWidget(w)
            row.addWidget(bar, 1)
            row.addWidget(size)
            row.addWidget(kind)
            self.legend.addLayout(row)

        self.tree.blockSignals(True)
        self.tree.clear()
        for cat, c in ordered:
            top = QTreeWidgetItem([_cap(CATEGORY_NAMES.get(cat, cat)) + f" ({len(c['items'])})",
                                   _human(c["size"]), _KIND_LABEL.get(c["action"], "")])
            top.setForeground(0, QColor(colors[cat]))
            tickable = c["action"] in ("recycle", "review")
            if tickable:
                top.setFlags(top.flags() | Qt.ItemIsAutoTristate | Qt.ItemIsUserCheckable)
            items = sorted(c["items"], key=lambda f: -f["size"])
            for f in items[:200]:
                label = f["path"] if f["category"] != "recycle_bin" else "Recycle Bin"
                child = QTreeWidgetItem([label, _human(f["size"]), f.get("reason", "")])
                child.setData(0, Qt.UserRole, f)
                child.setToolTip(0, f["path"])
                if tickable:
                    child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.Checked if f["action"] == "recycle" else Qt.Unchecked)
                top.addChild(child)
            if len(items) > 200:
                top.addChild(QTreeWidgetItem([f"…and {len(items) - 200} smaller ones (not shown)", "", ""]))
            self.tree.addTopLevelItem(top)
        self.tree.resizeColumnToContents(1)
        self.tree.setColumnWidth(0, 420)
        self.tree.blockSignals(False)
        self.tree.setVisible(bool(findings))
        self.cleanmgr_btn.setVisible(any(f["action"] == "cleanmgr" for f in findings))
        self.bin_btn.setVisible(any(f["category"] == "recycle_bin" for f in findings))
        self._update_selection()

    def _ticked(self):
        out = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                ch = top.child(j)
                f = ch.data(0, Qt.UserRole)
                if f and ch.checkState(0) == Qt.Checked:
                    out.append(f)
        return out

    def _update_selection(self):
        items = self._ticked()
        size = sum(f["size"] for f in items)
        self.recycle_btn.setEnabled(bool(items))
        self.recycle_btn.setText(f"Move ticked to Recycle Bin ({_human(size)})" if items else
                                 "Move ticked to Recycle Bin")

    def _recycle(self):
        from PySide6.QtWidgets import QMessageBox
        items = self._ticked()
        if not items:
            return
        size = sum(f["size"] for f in items)
        ok = QMessageBox.question(
            self, "Move to the Recycle Bin?",
            f"Move {len(items)} item{'s' if len(items) != 1 else ''} ({_human(size)}) to the Recycle Bin?\n\n"
            "Nothing is deleted permanently — you can restore them from the Recycle Bin. Caches rebuild "
            "themselves; apps that are open may keep a few files.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ok != QMessageBox.Yes:
            return
        from modules.automation.tools import get_tool_registry
        self.recycle_btn.setEnabled(False)
        self.status.setText("Moving them to the Recycle Bin…")

        def go():
            return get_tool_registry().execute("files.recycle", _confirmed=True, paths=[f["path"] for f in items])

        def done(res):
            self.status.setText((res.result or {}).get("summary", "Done.") if res.success else
                                (res.error or "That didn't work."))
            if self._report:
                self.show_report(self._report)          # gone items drop out
        run_async(go, done)

    def _disk_cleanup(self):
        from modules.automation.tools import get_tool_registry
        drives = sorted({f["path"][:1] for f in (self._report or {}).get("findings", []) if f["action"] == "cleanmgr"})
        run_async(lambda: [get_tool_registry().execute("files.disk_cleanup", drive=d) for d in drives[:2]], None)
        self.status.setText("Opened Disk Cleanup — tick “Windows Update Cleanup” there.")

    def _open_item(self, item, _col):
        f = item.data(0, Qt.UserRole)
        if not f:
            return
        if f["category"] == "recycle_bin":
            run_async(lambda: os.startfile("shell:RecycleBinFolder"), None)
            return
        from modules.files.ops import open_in_explorer
        run_async(lambda: open_in_explorer(f["path"], select=True), None)

    def _on_event(self, ev):
        p = ev.payload or {}
        if ev.type == EventType.TASK_PROGRESS and p.get("id") == self._task_id:
            self.progress.setValue(int(100 * p.get("progress", 0)))
            if p.get("text"):
                self.summary.setText(f"Looking through your drives — {p['text']}…")
        elif ev.type == EventType.TASK_DONE:
            res = p.get("result") if isinstance(p.get("result"), dict) else {}
            if p.get("id") == self._task_id:
                self._task_id = None
                self.progress.hide()
                self.check_btn.setEnabled(True)
            if res.get("findings") is not None:
                self.show_report(res)                   # any junk check, voice ones too


class StoragePage(Page):
    def __init__(self):
        super().__init__("Storage", "Where your space went, and what you can safely get back.")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        body = QVBoxLayout(inner)
        body.setContentsMargins(0, 0, 8, 0)
        body.setSpacing(16)
        scroll.setWidget(inner)
        self.root.addWidget(scroll, 1)
        self.drives = StorageCard(junk_button=False)
        body.addWidget(self.drives)
        self.cleanup = CleanupCard()
        body.addWidget(self.cleanup, 1)

    def showEvent(self, e):
        super().showEvent(e)
        self.drives.refresh()
        self.cleanup.show_latest()

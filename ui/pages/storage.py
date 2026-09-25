"""
ui/pages/storage.py

The Storage card on the System page: every drive's used / free space, a scan
of what's using it (runs in the background, cached), the biggest folders with
an Open button, and a junk check. Nothing is removed from here — cleanup is
always confirmed by you in the conversation ("clean those up").
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from core.events import EventType
from ui.reactive import ui_bus
from ui.theme import current_palette
from ui.widgets import Card, ElidedLabel, Segmented, clear_layout, run_async


def _human(n):
    from modules.files.scan import human
    return human(n)


class StorageCard(Card):
    def __init__(self):
        super().__init__("Storage")
        self._drives = []
        self._task_ids = {}            # task id -> ("scan", drive) | ("junk", "")
        self._selected = 0

        self.junk_btn = QPushButton("Check for junk")
        self.junk_btn.setObjectName("Ghost")
        self.junk_btn.clicked.connect(self._check_junk)
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

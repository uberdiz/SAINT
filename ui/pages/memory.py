"""
ui/pages/memory.py

What SAINT knows about you. The profile at the top is assembled only from
what's stored on this PC — what you told SAINT, what it picked up from
conversation, your listening and usage habits — with a short summary the
local model writes from those facts. Below it, every memory: edit, delete,
or confirm a learned one so it's kept for good.
"""

from datetime import datetime

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLayout,
                               QLineEdit, QMessageBox, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from core.events import EventType
from ui import icons
from ui.reactive import ui_bus
from ui.theme import current_palette
from ui.widgets import Card, Page, Segmented, chip, clear_layout, run_async, with_alpha

FILTERS = ["All", "Told", "Learned", "About me", "Likes", "People", "Projects", "Other"]
_FILTER_CATS = {"About me": {"identity", "personal"}, "Likes": {"preference", "interest", "dislike"},
                "People": {"person"}, "Projects": {"project", "goal", "routine"}, "Other": {"fact"}}
_CAT_LABEL = {"identity": "about you", "personal": "about you", "person": "people", "preference": "favorite",
              "interest": "likes", "dislike": "dislikes", "project": "project", "goal": "goal", "routine": "routine",
              "fact": "fact"}
_ID_LABEL = {"age": "{} years old", "hometown": "from {}", "home location": "lives in {}", "city": "lives in {}",
             "location": "lives in {}", "birthday": "birthday {}", "employer": "works at {}", "school": "{}",
             "studies": "studies {}", "major": "studies {}", "pronouns": "{}"}


class FlowLayout(QLayout):
    """Chips that wrap onto the next line."""

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self._sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._place(QRect(0, 0, w, 0), dry=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _place(self, rect, dry=False):
        x, y, line = rect.x(), rect.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            if x + hint.width() > rect.right() + 1 and line > 0:
                x, y, line = rect.x(), y + line + self._sp, 0
            if not dry:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._sp
            line = max(line, hint.height())
        return y + line - rect.y()


class Avatar(QWidget):
    def __init__(self, size: int = 64):
        super().__init__()
        self.setFixedSize(size, size)
        self._text = ""

    def set_name(self, name: str):
        self._text = (name or "").strip()[:1].upper()
        self.update()

    def paintEvent(self, _):
        p = current_palette()
        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        g.setPen(Qt.NoPen)
        g.setBrush(QColor(p.accent_soft))
        g.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        g.setPen(with_alpha(p.accent, 120))
        g.setBrush(Qt.NoBrush)
        g.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        if self._text:
            f = QFont(self.font())
            f.setPixelSize(int(self.height() * 0.42))
            f.setWeight(QFont.DemiBold)
            g.setFont(f)
            g.setPen(QColor(p.accent))
            g.drawText(self.rect(), Qt.AlignCenter, self._text)
        else:
            s = int(self.height() * 0.46)
            g.drawPixmap((self.width() - s) // 2, (self.height() - s) // 2, icons.pixmap("user", p.accent, s))
        g.end()


class ProfileCard(Card):
    """The user, at a glance."""

    def __init__(self, page):
        super().__init__("Your profile")
        self.page = page
        self.stats = QLabel("")
        self.stats.setObjectName("Faint")
        self.header.addWidget(self.stats)
        self.refresh_btn = QPushButton(" Rewrite summary")
        self.refresh_btn.setToolTip("Have the local model write the summary again from what's stored")
        self.refresh_btn.clicked.connect(lambda: page.refresh_profile(force_summary=True))
        self.header.addWidget(self.refresh_btn)

        top = QHBoxLayout()
        top.setSpacing(18)
        self.avatar = Avatar(68)
        top.addWidget(self.avatar, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(4)
        self.name = QLabel("")
        self.name.setObjectName("Display")
        self.name.setStyleSheet("font-size: 26px; font-weight: 600;")
        self.tagline = QLabel("")
        self.tagline.setObjectName("Muted")
        self.tagline.setWordWrap(True)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.summary.setStyleSheet("font-size: 14px; line-height: 140%;")
        col.addWidget(self.name)
        col.addWidget(self.tagline)
        col.addSpacing(6)
        col.addWidget(self.summary)
        top.addLayout(col, 1)
        self.body.addLayout(top)

        self.sections = QVBoxLayout()
        self.sections.setSpacing(8)
        self.body.addSpacing(6)
        self.body.addLayout(self.sections)

    def apply_theme(self):
        self.refresh_btn.setIcon(icons.icon("refresh", current_palette().text, 14))
        self.avatar.update()

    def render(self, prof: dict, summary: str):
        idt = prof.get("identity", {})
        name = prof.get("name") or ""
        self.avatar.set_name(name)
        self.name.setText(name or "You")
        bits = []
        if idt.get("occupation"):
            bits.append(idt["occupation"][0].upper() + idt["occupation"][1:])
        for k in ("studies", "school", "age", "home location", "city", "location", "hometown", "birthday"):
            if idt.get(k) and not (k in ("city", "location") and idt.get("home location")):
                bits.append(_ID_LABEL.get(k, "{}").format(idt[k]))
        self.tagline.setText("  ·  ".join(bits[:4]) if bits else
                             "Tell SAINT your name, what you do and where you live — “call me Sam”, "
                             "“I'm a nurse”, “I live in Toronto”.")
        self.summary.setText(summary)
        c = prof.get("counts", {})
        self.stats.setText(f"{c.get('total', 0)} memories · {c.get('told', 0)} told · {c.get('learned', 0)} learned")

        clear_layout(self.sections)
        rows = [("Likes", prof.get("likes", []), "heart"), ("Dislikes", prof.get("dislikes", []), "x"),
                ("People & pets", prof.get("people", []), "user"),
                ("Projects & goals", prof.get("projects", []), "zap"), ("Routines", prof.get("routines", []), "clock")]
        for title, items, icon in rows:
            if items:
                self._row(title, icon, [(i["label"], "learned" if i["how"] == "learned" else "told", i["text"])
                                        for i in items[:14]])
        music = prof.get("music") or {}
        if music.get("artists") or music.get("genres"):
            self._row("Music", "music", [(a, "seen", "Top artist, last 30 days") for a in music.get("artists", [])[:6]]
                      + [(g, "seen", "Top genre") for g in music.get("genres", [])[:4]])
        h = prof.get("habits") or {}
        if h:
            items = []
            if h.get("time_of_day"):
                items.append((f"Mostly {h['time_of_day']}", "seen", "When you talk to SAINT most"))
            if h.get("busiest_day"):
                items.append((f"Busiest on {h['busiest_day']}s", "seen", "Your busiest day with SAINT"))
            if h.get("voice_share") is not None:
                items.append((f"{int(round(h['voice_share'] * 100))}% by voice", "seen", "Voice vs typed requests"))
            for dom, share in h.get("top_uses", [])[:3]:
                items.append((f"{dom} {int(round(share * 100))}%", "seen", "What you ask SAINT for"))
            items += [(s, "seen", "A site you use with SAINT") for s in h.get("top_sites", [])[:4]]
            items += [(a, "seen", "An app you open with SAINT") for a in h.get("top_apps", [])[:4]]
            self._row(f"Habits · {h.get('requests', 0)} requests", "activity", items)
        if not any(prof.get(k) for k in ("likes", "dislikes", "people", "projects", "routines")) and not music \
                and not h:
            hint = QLabel("Your profile fills in as you talk — SAINT picks up things like what you do, what you "
                          "like and the people in your life, and you can see and edit all of it below.")
            hint.setObjectName("Faint")
            hint.setWordWrap(True)
            self.sections.addWidget(hint)

    def _row(self, title: str, icon: str, items):
        p = current_palette()
        row = QHBoxLayout()
        row.setSpacing(12)
        head = QHBoxLayout()
        head.setContentsMargins(0, 3, 0, 0)
        head.setSpacing(6)
        ic = QLabel()
        ic.setPixmap(icons.pixmap(icon, p.faint, 14))
        head.addWidget(ic)
        t = QLabel(title.upper())
        t.setObjectName("CardTitle")
        head.addWidget(t)
        head.addStretch()
        hw = QWidget()
        hw.setLayout(head)
        hw.setFixedWidth(190)
        row.addWidget(hw, 0, Qt.AlignTop)
        box = QWidget()
        flow = FlowLayout(box)
        # told = accent (you said it) · learned = plain (picked up) · seen = plain (from your usage)
        for text, how, tip in items:
            c = chip(text if len(text) <= 48 else text[:46] + "…", "accent" if how == "told" else "")
            c.setToolTip({"told": f"{tip}\nYou told SAINT this.",
                          "learned": f"{tip}\nLearned from conversation — “Keep it” below makes it permanent."}
                         .get(how, tip))
            flow.addWidget(c)
        row.addWidget(box, 1)
        self.sections.addLayout(row)


class MemoryPage(Page):
    def __init__(self):
        super().__init__("Memory", "What SAINT knows about you — only from what you've said, kept on this PC. "
                                   "Say “forget …” or edit anything here.")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search memories…")
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(self.refresh)
        self.actions.addWidget(self.search)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 8, 0)
        col.setSpacing(18)
        scroll.setWidget(inner)
        self.root.addWidget(scroll, 1)

        self.profile = ProfileCard(self)
        col.addWidget(self.profile)

        self.category = Segmented(FILTERS)
        self.category.changed.connect(lambda _i: self.refresh())
        col.addWidget(self.category, 0, Qt.AlignLeft)

        card = Card("Everything SAINT remembers")
        self.count = QLabel("")
        self.count.setObjectName("Faint")
        card.header.addWidget(self.count)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Memory", "Kind", "How", "Updated"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setMinimumHeight(300)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.doubleClicked.connect(self.edit_selected)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        card.body.addWidget(self.table)
        btns = QHBoxLayout()
        self.confirm_btn = QPushButton("Keep it")
        self.confirm_btn.setToolTip("Mark a learned memory as correct — it won't fade out")
        self.confirm_btn.clicked.connect(self.confirm_selected)
        self.confirm_btn.setEnabled(False)
        for label, fn, obj in (("Add…", self.add, "Primary"), ("Edit…", self.edit_selected, ""),
                               (None, None, None), ("Delete", self.delete_selected, "Danger")):
            if label is None:
                btns.addWidget(self.confirm_btn)
                continue
            b = QPushButton(label)
            if obj:
                b.setObjectName(obj)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch()
        note = QLabel("Learned = picked up from conversation; it fades if it never comes up again.")
        note.setObjectName("Faint")
        btns.addWidget(note)
        card.body.addLayout(btns)
        col.addWidget(card)

        self.music = Card("Music memory")
        self.music_label = QLabel("")
        self.music_label.setWordWrap(True)
        self.music_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.music.body.addWidget(self.music_label)
        col.addWidget(self.music)
        col.addStretch()
        self._items = {}
        self._writing = False
        ui_bus.event.connect(self._on_event)

    def apply_theme(self):
        self.profile.apply_theme()

    def _on_event(self, ev):
        if ev.type in (EventType.MEMORY_STORED, EventType.MEMORY_UPDATED, EventType.MEMORY_DELETED) and self.isVisible():
            self.refresh()
            self.refresh_profile()

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()
        self.refresh_profile()
        self.refresh_music()

    # ------------------------------------------------------------------ #
    def refresh_profile(self, force_summary: bool = False):
        from modules.memory import profile as prof_mod

        def load():
            p = prof_mod.build()
            cache = prof_mod.cached_summary()
            fresh = cache.get("fingerprint") == prof_mod._fingerprint(p) and cache.get("text")
            return p, (cache["text"] if fresh and not force_summary else ""), bool(fresh) and not force_summary

        def show(data):
            p, text, fresh = data
            self.profile.render(p, text or prof_mod.fallback_summary(p))
            if not fresh and p["counts"]["total"] >= 2 and not self._writing:
                self._writing = True
                self.profile.refresh_btn.setEnabled(False)
                self.profile.summary.setText((text or prof_mod.fallback_summary(p)) + "\n\nWriting your summary…")
                run_async(lambda: prof_mod.summary(p, force=force_summary), self._summary_done,
                          lambda _e: self._summary_done(prof_mod.fallback_summary(p)))
        run_async(load, show, lambda err: self.profile.summary.setText(f"Couldn't build your profile: {err}"))

    def _summary_done(self, text):
        self._writing = False
        self.profile.summary.setText(text)
        self.profile.refresh_btn.setEnabled(True)

    def refresh(self):
        from modules.memory.service import memory_service
        flt = FILTERS[max(0, self.category.index())]
        query = self.search.text().strip().lower()

        def load():
            items = memory_service.all()
            if flt == "Told":
                items = [e for e in items if e.metadata.get("how", "told") != "learned"]
            elif flt == "Learned":
                items = [e for e in items if e.metadata.get("how") == "learned"]
            elif flt in _FILTER_CATS:
                items = [e for e in items if e.metadata.get("category", "fact") in _FILTER_CATS[flt]]
            return [e for e in items if query in e.content.lower()] if query else items

        def show(items):
            p = current_palette()
            self._items = {e.id: e for e in items}
            self.table.setRowCount(0)
            for e in items:
                r = self.table.rowCount()
                self.table.insertRow(r)
                learned = e.metadata.get("how") == "learned"
                mentions = int(e.metadata.get("mentions", 1))
                how = (f"learned · {int(round(e.confidence * 100))}%" if learned else "told") + \
                    (f" · ×{mentions}" if mentions > 1 else "")
                vals = (e.content, _CAT_LABEL.get(e.metadata.get("category", "fact"), e.metadata.get("category", "")),
                        how, datetime.fromtimestamp(e.updated_at).strftime("%b %d, %H:%M"))
                for c, v in enumerate(vals):
                    it = QTableWidgetItem(v)
                    it.setData(Qt.UserRole, e.id)
                    if c == 2:
                        it.setForeground(QColor(p.muted if learned else p.accent))
                        it.setToolTip("Picked up from conversation. “Keep it” makes it permanent." if learned
                                      else "You told SAINT this.")
                    self.table.setItem(r, c, it)
            self.count.setText(f"{len(items)} shown")
            self._selection_changed()
        run_async(load, show, lambda err: self.count.setText(f"Couldn't read memory: {err}"))

    def _selected_id(self):
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole) if rows else None

    def _selection_changed(self):
        e = self._items.get(self._selected_id())
        self.confirm_btn.setEnabled(bool(e) and e.metadata.get("how") == "learned")

    def add(self):
        text, ok = QInputDialog.getText(self, "Add memory", "What should SAINT remember?\n"
                                        "(e.g. “My favorite programming language is Python”)")
        if ok and text.strip():
            from modules.memory.service import extract_fact, memory_service
            fact = extract_fact(text) or {}
            memory_service.remember(content=fact.get("content", text.strip()), key=fact.get("key", ""),
                                    value=fact.get("value", ""), category=fact.get("category", "fact"))
            self.refresh()

    def edit_selected(self):
        mid = self._selected_id()
        if mid is None:
            return
        from modules.memory.service import extract_fact, memory_service
        entry = memory_service.db.retrieve(mid)
        if entry is None:
            return
        text, ok = QInputDialog.getText(self, "Edit memory", "Memory:", text=entry.content)
        if ok and text.strip():
            fact = extract_fact(text)
            memory_service.update(mid, content=text.strip(), value=fact["value"] if fact else None)
            memory_service.confirm(mid)            # an edited memory is one you vouched for
            self.refresh()

    def confirm_selected(self):
        mid = self._selected_id()
        if mid is not None:
            from modules.memory.service import memory_service
            memory_service.confirm(mid)
            self.refresh()

    def delete_selected(self):
        mid = self._selected_id()
        if mid is not None and QMessageBox.question(self, "Delete memory", "Delete this memory permanently?") \
                == QMessageBox.Yes:
            from modules.memory.service import memory_service
            memory_service.forget(mid)
            self.refresh()

    def refresh_music(self):
        from core.module_manager import module_manager
        sp = module_manager.get("spotify")
        if sp is None:
            self.music.hide()
            return

        def show(s):
            top = ", ".join(f"{a['artist']} ({a['plays']})" for a in s["top_artists"][:6]) or "—"
            genres = ", ".join(g["genre"] for g in s["top_genres"][:6]) or "—"
            recs = s["recommendations"]
            self.music_label.setText(
                f"<b>Plays today</b> {len(s['today'])} &nbsp;·&nbsp; <b>Skips (7 days)</b> {s['skips_7d']} "
                f"&nbsp;·&nbsp; <b>Picks kept</b> {recs['accepted']} of {recs['accepted'] + recs['rejected']}<br>"
                f"<b>Top artists (30 days)</b> {top}<br><b>Top genres</b> {genres}")
        run_async(lambda: sp.tools.memory.snapshot(), show,
                  lambda e: self.music_label.setText(f"Couldn't read music memory: {e}"))

"""
ui/pages/devices.py

Devices: your phone, your other PCs and other people's SAINTs, connected over
your network (modules/link). Pair with a QR code, or type the short code alone
(another PC on the same Wi-Fi or Tailscale finds it), or an IP:port and the code,
choose what each device may do, send a file, say yes or no when a friend's SAINT
asks for something, and keep what they shared.

Nothing listens until "Connect my devices" is on.
"""

import os
import time

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from core.config import config
from ui.reactive import ui_bus
from ui.theme import current_palette
from ui.widgets import Card, Page, Segmented, Switch, chip, clear_layout, run_async, set_chip

POLICIES = ["allow", "ask", "deny"]
POLICY_WORDS = {"allow": "Allow", "ask": "Ask me each time", "deny": "Don't allow"}


def _link():
    from modules.link.service import get_link
    return get_link()


class QrView(QWidget):
    """A QR code painted from its module matrix (no image files, no extra Qt modules)."""

    def __init__(self, size: int = 224):
        super().__init__()
        self.matrix = None
        self.setFixedSize(size, size)

    def set_matrix(self, matrix):
        self.matrix = matrix
        self.update()

    def paintEvent(self, _):
        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        g.setPen(Qt.NoPen)
        g.setBrush(QColor("#ffffff"))
        g.drawRoundedRect(self.rect(), 12, 12)
        if not self.matrix:
            g.setPen(QColor("#555555"))
            g.drawText(self.rect(), Qt.AlignCenter, "QR code unavailable\n(use the code below)")
            return
        n = len(self.matrix)
        pad = 14
        cell = (self.width() - 2 * pad) / (n + 8)          # 4 quiet modules each side
        origin = pad + 4 * cell
        g.setBrush(QColor("#0b0c0e"))
        for y, row in enumerate(self.matrix):
            for x, v in enumerate(row):
                if v:
                    g.drawRect(QRectF(origin + x * cell, origin + y * cell, cell + 0.6, cell + 0.6))


class PermissionsDialog(QDialog):
    def __init__(self, peer: dict, parent=None):
        super().__init__(parent)
        from modules.link.identity import PERMISSIONS
        self.peer = peer
        self.setWindowTitle(f"{peer['name']} — what it may do")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        intro = QLabel("Your own devices can do everything by default. A friend's SAINT can do almost nothing "
                       "until you say so, and “Ask me each time” means SAINT asks you out loud, with the exact "
                       "text, before anything happens.")
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        lay.addWidget(intro)
        form = QFormLayout()
        self.role = QComboBox()
        self.role.addItems(["My own device", "A friend's SAINT"])
        self.role.setCurrentIndex(0 if peer["role"] == "own" else 1)
        form.addRow("This is", self.role)
        self.nick = QLineEdit(", ".join(peer.get("nicknames", [])))
        self.nick.setPlaceholderText("other names you'll call it, separated by commas")
        form.addRow("Also called", self.nick)
        self.boxes = {}
        for perm, text in PERMISSIONS.items():
            box = QComboBox()
            box.addItems([POLICY_WORDS[p] for p in POLICIES])
            box.setCurrentIndex(POLICIES.index(peer["perms"].get(perm, "deny")))
            self.boxes[perm] = box
            form.addRow(text, box)
        lay.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def apply(self):
        link = _link()
        role = "own" if self.role.currentIndex() == 0 else "collaborator"
        if role != self.peer["role"]:
            link.set_role(self.peer["id"], role)
        for perm, box in self.boxes.items():
            link.set_permission(self.peer["id"], perm, POLICIES[box.currentIndex()])
        nicknames = [n.strip() for n in self.nick.text().split(",") if n.strip()]
        link.peers.update(self.peer["id"], nicknames=nicknames)


class DevicesPage(Page):
    def __init__(self):
        super().__init__("Devices", "Your phone, your other PCs and friends' SAINTs, connected over your network.")
        self._offer = None
        self._busy = False
        self.enable = Switch("Connect my devices")
        self.enable.setChecked(bool(config.get("link.enabled", False)))
        self.enable.toggled.connect(self._toggle)
        self.actions.addWidget(self.enable)
        self.status = chip("Off")
        self.actions.addWidget(self.status)

        self.note = QLabel("")
        self.note.setObjectName("Muted")
        self.note.setWordWrap(True)
        self.root.addWidget(self.note)

        # ---- add a device ---------------------------------------------------------------
        self.add_card = Card("Add a device")
        row = QHBoxLayout()
        self.role = Segmented(["My phone or PC", "A friend's SAINT"])
        row.addWidget(self.role)
        self.show_btn = QPushButton("Show pairing code")
        self.show_btn.setObjectName("Primary")
        self.show_btn.clicked.connect(self._show_offer)
        row.addWidget(self.show_btn)
        row.addStretch()
        self.add_card.body.addLayout(row)
        self.offer_box = QWidget()
        ob = QHBoxLayout(self.offer_box)
        ob.setContentsMargins(0, 4, 0, 0)
        ob.setSpacing(18)
        self.qr = QrView()
        ob.addWidget(self.qr)
        col = QVBoxLayout()
        col.setSpacing(6)
        self.how = QLabel("")
        self.how.setWordWrap(True)
        self.how.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(self.how)
        self.code = QLabel("")
        mono = QFont("Cascadia Mono")
        mono.setPointSize(13)
        self.code.setFont(mono)
        self.code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(self.code)
        self.countdown = QLabel("")
        self.countdown.setObjectName("Faint")
        col.addWidget(self.countdown)
        cancel = QPushButton("Close pairing")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self._cancel_offer)
        col.addWidget(cancel, 0, Qt.AlignLeft)
        col.addStretch()
        ob.addLayout(col, 1)
        self.offer_box.setVisible(False)
        self.add_card.body.addWidget(self.offer_box)

        join = QHBoxLayout()
        self.join_text = QLineEdit()
        self.join_text.setPlaceholderText("Join another SAINT: type the code it shows (ABCD-EFGH), or paste its saint:// link")
        join_btn = QPushButton("Join")
        join_btn.clicked.connect(self._join)
        self.join_text.returnPressed.connect(self._join)
        join.addWidget(self.join_text, 1)
        join.addWidget(join_btn)
        self.add_card.body.addLayout(join)
        self.root.addWidget(self.add_card)

        # ---- waiting for a yes ------------------------------------------------------------
        self.approvals_card = Card("Waiting for your OK")
        self.approvals_box = QVBoxLayout()
        self.approvals_card.body.addLayout(self.approvals_box)
        self.approvals_card.setVisible(False)
        self.root.addWidget(self.approvals_card)

        # ---- devices ------------------------------------------------------------------------
        self.devices_card = Card("Paired devices")
        self.devices_box = QVBoxLayout()
        self.devices_box.setSpacing(0)
        self.devices_card.body.addLayout(self.devices_box)
        self.root.addWidget(self.devices_card)

        # ---- shared with you / received files ------------------------------------------------
        self.inbox_card = Card("Received")
        self.inbox_row = QHBoxLayout()
        self.inbox_label = QLabel("")
        self.inbox_label.setObjectName("Muted")
        self.inbox_row.addWidget(self.inbox_label, 1)
        self.keep_btn = QPushButton("Keep what friends shared")
        self.keep_btn.clicked.connect(self._keep_shared)
        self.open_btn = QPushButton("Open received files")
        self.open_btn.clicked.connect(self._open_inbox)
        self.inbox_row.addWidget(self.keep_btn)
        self.inbox_row.addWidget(self.open_btn)
        self.inbox_card.body.addLayout(self.inbox_row)
        self.root.addWidget(self.inbox_card)
        self.root.addStretch()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1500)
        ui_bus.event.connect(self._on_event)
        self.refresh()

    # ------------------------------------------------------------------ #
    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()

    def _say(self, text: str, error: bool = False):
        self.note.setText(text)
        self.note.setObjectName("ErrorText" if error else "Muted")
        self.note.style().unpolish(self.note)
        self.note.style().polish(self.note)

    def _on_event(self, ev):
        if ev.type == "link" and self.isVisible():
            kind = (ev.payload or {}).get("event", "")
            if kind == "link.paired":
                self._cancel_offer()
                self._say(f"Paired with {ev.payload.get('name', 'a new device')}.")
            elif kind == "link.file":
                self._say(f"{ev.payload.get('peer', 'A device')} sent you {ev.payload.get('name', 'a file')}.")
            self.refresh()

    def _tick(self):
        if not self.isVisible():
            return
        if self._offer is not None:
            left = int(self._offer["expires"] - time.time())
            if left <= 0:
                self._cancel_offer()
            else:
                self.countdown.setText(f"Open for {left // 60}:{left % 60:02d} more")
        self.refresh()

    # ------------------------------------------------------------------ #
    def _toggle(self, on: bool):
        if self._busy:
            return
        self._busy = True

        def done(ok):
            self._busy = False
            self._say("SAINT Link is on." if ok else "SAINT Link is off." if not on else
                      "SAINT Link couldn't start (is the port free? see Settings).", error=on and not ok)
            self.refresh()
        run_async(lambda: _link().set_enabled(on), done, lambda e: (setattr(self, "_busy", False), self._say(e, True)))

    def _show_offer(self):
        role = "own" if self.role.index() == 0 else "collaborator"

        def done(info):
            self._offer = info
            self.qr.set_matrix(info.get("matrix"))
            addr = info["addresses"][0] if info["addresses"] else "this PC's IP address"
            who = "your phone or other PC" if role == "own" else "your friend"
            ts = info.get("tailscale") or ""
            away = (f"\nFrom anywhere (Tailscale): {ts}:{info['port']} — the code includes it." if ts else
                    "\nTo reach this PC away from home, install Tailscale on this PC and your phone, then show "
                    "the code again.")
            qr = "" if info.get("matrix") else "\n(The QR code needs the segno package — type the address instead.)"
            self.how.setText(f"On {who}, open SAINT → Devices and scan this code — or on another PC just type the "
                             f"code below into “Join” (same Wi-Fi or Tailscale: no address needed).\n\n"
                             f"Address, if it asks: {addr}:{info['port']}{away}{qr}")
            self.code.setText(info["code"])
            self.offer_box.setVisible(True)
            self.enable.setChecked(True)
            self.refresh()
        run_async(lambda: _link().offer(role), done, lambda e: self._say(str(e), True))

    def _cancel_offer(self):
        self._offer = None
        self.offer_box.setVisible(False)
        try:
            _link().pairing.cancel()
        except Exception:
            pass

    def _join(self):
        text = self.join_text.text().strip()
        if not text:
            return
        self._say("Pairing…")

        def done(peer):
            self.join_text.clear()
            self._say(f"Paired with {peer.name}.")
            self.refresh()
        run_async(lambda: _link().pair(text, None if text.lower().startswith("saint://") else
                                       ("own" if self.role.index() == 0 else "collaborator")),
                  done, lambda e: self._say(str(e), True))

    # ------------------------------------------------------------------ #
    def refresh(self):
        link = _link()
        running = link.running
        if self.enable.isChecked() != link.enabled and not self._busy:
            self.enable.blockSignals(True)
            self.enable.setChecked(link.enabled)
            self.enable.blockSignals(False)
        if running:
            st = link.status()
            addrs = st["addresses"]
            remote = " · Tailscale on" if st.get("tailscale") else ""
            set_chip(self.status, f"Listening on {addrs[0] if addrs else '…'}:{link.node.port}{remote}", "ok")
        else:
            set_chip(self.status, "Off", "")
        self.add_card.setEnabled(True)
        self._render_approvals(link)
        self._render_devices(link)
        pending = link.shared_inbox.pending()
        files = link._inbox_dir()
        self.inbox_label.setText((f"{len(pending)} thing{'s' if len(pending) != 1 else ''} shared with you are waiting. "
                                  if pending else "Nothing shared with you is waiting. ") +
                                 f"Files from your devices land in {files}.")
        self.keep_btn.setVisible(bool(pending))

    def _render_approvals(self, link):
        items = link.approvals.pending()
        sig = [(a["id"], a["description"]) for a in items]
        if sig == getattr(self, "_approvals_sig", None):
            return                                  # nothing changed: don't rebuild (and flicker) every tick
        self._approvals_sig = sig
        self.approvals_card.setVisible(bool(items))
        clear_layout(self.approvals_box)
        for a in items:
            row = QHBoxLayout()
            label = QLabel(f"<b>{a['peer']}</b> wants to {a['description']}")
            label.setWordWrap(True)
            row.addWidget(label, 1)
            yes = QPushButton("Allow")
            yes.setObjectName("Primary")
            yes.clicked.connect(lambda _=False, i=a["id"]: (link.approvals.resolve(i, True), self.refresh()))
            no = QPushButton("Deny")
            no.setObjectName("Danger")
            no.clicked.connect(lambda _=False, i=a["id"]: (link.approvals.resolve(i, False), self.refresh()))
            row.addWidget(yes)
            row.addWidget(no)
            holder = QWidget()
            holder.setLayout(row)
            self.approvals_box.addWidget(holder)

    def _render_devices(self, link):
        devices = link.devices()
        sig = [(d["id"], d["name"], d["role"], d["platform"], d["connected"], tuple(sorted(d["perms"].items())))
               for d in devices]
        if sig == getattr(self, "_devices_sig", None):
            return
        self._devices_sig = sig
        clear_layout(self.devices_box)
        if not devices:
            empty = QLabel("No devices yet. Use “Show pairing code” above, then scan it from SAINT on your phone.")
            empty.setObjectName("Muted")
            empty.setWordWrap(True)
            self.devices_box.addWidget(empty)
            return
        for d in devices:
            row = QFrame()
            row.setObjectName("Row")
            h = QHBoxLayout(row)
            h.setContentsMargins(2, 10, 2, 10)
            name = QLabel(d["name"])
            name.setObjectName("SectionTitle")
            h.addWidget(name)
            h.addWidget(chip("My device" if d["role"] == "own" else "Friend", "accent" if d["role"] == "own" else ""))
            if d["platform"]:
                h.addWidget(chip({"ios": "iPhone", "windows": "Windows", "mac": "Mac"}.get(d["platform"], d["platform"])))
            h.addWidget(chip("Connected" if d["connected"] else "Offline", "ok" if d["connected"] else "warn"))
            h.addStretch()
            if not d["connected"]:
                b = QPushButton("Check connection")
                b.setToolTip("Try every address SAINT knows for it and say what's in the way")
                b.clicked.connect(lambda _=False, i=d["id"]: self._check(i))
                h.addWidget(b)
            if d["connected"] and d["role"] == "own":
                b = QPushButton("Sync now")
                b.clicked.connect(lambda _=False, i=d["id"]: self._sync(i))
                h.addWidget(b)
            if d["connected"]:
                b = QPushButton("Send file…")
                b.clicked.connect(lambda _=False, i=d["id"]: self._send_file(i))
                h.addWidget(b)
            b = QPushButton("Rename…")
            b.clicked.connect(lambda _=False, i=d["id"], n=d["name"]: self._rename(i, n))
            h.addWidget(b)
            b = QPushButton("Permissions…")
            b.clicked.connect(lambda _=False, dev=d: self._permissions(dev))
            h.addWidget(b)
            b = QPushButton("Remove")
            b.setObjectName("Danger")
            b.clicked.connect(lambda _=False, i=d["id"], n=d["name"]: self._remove(i, n))
            h.addWidget(b)
            self.devices_box.addWidget(row)

    # ------------------------------------------------------------------ #
    def _check(self, peer_id):
        self._say("Checking…")

        def run():
            from modules.link.service import ensure_firewall_rule
            lines = _link().check(peer_id)
            if not any("answers" in ln or "Connected" in ln for ln in lines):
                ensure_firewall_rule(_link().node.port or int(config.get("link.port", 8765)), force=True)
            return lines
        run_async(run, lambda lines: self._say("\n".join(lines), not any("answers" in ln or "Connected" in ln
                                                                          for ln in lines)),
                  lambda e: self._say(str(e), True))

    def _sync(self, peer_id):
        self._say("Syncing…")
        run_async(lambda: _link().sync_with(peer_id),
                  lambda r: self._say(f"Synced: received {r['received']}, sent {r['sent']}." if r["received"] or r["sent"]
                                      else "Already in sync — nothing new on either side."),
                  lambda e: self._say(str(e), True))

    def _send_file(self, peer_id):
        path, _ = QFileDialog.getOpenFileName(self, "Send a file")
        if not path:
            return
        peer = _link().peers.get(peer_id)
        self._say(f"Sending {os.path.basename(path)}…")
        run_async(lambda: _link().send_file(peer, path),
                  lambda r: self._say(f"Sent {os.path.basename(path)} to {peer.name}."),
                  lambda e: self._say(str(e), True))

    def _permissions(self, device: dict):
        dlg = PermissionsDialog(device, self)
        if dlg.exec() == QDialog.Accepted:
            dlg.apply()
            self.refresh()

    def _rename(self, peer_id, name):
        from PySide6.QtWidgets import QInputDialog
        new, ok = QInputDialog.getText(self, "Rename device", "Name for this device:", text=name)
        if ok and new.strip() and new.strip() != name:
            _link().rename_peer(peer_id, new)
            self._say(f"Renamed to {' '.join(new.split())[:40]}. Say it by that name, e.g. “lock {new.strip()}”.")
            self.refresh()

    def _remove(self, peer_id, name):
        _link().unpair(peer_id)
        self._say(f"Removed {name}. It can't reconnect unless you pair again.")
        self.refresh()

    def _keep_shared(self):
        n = _link().accept_shared()
        self._say(f"Kept {n} shared thing{'s' if n != 1 else ''}.")
        self.refresh()

    def _open_inbox(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(_link()._inbox_dir()))

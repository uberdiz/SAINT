"""
modules/link/service.py

SAINT Link for this PC: the one object the rest of SAINT talks to.

    link = get_link()
    link.start()                    # when Settings -> Devices -> "Connect my devices" is on
    link.offer("own")               # a code + QR for a phone or another PC of yours
    link.pair("saint://pair?...")   # or "192.168.1.20:8765 ABCD-EFGH", or just "ABCD-EFGH"
                                    # (finds the SAINT with that window open on this network or Tailscale)
    link.run_remote(peer, "send_prompt", {"target": "claude", "prompt": "..."})

It owns the node (modules/link/node.py), registers what a paired device may ask
(talk to this SAINT, sync, send files, run an automation, share something,
push context), keeps the synced stores up to date, and tells the rest of SAINT
what happened through one event, ``EventType.LINK``, whose payload has an
``event`` name (``link.connected``, ``link.paired``, ``link.file``,
``link.message``, ``link.approval``, ...).

Nothing here starts on import, and nothing listens unless ``link.enabled`` is on.
"""

import json
import logging
import os
import threading
import time
import uuid
from typing import Dict, List, Optional

from core.config import config
from core.events import EventType, event_bus
from core.paths import data_path
from modules.link import automations
from modules.link.approvals import ApprovalQueue
from modules.link.context_feed import context_feed
from modules.link.identity import (ALLOW, COLLABORATOR, DENY, OWN, PERMISSIONS, Identity, PairingManager, Peer,
                                   PeerStore, decode_code, looks_like_code, parse_address, parse_pair_uri)
from modules.link.node import DEFAULT_PORT, LinkNode, dial_first, is_tailscale, lan_addresses
from modules.link.sync import SyncEngine
from modules.link.wire import LinkError

log = logging.getLogger("saint.link")

_SYNC_EVENTS = None


def plain_answer(question: str, context_line: str = "") -> str:
    """The language model on its own: no memories, no tools, no PC access.
    Used when someone else's SAINT asks this one a question."""
    from core.module_manager import module_manager
    from modules.agent.output import clean_reply
    from modules.ai.providers import get_provider
    name = config.get("ai.provider", "ollama")
    base_url = config.get("ai.base_url", "http://localhost:11434")
    ai = module_manager.get("ai")
    model, _ = ai._resolve_model(name, base_url, config.get("ai.model", ""))
    system = ("You are SAINT, a voice assistant. " + context_line + " Answer in one or two short sentences. "
              "You don't know anything about the owner's private life, files or accounts, and you can't "
              "control their computer from here; say so if asked.")
    text = get_provider(name).send(messages=[{"role": "system", "content": system},
                                             {"role": "user", "content": question}],
                                   model=model, api_key=config.get("ai.api_key", ""), base_url=base_url,
                                   temperature=float(config.get("ai.temperature", 0.5)),
                                   timeout=float(config.get("ai.timeout_seconds", 30)))
    return clean_reply(text, question) or text.strip()


def qr_matrix(text: str):
    """The QR code for ``text`` as rows of 0/1 (for the Devices page), or None
    when the optional ``segno`` package isn't installed."""
    try:
        import segno
        return [[1 if cell else 0 for cell in row] for row in segno.make(text, error="m", micro=False).matrix]
    except Exception:
        return None


class LinkService:
    def __init__(self, identity: Optional[Identity] = None, peers: Optional[PeerStore] = None,
                 engine: Optional[SyncEngine] = None):
        self.identity = identity or Identity()
        self.peers = peers or PeerStore()
        self.pairing = PairingManager()
        self.approvals = ApprovalQueue(emit=self._emit, announce=self._announce)
        self.node = LinkNode(self.identity, self.peers, self.pairing, self.approvals, emit=self._emit,
                             inbox_dir=self._inbox_dir, max_file_mb=lambda: float(config.get("link.max_file_mb", 1024)))
        self._engine = engine
        self._discovery = None
        self._lock = threading.RLock()
        self._chat_lock = threading.Lock()
        self._sync_timer: Optional[threading.Timer] = None
        self._sync_lock = threading.Lock()
        self._stop = threading.Event()
        self._started = False
        self._subscribed = False
        self.shared_inbox = SharedInbox()
        self._register_handlers()

    # ------------------------------------------------------------------ #
    @property
    def engine(self) -> SyncEngine:
        if self._engine is None:
            self._engine = SyncEngine(self.identity.device_id)
        return self._engine

    @staticmethod
    def _inbox_dir() -> str:
        custom = str(config.get("link.inbox_dir", "") or "").strip()
        if custom:
            os.makedirs(custom, exist_ok=True)
            return custom
        return str(data_path("link", "inbox"))

    # ------------------------------------------------------------------ #
    # Events out
    # ------------------------------------------------------------------ #
    def _emit(self, name: str, payload: dict):
        event_bus.emit_event(EventType.LINK, dict(payload, event=name))
        try:
            if name == "link.connected":
                peer = self.peers.get(payload.get("peer_id", ""))
                if peer is not None and peer.role == OWN and peer.permission("sync") == ALLOW:
                    threading.Thread(target=self._safe_sync, args=(peer.id,), daemon=True, name="link-sync").start()
            elif name == "link.paired":
                event_bus.emit_event(EventType.NOTIFY, {"title": "SAINT",
                                                        "message": f"Paired with {payload.get('name', 'a device')}."})
            elif name == "link.file":
                event_bus.emit_event(EventType.NOTIFY, {
                    "title": "SAINT", "message": f"{payload['peer']} sent you {payload['name']}."})
                self._announce(f"{payload['peer']} sent you {payload['name']}.", "link")
            elif name == "link.message":
                event_bus.emit_event(EventType.NOTIFY, {"title": payload["peer"], "message": payload["text"]})
                self._announce(f"{payload['peer']} says: {payload['text']}", "link")
            elif name == "link.approval":
                event_bus.emit_event(EventType.NOTIFY, {"title": "SAINT", "message": f"{payload['peer']} wants to "
                                                                                     f"{payload['description']}"})
        except Exception:
            log.exception("link.emit_side_effect_failed %s", name)

    @staticmethod
    def _announce(text: str, source: str = "link"):
        try:
            from core.conversation import get_controller
            controller = get_controller()
            if controller is not None and config.get("link.announce", True):
                controller.announce(text, source=source)
        except Exception:
            log.debug("link.announce_failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return bool(config.get("link.enabled", False))

    @property
    def running(self) -> bool:
        return self.node.running

    def start(self) -> bool:
        """Listen and connect, when Link is turned on. Safe to call again."""
        with self._lock:
            if not self.enabled or self.node.running:
                return self.node.running
            if config.get("link.device_name", ""):
                self.identity.set_name(config.get("link.device_name", ""))
            self.node.auto_connect = bool(config.get("link.auto_connect", True))
            self.node.approval_timeout = float(config.get("link.approval_timeout_sec", 60))
            try:
                port = self.node.start(str(config.get("link.bind", "0.0.0.0")), int(config.get("link.port", DEFAULT_PORT)))
            except OSError as e:
                log.error("link.cannot_listen %s", e)
                event_bus.emit_event(EventType.WARNING, {"message": f"SAINT Link couldn't open its port: {e}"})
                return False
            self._stop.clear()
            if config.get("link.discoverable", True):
                from modules.link.discovery import Discovery
                self._discovery = Discovery(self.identity.device_id, self.identity.name, self._found,
                                            pairing_open=lambda: (self.pairing.current.role
                                                                  if self.pairing.current else ""))
                self._discovery.start(port)
            threading.Thread(target=ensure_firewall_rule, args=(port,), daemon=True, name="link-firewall").start()
            if not self._subscribed:
                event_bus.subscribe(self._on_event)
                self._subscribed = True
            threading.Thread(target=self._sync_loop, daemon=True, name="link-sync-loop").start()
            self._started = True
            log.info("link.started port=%d", port)
            return True

    def stop(self):
        with self._lock:
            self._stop.set()
            if self._sync_timer is not None:
                self._sync_timer.cancel()
            if self._discovery is not None:
                self._discovery.stop()
                self._discovery = None
            self.pairing.cancel()
            self.node.stop()
            if self._subscribed:
                event_bus.unsubscribe(self._on_event)
                self._subscribed = False
            self._started = False
            log.info("link.stopped")

    def set_enabled(self, on: bool) -> bool:
        config.set("link.enabled", bool(on))
        if on:
            return self.start()
        self.stop()
        return False

    def _found(self, peer_id: str, host: str, port: int):
        if self.peers.get(peer_id) is not None:
            self.node.address_hints[peer_id] = (host, port)

    # ------------------------------------------------------------------ #
    # Pairing
    # ------------------------------------------------------------------ #
    def offer(self, role: str = OWN, label: str = "") -> dict:
        """Open a pairing window (5 minutes) and describe how to join it."""
        if not self.node.running:
            if not self.start():
                raise LinkError("Turn on SAINT Link first (Devices page, or say “turn on SAINT Link”).", "disabled")
        offer = self.pairing.create(role, label)
        addrs = lan_addresses()
        host = addrs[0] if addrs else "127.0.0.1"
        ts = tailscale_status()
        alternates = addrs[1:] + [a for a in (ts.get("dns"),) if a]
        uri = offer.uri(host, self.node.port, self.identity.name, alternates=alternates)
        if self._discovery is not None:
            self._discovery.announce_now()          # PCs nearby see the open window straight away
        return {"code": offer.code, "uri": uri, "port": self.node.port, "addresses": addrs, "role": role,
                "expires": offer.expires, "matrix": qr_matrix(uri), "name": self.identity.name,
                "tailscale": tailscale_address(addrs) or ts.get("ip", ""), "tailscale_name": ts.get("dns", "")}

    def pair(self, text: str, role: Optional[str] = None) -> Peer:
        """Join another device's pairing window. ``text`` is the ``saint://pair?...``
        link (what the QR code holds), ``ip:port`` followed by the code, or just the
        code — then the SAINT with that window open is found on this network (or
        among your Tailscale devices)."""
        text = (text or "").strip()
        if not self.node.running:
            self.node.start("0.0.0.0", int(config.get("link.port", DEFAULT_PORT))) if self.enabled else None
        if text.lower().startswith("saint://"):
            info = parse_pair_uri(text)
            # every address at once: the home one, then Tailscale (whichever answers first)
            return self.node.pair([info["host"]] + info.get("alternates", []), info["port"], info["token"],
                                  role or info["role"])
        parts = text.replace(",", " ").split()
        if parts and looks_like_code("".join(parts)) and not any(c in parts[0] for c in ".:[") :
            return self._pair_by_code("".join(parts), role or OWN)
        if len(parts) < 2:
            raise LinkError("Type the code the other device shows (like “ABCD-EFGH”), or its address and the code.",
                            "bad_args")
        host, port = parse_address(parts[0])
        return self.node.pair(host, port, "".join(parts[1:]), role or OWN)

    def _pair_by_code(self, code: str, role: str) -> Peer:
        """Only a code: try the SAINTs that say they have a window open (UDP beacon), then the
        ones on your Tailscale network that answer on SAINT's port."""
        token = decode_code(code)
        port = int(config.get("link.port", DEFAULT_PORT))
        tried = set()
        d = self._discovery
        if d is not None:
            d.probe()
            deadline = time.time() + 2.5
            while time.time() < deadline and not d.nearby(pairing_only=True):
                time.sleep(0.2)
            for found in d.nearby(pairing_only=True):
                tried.add(found["host"])
                try:
                    return self.node.pair(found["host"], found["port"], token, role)
                except LinkError as e:
                    if e.code not in ("unreachable", "timeout", "closed", "pair_failed", "handshake"):
                        raise
        hosts = [h for h in tailscale_status().get("peers", []) if h not in tried]
        if hosts:
            live = []
            lock = threading.Lock()

            def probe(h):
                try:
                    sock, _ = dial_first([h], port, 2.0)
                    sock.close()
                    with lock:
                        live.append(h)
                except LinkError:
                    pass
            threads = [threading.Thread(target=probe, args=(h,), daemon=True) for h in hosts[:64]]
            for t in threads:
                t.start()
            for t in threads:
                t.join(3.0)
            for h in live:
                try:
                    return self.node.pair(h, port, token, role)
                except LinkError as e:
                    if e.code not in ("unreachable", "timeout", "closed", "pair_failed", "handshake"):
                        raise
        if tried or hosts:
            raise LinkError("I found SAINT nearby, but that code didn't work. Check it, or make a new one on the "
                            "other PC (codes last five minutes).", "pair_failed")
        raise LinkError("I couldn't find a SAINT with a pairing window open. On the other PC open Devices → Pair "
                        "another PC, and make sure both have SAINT Link on (same Wi-Fi, or both on Tailscale) — "
                        "or type its address before the code.", "not_found")

    def nearby(self) -> List[dict]:
        """SAINTs heard on this network (paired or not)."""
        if self._discovery is None:
            return []
        paired = {p.id for p in self.peers.all()}
        return [dict(d, paired=d["id"] in paired) for d in self._discovery.nearby()]

    def check(self, peer_id: str) -> List[str]:
        """Why can't I reach it? One line per address tried, in plain words."""
        peer = self.peers.get(peer_id)
        if peer is None:
            return ["That device isn't paired any more."]
        if not self.node.running:
            return ["SAINT Link is off on this PC — turn it on in Devices."]
        hint = self.node.address_hints.get(peer.id)
        port = (hint[1] if hint else 0) or peer.port
        out = []
        if self.node.session_for(peer.id) is not None:
            out.append(f"Connected to {peer.name} right now.")
        hosts = self.node.candidates(peer)
        if not hosts or not port:
            return out + [f"I don't have an address for {peer.name} yet. Open SAINT on it (its Link must be on) or "
                          f"pair again."]
        for h in hosts:
            label = f"{h}:{port}" + (" (Tailscale)" if is_tailscale(h) or h.endswith(".ts.net") else "")
            try:
                sock, _ = dial_first([h], port, 3.0)
                sock.close()
                out.append(f"{label}: SAINT answers.")
            except LinkError:
                why = ("not reachable — is Tailscale on (and logged in to the same account) on both devices?"
                       if is_tailscale(h) or h.endswith(".ts.net") else
                       "not reachable — different network, the device is asleep, or its firewall blocks SAINT's port")
                out.append(f"{label}: {why}")
        ts = tailscale_status()
        if ts.get("installed") and not ts.get("ip"):
            out.append("Tailscale is installed here but not connected — open it and log in.")
        if peer.platform in ("windows", "") and not any("answers" in o for o in out):
            out.append("If the other PC is on, allow SAINT through its firewall: on that PC turn SAINT Link off "
                       "and on again and accept the Windows prompt.")
        return out

    # ------------------------------------------------------------------ #
    # Devices
    # ------------------------------------------------------------------ #
    def devices(self) -> List[dict]:
        connected = set(self.node.connected_ids())
        out = []
        for p in sorted(self.peers.all(), key=lambda p: p.name.lower()):
            d = p.public()
            d["connected"] = p.id in connected
            out.append(d)
        return out

    def status(self) -> dict:
        offer = self.pairing.current
        addrs = lan_addresses() if self.node.running else []
        return {"enabled": self.enabled, "running": self.node.running, "port": self.node.port,
                "addresses": addrs, "tailscale": tailscale_address(addrs), "device": self.identity.hello(),
                "devices": self.devices(), "pending_approvals": self.approvals.pending(),
                "pairing": None if offer is None else {"role": offer.role, "expires": offer.expires},
                "discovery": self._discovery.backends if self._discovery else [],
                "inbox": self._inbox_dir()}

    def set_permission(self, peer_id: str, perm: str, policy: str) -> Optional[Peer]:
        if perm not in PERMISSIONS or policy not in (ALLOW, "ask", DENY):
            raise ValueError("unknown permission")
        return self.peers.update(peer_id, perms={perm: policy})

    def set_role(self, peer_id: str, role: str) -> Optional[Peer]:
        if role not in (OWN, COLLABORATOR):
            raise ValueError("role must be own or collaborator")
        peer = self.peers.update(peer_id, role=role, perms={})
        if peer is not None:
            peer.perms = {}
            self.peers.save(peer)
        return peer

    def rename_peer(self, peer_id: str, name: str):
        return self.peers.update(peer_id, name=" ".join(name.split())[:40])

    def unpair(self, peer_id: str) -> bool:
        self.node.close_peer(peer_id)
        return self.peers.remove(peer_id)

    def find_peer(self, spoken: str) -> Optional[Peer]:
        return self.peers.find_by_name(spoken)

    # ------------------------------------------------------------------ #
    # Outgoing
    # ------------------------------------------------------------------ #
    def run_remote(self, peer: Peer, name: str, args: dict, timeout: float = 150.0) -> dict:
        return self.node.request(peer.id, "automation.run", {"name": name, "args": args}, timeout)

    def ask_peer(self, peer: Peer, text: str, timeout: float = 120.0) -> dict:
        return self.node.request(peer.id, "chat.ask", {"text": text}, timeout)

    def send_file(self, peer: Peer, path: str, progress=None) -> dict:
        return self.node.send_file(peer.id, path, progress)

    def share(self, peer: Peer, kind: str, uid: str, data: dict) -> dict:
        return self.node.request(peer.id, "share.push", {"items": [{"k": kind, "u": uid, "d": data}]})

    # ------------------------------------------------------------------ #
    # Sync
    # ------------------------------------------------------------------ #
    def sync_with(self, peer_id: str) -> dict:
        eng = self.engine
        eng.scan()
        resp = self.node.request(peer_id, "sync.manifest", {"manifest": eng.manifest()}, timeout=90)
        applied = eng.apply(resp.get("items") or [])
        items = eng.items_for(resp.get("want") or [])
        for i in range(0, len(items), 150):
            self.node.request(peer_id, "sync.push", {"items": items[i:i + 150]}, timeout=90)
        self.peers.update(peer_id, last_seen=time.time())
        if applied or items:
            log.info("link.sync peer=%s received=%d sent=%d", peer_id, applied, len(items))
        return {"received": applied, "sent": len(items)}

    def _safe_sync(self, peer_id: str):
        with self._sync_lock:
            try:
                self.sync_with(peer_id)
            except LinkError as e:
                log.info("link.sync_failed peer=%s %s", peer_id, e)
            except Exception:
                log.exception("link.sync_crashed peer=%s", peer_id)

    def sync_now(self) -> int:
        n = 0
        for pid in self.node.connected_ids():
            peer = self.peers.get(pid)
            if peer is not None and peer.role == OWN and peer.permission("sync") == ALLOW:
                self._safe_sync(pid)
                n += 1
        return n

    def _schedule_sync(self, delay: float = 3.0):
        with self._lock:
            if self._sync_timer is not None:
                self._sync_timer.cancel()
            self._sync_timer = threading.Timer(delay, self.sync_now)
            self._sync_timer.daemon = True
            self._sync_timer.start()

    def _sync_loop(self):
        while not self._stop.wait(max(15.0, float(config.get("link.sync_interval_sec", 60)))):
            try:
                self.sync_now()
            except Exception:
                log.exception("link.sync_loop")

    # ------------------------------------------------------------------ #
    # Local events: changes to sync, turns to share
    # ------------------------------------------------------------------ #
    def _on_event(self, ev):
        t = ev.type
        p = ev.payload or {}
        if t in (EventType.MEMORY_STORED, EventType.MEMORY_UPDATED, EventType.MEMORY_DELETED,
                 EventType.AUTOMATION_CREATED, EventType.AUTOMATION_UPDATED, EventType.AUTOMATION_CANCELLED):
            if not p.get("link") and self.node.connected_ids():
                self._schedule_sync()
        elif t == EventType.CONVERSATION_TURN_END:
            self._share_turn(p)

    def _share_turn(self, p: dict):
        if not config.get("link.share_context", True) or not self.node.connected_ids():
            return
        user, reply = p.get("user_text", ""), p.get("response", "")
        if not user:
            return
        self.node.broadcast("context.feed", {"source": self.identity.name, "user": user[:240],
                                             "reply": reply[:240], "ts": time.time()}, perm="context")

    # ------------------------------------------------------------------ #
    # What paired devices may ask
    # ------------------------------------------------------------------ #
    def _register_handlers(self):
        n = self.node
        n.register("status.get", self._h_status)
        n.register("chat.ask", self._h_chat)
        n.register("sync.manifest", self._h_sync_manifest)
        n.register("sync.push", self._h_sync_push)
        n.register("automation.run", lambda ctx, d: automations.run(ctx, str(d.get("name", "")), d.get("args") or {}))
        n.register("automation.list", lambda ctx, d: {"automations": automations.catalog(ctx.peer)})
        n.register("share.push", self._h_share)
        n.register("context.feed", self._h_context)

    def _h_status(self, ctx, data: dict) -> dict:
        ctx.require("status", "tell you what this PC is doing")
        playing = {}
        try:
            from modules.desktop.media import media
            cur = media.current() if hasattr(media, "current") else None
            if cur:
                playing = {k: cur.get(k) for k in ("title", "artist", "app", "playing") if isinstance(cur, dict)}
        except Exception:
            pass
        return {"device": self.identity.hello(), "time": time.time(), "now_playing": playing,
                "language": config.get("language.preferred", [])}

    def _h_chat(self, ctx, data: dict) -> dict:
        ctx.require("chat")
        text = str(data.get("text", "")).strip()
        if not text or len(text) > 2000:
            raise LinkError("Send me something to answer (under 2000 characters).", "bad_args")
        return self.process_text(text, str(data.get("lang", "") or ""), source=ctx.peer.name)

    def process_text(self, text: str, language: str = "", source: str = "remote") -> dict:
        """Run ``text`` through SAINT's agent and language model as if it had been
        typed here, and return the answer instead of speaking it."""
        from core.conversation import get_controller
        from core.module_manager import module_manager
        ai = module_manager.get("ai")
        controller = get_controller()
        deadline = time.monotonic() + 25
        while controller is not None and controller.busy and time.monotonic() < deadline:
            time.sleep(0.2)                                   # don't cut in on an answer being spoken here
        parts: List[str] = []
        with self._chat_lock:
            ai.stream_prompt(prompt=text, on_token=parts.append, turn_id=int(time.time() * 1000) % 1_000_000_000,
                             language=language, source=source)
            expects = bool(getattr(ai, "expects_reply", False))
            lang = getattr(ai, "last_language", "") or language
            failed = str(getattr(ai, "model_failed", "") or "")
        reply = "".join(parts).strip()
        context_feed.add(f"{source} (asked this PC)", text, reply)
        # "ok": false tells the phone the PC's model couldn't answer (Ollama stopped, model not
        # installed), so it answers with its own model instead of reading the error out.
        return {"text": reply, "expects_reply": expects, "lang": lang, "ok": not failed, "model_error": failed}

    def _h_sync_manifest(self, ctx, data: dict) -> dict:
        ctx.require("sync")
        if ctx.peer.role != OWN:
            raise LinkError("Only your own devices can sync.", "denied")
        eng = self.engine
        eng.scan()
        want, offer = eng.diff(data.get("manifest") or {})
        return {"want": want, "items": offer}

    def _h_sync_push(self, ctx, data: dict) -> dict:
        ctx.require("sync")
        if ctx.peer.role != OWN:
            raise LinkError("Only your own devices can sync.", "denied")
        items = data.get("items") or []
        if not isinstance(items, list):
            raise LinkError("bad items", "bad_args")
        return {"applied": self.engine.apply(items)}

    def _h_share(self, ctx, data: dict) -> dict:
        ctx.require("share")
        items = data.get("items") or []
        if not isinstance(items, list) or not items or len(items) > 50:
            raise LinkError("bad share", "bad_args")
        added = [self.shared_inbox.add(ctx.peer.id, ctx.peer.name, it.get("k"), str(it.get("u") or uuid.uuid4().hex[:16]),
                                       it.get("d"))
                 for it in items if isinstance(it, dict) and it.get("k") in ("memory", "skill", "alias", "scene")
                 and isinstance(it.get("d"), dict)]
        if added:
            self._emit("link.shared", {"peer": ctx.peer.name, "count": len(added)})
            self._announce(f"{ctx.peer.name} shared {len(added)} thing{'s' if len(added) != 1 else ''} with you. "
                           f"Say “accept what {ctx.peer.name} shared” to keep them.")
        return {"received": len(added)}

    def _h_context(self, ctx, data: dict) -> dict:
        ctx.require("context")
        if config.get("link.share_context", True) and ctx.peer.role == OWN:
            context_feed.add(str(data.get("source") or ctx.peer.name), str(data.get("user", "")),
                             str(data.get("reply", "")), ts=float(data.get("ts") or 0) or time.time())
        return {}

    # ------------------------------------------------------------------ #
    def accept_shared(self, peer_id: Optional[str] = None) -> int:
        """Keep what collaborators shared (everything, or only from one peer)."""
        eng = self.engine
        n = 0
        for entry in self.shared_inbox.pending(peer_id):
            adapter = eng.adapters.get(entry["kind"])
            try:
                if adapter is not None:
                    adapter.apply_batch([(entry["uid"], entry["data"])])
                    n += 1
            except Exception:
                log.exception("link.accept_shared_failed")
            self.shared_inbox.remove(entry["id"])
        return n


class SharedInbox:
    """Things collaborators shared with you, waiting for a yes (data/link/shared.json)."""

    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return self._path or str(data_path("link", "shared.json"))

    def _load(self) -> List[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, items: List[dict]):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
        os.replace(tmp, self.path)

    def add(self, peer_id: str, peer_name: str, kind: str, uid: str, data: dict) -> dict:
        entry = {"id": uuid.uuid4().hex[:8], "peer_id": peer_id, "peer": peer_name, "kind": kind,
                 "uid": uid, "data": data, "ts": time.time()}
        with self._lock:
            items = (self._load() + [entry])[-200:]
            self._save(items)
        return entry

    def pending(self, peer_id: Optional[str] = None) -> List[dict]:
        with self._lock:
            return [e for e in self._load() if peer_id is None or e["peer_id"] == peer_id]

    def remove(self, entry_id: str):
        with self._lock:
            self._save([e for e in self._load() if e["id"] != entry_id])


_link: Optional[LinkService] = None
_link_lock = threading.Lock()


_ts_cache = {"at": 0.0, "value": {}}


def tailscale_status(max_age: float = 60.0) -> dict:
    """This PC's Tailscale state from the ``tailscale`` command, if it's installed:
    {"installed", "ip", "dns" (MagicDNS name), "peers": [online peers' 100.x addresses]}.
    Cached for a minute; {} when Tailscale isn't installed."""
    now = time.time()
    if now - _ts_cache["at"] < max_age:
        return _ts_cache["value"]
    import shutil
    import subprocess
    exe = shutil.which("tailscale")
    if exe is None and os.name == "nt":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
            cand = os.path.join(base, "Tailscale", "tailscale.exe") if base else ""
            if cand and os.path.isfile(cand):
                exe = cand
                break
    value: dict = {}
    if exe:
        value = {"installed": True, "ip": "", "dns": "", "peers": []}
        try:
            kw = {"creationflags": 0x08000000} if os.name == "nt" else {}       # no console window
            out = subprocess.run([exe, "status", "--json"], capture_output=True, timeout=4, **kw)
            data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
            me = data.get("Self") or {}
            value["ip"] = next((a for a in me.get("TailscaleIPs") or [] if is_tailscale(a)), "")
            value["dns"] = str(me.get("DNSName") or "").rstrip(".")
            for p in (data.get("Peer") or {}).values():
                if p.get("Online"):
                    ip = next((a for a in p.get("TailscaleIPs") or [] if is_tailscale(a)), "")
                    if ip:
                        value["peers"].append(ip)
        except Exception:
            log.debug("link.tailscale_status_failed", exc_info=True)
    _ts_cache.update(at=now, value=value)
    return value


def ensure_firewall_rule(port: int, udp_port: int = 8766, force: bool = False):
    """Windows Firewall blocks SAINT Link's port by default on networks Windows calls "public" —
    and a dismissed "allow access?" prompt blocks it everywhere. That's the usual reason two devices
    "both connected in Tailscale" still can't reach SAINT. Add an inbound rule limited to the local
    network and Tailscale's range (once; it asks for admin rights the first time)."""
    if os.name != "nt" or not config.get("link.manage_firewall", True):
        return
    import subprocess
    name = "SAINT Link"
    kw = {"creationflags": 0x08000000, "capture_output": True, "timeout": 10}
    try:
        shown = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"], **kw)
        if shown.returncode == 0 and str(port).encode() in shown.stdout:
            return
        if config.get("link.firewall_asked_port", 0) == port and not force:
            return                                   # asked once already; don't keep prompting
        config.set("link.firewall_asked_port", port)
        remote = "localsubnet,100.64.0.0/10"
        script = (f'netsh advfirewall firewall delete rule name="{name}" & '
                  f'netsh advfirewall firewall add rule name="{name}" dir=in action=allow protocol=TCP '
                  f'localport={port} remoteip={remote} profile=any & '
                  f'netsh advfirewall firewall add rule name="{name}" dir=in action=allow protocol=UDP '
                  f'localport={udp_port} remoteip=localsubnet profile=any')
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f'/c {script}', None, 0)
        log.info("link.firewall_rule_requested port=%d rc=%s", port, rc)
    except Exception:
        log.warning("link.firewall_rule_failed", exc_info=True)


def tailscale_address(addrs: List[str]) -> str:
    """This PC's Tailscale address (100.64.0.0/10), if Tailscale is running — the phone uses it
    to reach SAINT from anywhere."""
    for a in addrs:
        parts = a.split(".")
        if len(parts) == 4 and parts[0] == "100" and parts[1].isdigit() and 64 <= int(parts[1]) <= 127:
            return a
    return ""


def get_link() -> LinkService:
    global _link
    with _link_lock:
        if _link is None:
            _link = LinkService()
        return _link

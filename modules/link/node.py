"""
modules/link/node.py

A SAINT Link node: listens on IP:port, dials the devices you paired with, keeps
those connections alive and turns messages into calls.

    - Server:   accepts a TCP connection and works out whether it is a paired
                device reconnecting (Noise IK) or a new device holding the
                pairing code (Noise XXpsk3). Anyone else is dropped without an
                answer.
    - Client:   ``connect()`` a known peer, ``pair()`` a new one.
    - Sessions: one encrypted, authenticated channel per peer. Requests carry an
                id and get a response; events don't. Handlers run on a small
                thread pool so a slow one (waiting for the owner to approve
                something) never blocks the connection.
    - Every handler checks what the peer is allowed to do with
      ``ctx.require(permission)``: "allow" runs, "deny" refuses, "ask" waits for
      the owner (modules/link/approvals.py).

The node knows nothing about SAINT's agent, memory or automations; those are
handlers registered by modules/link/service.py.
"""

import concurrent.futures
import itertools
import logging
import socket
import threading
import time
from typing import Callable, Dict, List, Optional

from modules.link import wire
from modules.link.approvals import ApprovalQueue
from modules.link.identity import (ALLOW, ASK, COLLABORATOR, DENY, OWN, Identity, Peer, PeerStore,
                                   PairingManager, device_id_for, decode_code, psk_for)
from modules.link.wire import Channel, LinkClosed, LinkError, ServerHandshake

log = logging.getLogger("saint.link")

KEEPALIVE_SEC = 15.0
DEAD_AFTER_SEC = 50.0
DEFAULT_PORT = 8765


def lan_addresses() -> List[str]:
    """This machine's IPv4 addresses a phone on the same network could use,
    most likely first (home networks, then VPN ranges like Tailscale's)."""
    found: List[str] = []
    try:
        import psutil
        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.", "0.")):
                    found.append(a.address)
    except Exception:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 1))
                found.append(s.getsockname()[0])
        except OSError:
            pass

    def rank(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31:
            return 2
        if ip.startswith("100.") and 64 <= int(ip.split(".")[1]) <= 127:
            return 3                      # Tailscale / CGNAT
        return 4
    return sorted(dict.fromkeys(found), key=rank)


# ---------------------------------------------------------------------- #
# Sessions
# ---------------------------------------------------------------------- #
class _Pending:
    __slots__ = ("event", "response")

    def __init__(self):
        self.event = threading.Event()
        self.response: Optional[dict] = None


class Session:
    _ids = itertools.count(1)

    def __init__(self, node: "LinkNode", peer: Peer, channel: Channel, incoming: bool):
        self.node = node
        self.id = next(Session._ids)
        self.peer = peer
        self.channel = channel
        self.incoming = incoming
        self.created = time.time()
        self.last_rx = time.time()
        self._pending: Dict[int, _Pending] = {}
        self._req_ids = itertools.count(1)
        self._lock = threading.Lock()
        self.closed = False

    # -- sending ---------------------------------------------------------
    def request(self, type_: str, data: Optional[dict] = None, timeout: float = 20.0) -> dict:
        if self.closed:
            raise LinkClosed()
        rid = next(self._req_ids)
        p = _Pending()
        with self._lock:
            self._pending[rid] = p
        try:
            self.channel.send_message({"t": type_, "id": rid, "d": data or {}})
            if not p.event.wait(timeout):
                raise LinkError(f"{self.peer.name} didn't answer in time", "timeout")
        finally:
            with self._lock:
                self._pending.pop(rid, None)
        resp = p.response or {}
        if resp.get("closed"):
            raise LinkClosed()
        if not resp.get("ok"):
            err = resp.get("e") or {}
            raise LinkError(err.get("msg", "the request failed"), err.get("code", "error"))
        return resp.get("d") or {}

    def notify(self, type_: str, data: Optional[dict] = None):
        if self.closed:
            raise LinkClosed()
        self.channel.send_message({"t": type_, "d": data or {}})

    def _respond(self, rid, ok: bool, data=None, err: Optional[LinkError] = None):
        if rid is None or self.closed:
            return
        msg = {"re": rid, "ok": ok}
        if ok:
            msg["d"] = data or {}
        else:
            msg["e"] = {"code": err.code if err else "error", "msg": str(err) if err else "the request failed"}
        try:
            self.channel.send_message(msg)
        except LinkError:
            pass

    # -- receiving -------------------------------------------------------
    def run(self):
        try:
            while not self.closed:
                kind, payload = self.channel.recv()
                self.last_rx = time.time()
                if kind == "chunk":
                    self.node.files.on_chunk(self, *payload)
                else:
                    self._dispatch(payload)
        except LinkError as e:
            if e.code not in ("closed",):
                log.info("link.session_error peer=%s %s", self.peer.name, e)
        except Exception:
            log.exception("link.session_crashed peer=%s", self.peer.name)
        finally:
            self.close()

    def _dispatch(self, msg: dict):
        if "re" in msg:
            with self._lock:
                p = self._pending.get(msg["re"])
            if p is not None:
                p.response = msg
                p.event.set()
            return
        t = msg.get("t")
        if not isinstance(t, str) or t in ("ping", "pong"):
            return
        data = msg.get("d") if isinstance(msg.get("d"), dict) else {}
        try:
            self.node._executor.submit(self.node._run_handler, self, t, data, msg.get("id"))
        except RuntimeError:                # pool shut down
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.channel.close()
        with self._lock:
            for p in self._pending.values():
                p.response = {"closed": True}
                p.event.set()
        self.node._session_closed(self)


class CallContext:
    """What a handler gets besides the request data."""

    def __init__(self, node: "LinkNode", session: Session, peer: Peer):
        self.node, self.session, self.peer = node, session, peer

    def permission(self, perm: str) -> str:
        return self.peer.permission(perm)

    def require(self, perm: str, describe: str = "", timeout: Optional[float] = None):
        """Return when this peer may do ``perm`` (asking the owner if the
        permission is "ask"); raise LinkError("denied") otherwise."""
        policy = self.peer.permission(perm)
        if policy == ALLOW:
            return
        if policy == ASK:
            if self.node.approvals.request(self.peer.id, self.peer.name, describe or perm,
                                           timeout or self.node.approval_timeout):
                return
            raise LinkError(f"{self.node.identity.name} said no", "denied")
        raise LinkError(f"{self.peer.name} isn't allowed to do that here", "denied")


# ---------------------------------------------------------------------- #
# The node
# ---------------------------------------------------------------------- #
class LinkNode:
    def __init__(self, identity: Identity, peers: PeerStore, pairing: Optional[PairingManager] = None,
                 approvals: Optional[ApprovalQueue] = None, emit: Optional[Callable] = None,
                 inbox_dir: Optional[Callable[[], str]] = None, max_file_mb: Callable[[], float] = lambda: 1024):
        from modules.link.files import FileTransfers
        self.identity = identity
        self.peers = peers
        self.pairing = pairing or PairingManager()
        self._emit = emit or (lambda name, payload: None)
        self.approvals = approvals or ApprovalQueue(emit=self._emit)
        self.approval_timeout = 60.0
        self.handlers: Dict[str, Callable] = {}
        self.files = FileTransfers(self, inbox_dir, max_file_mb)
        self._sessions: Dict[str, List[Session]] = {}
        self._lock = threading.RLock()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=12, thread_name_prefix="link")
        self._server: Optional[socket.socket] = None
        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()
        self.port = 0
        self.address_hints: Dict[str, tuple] = {}       # peer id -> (host, port) from discovery
        self._backoff: Dict[str, tuple] = {}            # peer id -> (next try, delay)
        self.auto_connect = True

    # ------------------------------------------------------------------ #
    def emit(self, name: str, payload: dict):
        try:
            self._emit(name, payload)
        except Exception:
            log.exception("link.emit_failed %s", name)

    def register(self, type_: str, fn: Callable):
        self.handlers[type_] = fn

    def _hello(self, **extra) -> dict:
        hello = self.identity.hello()
        if self.port:
            hello["port"] = self.port
        hello.update(extra)
        return hello

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> int:
        if self._server is not None:
            return self.port
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(16)
        self._server = srv
        self.port = srv.getsockname()[1]
        self._stop.clear()
        for target, name in ((self._accept_loop, "link-accept"), (self._keepalive_loop, "link-keepalive"),
                             (self._connector_loop, "link-connector")):
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()
            self._threads.append(t)
        log.info("link.listening %s:%d device=%s", host, self.port, self.identity.device_id)
        return self.port

    def stop(self):
        self._stop.set()
        srv, self._server = self._server, None
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        for s in self.all_sessions():
            s.close()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=12, thread_name_prefix="link")
        self._threads = []
        self.port = 0

    @property
    def running(self) -> bool:
        return self._server is not None

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #
    def all_sessions(self) -> List[Session]:
        with self._lock:
            return [s for lst in self._sessions.values() for s in lst if not s.closed]

    def session_for(self, peer_id: str) -> Optional[Session]:
        with self._lock:
            live = [s for s in self._sessions.get(peer_id, []) if not s.closed]
        return max(live, key=lambda s: s.created) if live else None

    def connected_ids(self) -> List[str]:
        with self._lock:
            return [pid for pid, lst in self._sessions.items() if any(not s.closed for s in lst)]

    def _adopt(self, peer: Peer, channel: Channel, incoming: bool, ip: str = "", hello: Optional[dict] = None) -> Optional[Session]:
        hello = hello or {}
        with self._lock:
            live = [s for s in self._sessions.get(peer.id, []) if not s.closed]
            for s in [s for s in live if s.incoming == incoming]:
                s.close()                     # the same device reconnecting: the new connection replaces the old
            other = [s for s in live if s.incoming != incoming and not s.closed]
            if other:
                # Both devices dialled at once: keep the connection the lower id started, on both sides.
                keep_incoming = not (self.identity.device_id < peer.id)
                if incoming != keep_incoming:
                    channel.close()
                    return other[0]
                for s in other:
                    s.close()
            session = Session(self, peer, channel, incoming)
            self._sessions.setdefault(peer.id, []).append(session)
        updates = {"last_seen": time.time()}
        if hello.get("name"):
            updates["name"] = str(hello["name"])[:40]
        if hello.get("platform"):
            updates["platform"] = str(hello["platform"])[:16]
        if incoming and ip:
            updates["host"] = ip
            if isinstance(hello.get("port"), int) and 0 < hello["port"] < 65536:
                updates["port"] = hello["port"]
        self.peers.update(peer.id, **updates)
        threading.Thread(target=session.run, daemon=True, name=f"link-session-{peer.name}").start()
        log.info("link.connected peer=%s (%s) incoming=%s", peer.name, peer.id, incoming)
        self.emit("link.connected", {"peer_id": peer.id, "name": peer.name, "incoming": incoming})
        return session

    def _session_closed(self, session: Session):
        with self._lock:
            lst = self._sessions.get(session.peer.id, [])
            if session in lst:
                lst.remove(session)
            still = any(not s.closed for s in lst)
        if not still:
            log.info("link.disconnected peer=%s", session.peer.name)
            self.emit("link.disconnected", {"peer_id": session.peer.id, "name": session.peer.name})

    def close_peer(self, peer_id: str):
        for s in list(self._sessions.get(peer_id, [])):
            s.close()

    # ------------------------------------------------------------------ #
    # Server side
    # ------------------------------------------------------------------ #
    def _accept_loop(self):
        while not self._stop.is_set():
            srv = self._server
            if srv is None:
                return
            try:
                conn, addr = srv.accept()
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self._serve, args=(conn, addr[0]), daemon=True, name="link-serve").start()

    def _serve(self, conn: socket.socket, ip: str):
        try:
            if self.pairing.too_many_failures(ip):
                conn.close()
                return
            hs = ServerHandshake(conn, self.identity.private_key)
            mode = hs.begin()
            if mode == wire.MODE_RECONNECT:
                self._serve_reconnect(hs, conn, ip)
            else:
                self._serve_pair(hs, conn, ip)
        except LinkError as e:
            log.info("link.refused ip=%s %s", ip, e)
            try:
                conn.close()
            except OSError:
                pass
        except Exception:
            log.exception("link.serve_failed ip=%s", ip)
            try:
                conn.close()
            except OSError:
                pass

    def _serve_reconnect(self, hs: ServerHandshake, conn: socket.socket, ip: str):
        found: dict = {}

        def authorize(remote_static: bytes, hello: dict) -> bool:
            peer = self.peers.by_key(remote_static)
            if peer is None or hello.get("id") != peer.id:
                self.pairing.note_failure(ip)         # an unknown device probing
                return False
            found["peer"] = peer
            return True

        channel, hello = hs.finish(self._hello(), authorize=authorize)
        self._adopt(found["peer"], channel, True, ip, hello)

    def _serve_pair(self, hs: ServerHandshake, conn: socket.socket, ip: str):
        offer = self.pairing.current
        if offer is None:
            self.pairing.note_failure(ip)
            raise LinkError("no pairing is open", "no_offer")
        try:
            channel, hello = hs.finish(self._hello(), psk=offer.psk)
        except LinkError as e:
            if e.code == "handshake":                 # wrong code
                self.pairing.note_failure(ip, offer)
            raise
        remote_static = channel.remote_static
        if hello.get("id") != device_id_for(remote_static) or hello.get("role") != offer.role:
            self.pairing.note_failure(ip, offer)
            channel.close()
            raise LinkError("pairing details didn't match", "mismatch")
        existing = self.peers.get(hello["id"])
        peer = Peer(id=hello["id"], name=str(hello.get("name") or "Device")[:40], public_key=remote_static.hex(),
                    role=offer.role, platform=str(hello.get("platform") or "")[:16], host=ip,
                    port=int(hello["port"]) if isinstance(hello.get("port"), int) else 0)
        if existing is not None:
            peer.nicknames, peer.perms, peer.added = existing.nicknames, existing.perms, existing.added
        if offer.label:
            peer.name = offer.label[:40]
        self.peers.save(peer)
        self.pairing.consume(offer)
        channel.send_message({"t": "pair.ok", "d": {"hello": self._hello(), "role": offer.role}})
        log.info("link.paired peer=%s role=%s", peer.name, peer.role)
        self.emit("link.paired", peer.public())
        self._adopt(peer, channel, True, ip, hello)

    # ------------------------------------------------------------------ #
    # Client side
    # ------------------------------------------------------------------ #
    def _dial(self, host: str, port: int, timeout: float) -> socket.socket:
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except (OSError, socket.timeout) as e:
            raise LinkError(f"couldn't reach {host}:{port} ({e})", "unreachable") from e
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        return sock

    def connect(self, peer: Peer, timeout: float = 6.0) -> Session:
        existing = self.session_for(peer.id)
        if existing is not None:
            return existing
        host, port = self.address_hints.get(peer.id) or (peer.host, peer.port)
        if not host or not port:
            raise LinkError(f"I don't know where {peer.name} is yet", "no_address")
        sock = self._dial(host, port, timeout)
        try:
            channel, hello = wire.client_handshake(sock, self.identity.private_key, self._hello(),
                                                   remote_static=peer.key, timeout=timeout + 4)
        except LinkError:
            sock.close()
            raise
        if hello.get("id") != peer.id:
            channel.close()
            raise LinkError("that isn't the device I paired with", "identity")
        self.peers.update(peer.id, host=host, port=port)
        session = self._adopt(peer, channel, False, "", hello)
        return session or self.session_for(peer.id)

    def pair(self, host: str, port: int, code: str, role: str = OWN, timeout: float = 10.0) -> Peer:
        """Pair with the device at host:port using the code (or token bytes) it shows."""
        token = code if isinstance(code, (bytes, bytearray)) else decode_code(code)
        sock = self._dial(host, port, timeout)
        try:
            channel, _ = wire.client_handshake(sock, self.identity.private_key, self._hello(role=role),
                                               psk=psk_for(bytes(token)), timeout=timeout)
            sock.settimeout(timeout)
            try:
                kind, msg = channel.recv()
            except LinkError as e:
                raise LinkError("Pairing failed: wrong code, or the other device closed the pairing window.",
                                "pair_failed") from e
            sock.settimeout(None)
            body = msg.get("d") if isinstance(msg, dict) else None
            if kind != "json" or msg.get("t") != "pair.ok" or not isinstance(body, dict):
                raise LinkError("Pairing failed: unexpected answer.", "pair_failed")
            shello = body.get("hello") or {}
            remote_static = channel.remote_static
            if shello.get("id") != device_id_for(remote_static):
                raise LinkError("Pairing failed: the device's identity didn't check out.", "identity")
        except LinkError:
            sock.close()
            raise
        existing = self.peers.get(shello["id"])
        peer = Peer(id=shello["id"], name=str(shello.get("name") or "Device")[:40], public_key=remote_static.hex(),
                    role=role, platform=str(shello.get("platform") or "")[:16], host=host,
                    port=int(shello["port"]) if isinstance(shello.get("port"), int) else port)
        if existing is not None:
            peer.nicknames, peer.perms, peer.added = existing.nicknames, existing.perms, existing.added
        self.peers.save(peer)
        log.info("link.paired peer=%s role=%s (we scanned)", peer.name, peer.role)
        self.emit("link.paired", peer.public())
        self._adopt(peer, channel, False, "", shello)
        return peer

    # ------------------------------------------------------------------ #
    # Calls
    # ------------------------------------------------------------------ #
    def _run_handler(self, session: Session, type_: str, data: dict, rid):
        peer = self.peers.get(session.peer.id)
        if peer is None:                                  # unpaired while connected
            session.close()
            return
        session.peer = peer
        handler = self.handlers.get(type_)
        try:
            if handler is None:
                raise LinkError(f"unknown request '{type_}'", "unknown")
            result = handler(CallContext(self, session, peer), data)
            session._respond(rid, True, result)
        except LinkError as e:
            session._respond(rid, False, err=e)
        except Exception:
            log.exception("link.handler_failed %s", type_)
            session._respond(rid, False, err=LinkError("something went wrong on the other device", "internal"))

    def request(self, peer_id: str, type_: str, data: Optional[dict] = None, timeout: float = 20.0) -> dict:
        session = self.session_for(peer_id)
        if session is None:
            peer = self.peers.get(peer_id)
            if peer is None:
                raise LinkError("that device isn't paired", "unknown_peer")
            session = self.connect(peer)
        return session.request(type_, data, timeout)

    def notify(self, peer_id: str, type_: str, data: Optional[dict] = None):
        session = self.session_for(peer_id)
        if session is None:
            raise LinkError("that device isn't connected", "offline")
        session.notify(type_, data)

    def broadcast(self, type_: str, data: Optional[dict] = None, perm: Optional[str] = None) -> int:
        """An event to every connected peer that may receive it. Returns how many."""
        n = 0
        for s in self.all_sessions():
            peer = self.peers.get(s.peer.id)
            if peer is None or (perm and peer.permission(perm) != ALLOW):
                continue
            try:
                s.notify(type_, data)
                n += 1
            except LinkError:
                pass
        return n

    def send_file(self, peer_id: str, path: str, progress: Optional[Callable] = None) -> dict:
        return self.files.send(peer_id, path, progress)

    # ------------------------------------------------------------------ #
    # Background loops
    # ------------------------------------------------------------------ #
    def _keepalive_loop(self):
        while not self._stop.wait(KEEPALIVE_SEC):
            now = time.time()
            for s in self.all_sessions():
                if now - s.last_rx > DEAD_AFTER_SEC:
                    log.info("link.timed_out peer=%s", s.peer.name)
                    s.close()
                    continue
                try:
                    s.notify("ping")
                except LinkError:
                    s.close()

    def _connector_loop(self):
        while not self._stop.wait(4.0):
            if not self.auto_connect:
                continue
            now = time.time()
            for peer in self.peers.all():
                if not peer.auto_connect or self.session_for(peer.id) is not None:
                    self._backoff.pop(peer.id, None)
                    continue
                host, port = self.address_hints.get(peer.id) or (peer.host, peer.port)
                if not host or not port:
                    continue
                nxt, delay = self._backoff.get(peer.id, (0.0, 4.0))
                if now < nxt:
                    continue
                try:
                    self.connect(peer, timeout=4.0)
                    self._backoff.pop(peer.id, None)
                except LinkError as e:
                    self._backoff[peer.id] = (now + delay, min(delay * 2, 300.0))
                    log.debug("link.connect_failed peer=%s %s", peer.name, e)
                except Exception:
                    log.exception("link.connector_failed peer=%s", peer.name)
                    self._backoff[peer.id] = (now + delay, min(delay * 2, 300.0))

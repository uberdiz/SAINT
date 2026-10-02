"""
modules/link/discovery.py

Finding your other SAINTs on the same network without typing an address.

    mDNS / Bonjour   advertises ``_saint._tcp`` (the iPhone app browses for it) and
                     browses for other SAINTs. Needs the optional ``zeroconf``
                     package; without it this part is skipped.
    UDP beacon       a small broadcast on port 8766 every few seconds — it works
                     between PCs with nothing extra installed.

The beacon also says when that SAINT has a pairing window open, so another PC
can pair by typing only the short code: it asks the network ("probe") and
tries the SAINTs that answered with a window open.

Both only say "a device called <name> with id <id> is at <ip>:<port>". That is
a hint to *where to knock*: the connection itself is authenticated by the
paired device's key (modules/link/noise.py), so a forged beacon can at worst
cause a failed connection attempt. Neither carries any data, and both are off
when SAINT Link is off or ``link.discoverable`` is false. You can always type
an IP:port instead.
"""

import json
import logging
import socket
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("saint.link.discovery")

BEACON_PORT = 8766
BEACON_MAGIC = b"SAINTLINK1"
SERVICE_TYPE = "_saint._tcp.local."


class Discovery:
    NEARBY_SEC = 30.0

    def __init__(self, device_id: str, name: str, on_found: Callable[[str, str, int], None],
                 pairing_open: Optional[Callable[[], str]] = None):
        self.device_id, self.name, self.on_found = device_id, name, on_found
        self.pairing_open = pairing_open or (lambda: "")      # "" or the open offer's role
        self._stop = threading.Event()
        self._threads = []
        self._zc = None
        self._info = None
        self._browser = None
        self._tx: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self.port = 0
        self.backends = []
        self.seen: dict = {}            # device id -> {"name", "host", "port", "pairing", "at"}

    def nearby(self, pairing_only: bool = False) -> list:
        """SAINTs heard from in the last half minute (paired or not), newest first."""
        now = time.time()
        with self._lock:
            out = [dict(v, id=k) for k, v in self.seen.items() if now - v["at"] < self.NEARBY_SEC]
        if pairing_only:
            out = [d for d in out if d.get("pairing")]
        return sorted(out, key=lambda d: -d["at"])

    def _note(self, peer_id: str, host: str, port: int, name: str = "", pairing: str = ""):
        with self._lock:
            self.seen[peer_id] = {"name": name or self.seen.get(peer_id, {}).get("name", ""), "host": host,
                                  "port": port, "pairing": pairing, "at": time.time()}
        self.on_found(peer_id, host, port)

    # ------------------------------------------------------------------ #
    def start(self, port: int, name: Optional[str] = None, udp: bool = True, mdns: bool = True):
        self.port = port
        if name:
            self.name = name
        self._stop.clear()
        if mdns:
            self._start_mdns()
        if udp:
            self._start_udp()
        log.info("link.discovery started backends=%s", self.backends or "none")

    def stop(self):
        self._stop.set()
        try:
            if self._zc is not None:
                if self._info is not None:
                    self._zc.unregister_service(self._info)
                self._zc.close()
        except Exception:
            log.debug("link.discovery.mdns_stop_failed", exc_info=True)
        self._zc = self._info = self._browser = None
        self._threads = []
        self.backends = []

    # ------------------------------------------------------------------ #
    # mDNS
    # ------------------------------------------------------------------ #
    def _start_mdns(self):
        try:
            from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf
        except Exception:
            return
        try:
            from modules.link.node import lan_addresses
            addrs = [socket.inet_aton(a) for a in lan_addresses()]
            if not addrs:
                return
            self._zc = Zeroconf()
            label = f"{self.name[:30]}-{self.device_id[:6]}"
            self._info = ServiceInfo(SERVICE_TYPE, f"{label}.{SERVICE_TYPE}", addresses=addrs, port=self.port,
                                     properties={"id": self.device_id, "name": self.name, "v": "1"})
            self._zc.register_service(self._info)
            outer = self

            class Listener:
                def _handle(self, zc, type_, name):
                    try:
                        info = zc.get_service_info(type_, name, timeout=2000)
                    except Exception:
                        return
                    if info is None or not info.addresses:
                        return
                    props = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                             for k, v in (info.properties or {}).items()}
                    peer_id = props.get("id", "")
                    if peer_id and peer_id != outer.device_id:
                        outer._note(peer_id, socket.inet_ntoa(info.addresses[0]), info.port, props.get("name", ""))

                def add_service(self, zc, type_, name):
                    self._handle(zc, type_, name)

                def update_service(self, zc, type_, name):
                    self._handle(zc, type_, name)

                def remove_service(self, zc, type_, name):
                    pass

            self._browser = ServiceBrowser(self._zc, SERVICE_TYPE, Listener())
            self.backends.append("mdns")
        except Exception:
            log.warning("link.discovery.mdns_failed", exc_info=True)
            self._zc = None

    # ------------------------------------------------------------------ #
    # UDP beacon
    # ------------------------------------------------------------------ #
    def _start_udp(self):
        try:
            rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            rx.bind(("", BEACON_PORT))
            rx.settimeout(1.0)
        except OSError as e:
            log.info("link.discovery.udp_unavailable %s", e)
            return
        self.backends.append("udp")
        for target, name in ((lambda: self._listen(rx), "link-beacon-rx"), (self._announce, "link-beacon-tx")):
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()
            self._threads.append(t)

    def _listen(self, rx: socket.socket):
        try:
            while not self._stop.is_set():
                try:
                    data, addr = rx.recvfrom(1024)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not data.startswith(BEACON_MAGIC):
                    continue
                try:
                    info = json.loads(data[len(BEACON_MAGIC):].decode("utf-8"))
                except (ValueError, TypeError):
                    continue
                if not isinstance(info, dict):
                    continue
                if info.get("probe"):
                    if str(info.get("id", "")) != self.device_id:
                        self._send_beacon([addr[0]])       # someone is looking: answer now, to them
                    continue
                try:
                    peer_id, port = str(info["id"]), int(info["port"])
                except (ValueError, KeyError, TypeError):
                    continue
                if peer_id != self.device_id and 0 < port < 65536:
                    pairing = str(info.get("pair") or "")
                    self._note(peer_id, addr[0], port, str(info.get("name") or "")[:40],
                               pairing if pairing in ("own", "collaborator") else "")
        finally:
            rx.close()

    def _targets(self):
        out = {"255.255.255.255"}
        try:
            import psutil
            for addrs in psutil.net_if_addrs().values():
                for a in addrs:
                    if a.family == socket.AF_INET and getattr(a, "broadcast", None):
                        out.add(a.broadcast)
        except Exception:
            pass
        return out

    def _payload(self) -> bytes:
        info = {"id": self.device_id, "name": self.name, "port": self.port}
        try:
            role = self.pairing_open() or ""
        except Exception:
            role = ""
        if role:
            info["pair"] = role
        return BEACON_MAGIC + json.dumps(info, separators=(",", ":")).encode("utf-8")

    def _send_beacon(self, targets=None, payload: Optional[bytes] = None):
        tx = self._tx
        if tx is None:
            return
        payload = payload or self._payload()
        for target in targets or self._targets():
            try:
                tx.sendto(payload, (target, BEACON_PORT))
            except OSError:
                pass

    def announce_now(self):
        """Say we're here right away (a pairing window just opened)."""
        self._send_beacon()

    def probe(self):
        """Ask every SAINT on the network to say where it is now (they answer straight back)."""
        self._send_beacon(payload=BEACON_MAGIC + json.dumps({"probe": 1, "id": self.device_id}).encode("utf-8"))

    def _announce(self):
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._tx = tx
        try:
            self._send_beacon()
            while not self._stop.wait(6.0):
                self._send_beacon()
        finally:
            self._tx = None
            tx.close()

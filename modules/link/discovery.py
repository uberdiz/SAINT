"""
modules/link/discovery.py

Finding your other SAINTs on the same network without typing an address.

    mDNS / Bonjour   advertises ``_saint._tcp`` (the iPhone app browses for it) and
                     browses for other SAINTs. Needs the optional ``zeroconf``
                     package; without it this part is skipped.
    UDP beacon       a small broadcast on port 8766 every few seconds — it works
                     between PCs with nothing extra installed.

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
    def __init__(self, device_id: str, name: str, on_found: Callable[[str, str, int], None]):
        self.device_id, self.name, self.on_found = device_id, name, on_found
        self._stop = threading.Event()
        self._threads = []
        self._zc = None
        self._info = None
        self._browser = None
        self.port = 0
        self.backends = []

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
                        outer.on_found(peer_id, socket.inet_ntoa(info.addresses[0]), info.port)

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
                    peer_id, port = str(info["id"]), int(info["port"])
                except (ValueError, KeyError, TypeError):
                    continue
                if peer_id != self.device_id and 0 < port < 65536:
                    self.on_found(peer_id, addr[0], port)
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

    def _announce(self):
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            while not self._stop.wait(6.0):
                payload = BEACON_MAGIC + json.dumps({"id": self.device_id, "name": self.name, "port": self.port},
                                                    separators=(",", ":")).encode("utf-8")
                for target in self._targets():
                    try:
                        tx.sendto(payload, (target, BEACON_PORT))
                    except OSError:
                        pass
        finally:
            tx.close()

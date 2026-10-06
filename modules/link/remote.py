"""
modules/link/remote.py

Reaching this PC from anywhere without Tailscale: ask the home router (UPnP
IGD — on by default on most home routers) to forward SAINT Link's TCP port to
this PC, and learn the router's public address. That address is then listed
with this PC's other addresses (pairing code, hellos), so a paired phone that
was told it at home dials it when it's away — on cellular, at a friend's.

Safe to expose: every connection is a Noise handshake (modules/link/noise.py).
Reconnecting needs a paired device's private key; pairing needs the one-time
code, works once, for five minutes, and closes after ten wrong tries. Nothing
is reachable without one of those.

It can't work when the router has UPnP turned off (say so: turn it on, or
forward the port by hand) or when the "public" address is itself private —
carrier-grade NAT (many mobile / satellite / some fibre ISPs) or a second
router in front: then only a relay or a VPN such as Tailscale gets through.

Standard library only (SSDP over UDP, SOAP over HTTP).
"""

import ipaddress
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin
from xml.etree import ElementTree

log = logging.getLogger("saint.link.remote")

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH_TARGETS = ("urn:schemas-upnp-org:device:InternetGatewayDevice:2",
                  "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
                  "urn:schemas-upnp-org:service:WANIPConnection:1")
_SERVICES = ("WANIPConnection", "WANPPPConnection")
LEASE_SEC = 3600
RENEW_SEC = 1500
DESCRIPTION = "SAINT Link"


def is_public(ip: str) -> bool:
    """A globally routable IPv4 address (not home, carrier-grade NAT, loopback, link-local...)."""
    try:
        a = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    if a.version != 4:
        return False
    return a.is_global and a not in ipaddress.ip_network("100.64.0.0/10")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def control_url(description_xml: str, location: str) -> Optional[Tuple[str, str]]:
    """The WAN connection service's (control URL, service type) in a router's device description."""
    try:
        root = ElementTree.fromstring(description_xml)
    except ElementTree.ParseError:
        return None
    base = next((e.text.strip() for e in root.iter() if _local(e.tag) == "URLBase" and e.text), "") or location
    for svc in root.iter():
        if _local(svc.tag) != "service":
            continue
        fields = {_local(c.tag): (c.text or "").strip() for c in svc}
        st = fields.get("serviceType", "")
        if any(name in st for name in _SERVICES) and fields.get("controlURL"):
            return urljoin(base, fields["controlURL"]), st
    return None


def discover(timeout: float = 2.5, local_ip: str = "") -> Optional[Tuple[str, str]]:
    """Find the router's port-mapping service on this network: (control URL, service type).
    ``local_ip``: the home network address to ask from — with Tailscale, Hyper-V or virtual audio
    adapters installed, an unbound multicast leaves through the wrong one and nothing answers."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    if local_ip:
        try:
            sock.bind((local_ip, 0))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        except OSError as e:
            log.debug("link.remote.bind_failed %s %s", local_ip, e)
    sock.settimeout(0.5)
    locations = []
    try:
        for st in SEARCH_TARGETS:
            msg = (f"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
                   f"MX: 2\r\nST: {st}\r\n\r\n").encode()
            try:
                sock.sendto(msg, SSDP_ADDR)
            except OSError as e:
                log.info("link.remote.ssdp_send_failed %s", e)
                return None
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                loc = m.group(1).decode("latin-1")
                if loc not in locations:
                    locations.append(loc)
    finally:
        sock.close()
    for loc in locations:
        try:
            with urllib.request.urlopen(loc, timeout=3) as r:
                found = control_url(r.read().decode("utf-8", "replace"), loc)
        except Exception as e:
            log.debug("link.remote.description_failed %s %s", loc, e)
            continue
        if found:
            return found
    return None


def _soap(url: str, service_type: str, action: str, args: Dict[str, object], timeout: float = 4.0) -> Dict[str, str]:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    envelope = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{service_type}">{body}</u:{action}></s:Body></s:Envelope>').encode()
    req = urllib.request.Request(url, data=envelope, method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service_type}#{action}"'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            xml = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:            # UPnP errors come back as HTTP 500 with a code
        text = e.read().decode("utf-8", "replace")
        code = re.search(r"<errorCode>(\d+)</errorCode>", text)
        desc = re.search(r"<errorDescription>([^<]*)</errorDescription>", text)
        raise UPnPError(int(code.group(1)) if code else e.code, desc.group(1) if desc else str(e)) from None
    out = {}
    try:
        for el in ElementTree.fromstring(xml).iter():
            if not list(el) and el.text is not None:
                out[_local(el.tag)] = el.text.strip()
    except ElementTree.ParseError:
        pass
    return out


class UPnPError(Exception):
    def __init__(self, code: int, text: str):
        super().__init__(f"{code}: {text}")
        self.code = code


class RemoteAccess:
    """Keeps a port mapping on the router while SAINT Link runs (renewed every 25 minutes) and
    knows the public address it gives. ``status()`` says, in words, whether it worked."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._svc: Optional[Tuple[str, str]] = None
        self._port = 0
        self._state = {"state": "off", "address": "", "port": 0, "message": ""}

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def public_address(self) -> str:
        st = self.status()
        return st["address"] if st["state"] == "open" else ""

    def _set(self, state: str, message: str, address: str = "", port: int = 0) -> dict:
        with self._lock:
            self._state = {"state": state, "address": address, "port": port, "message": message}
            return dict(self._state)

    # ------------------------------------------------------------------ #
    def open(self, port: int, internal_ip: str) -> dict:
        """Map ``port`` (TCP) on the router to this PC, once. Returns the new status."""
        self._set("working", "Asking your router to let SAINT through…", port=port)
        svc = discover(local_ip=internal_ip)
        if svc is None:
            return self._set("unavailable",
                             "Your router didn't answer a UPnP request. Turn on UPnP in the router's settings, or "
                             f"forward TCP port {port} to this PC ({internal_ip}) by hand — or use Tailscale.")
        url, st = svc
        mapping = {"NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": "TCP", "NewInternalPort": port,
                   "NewInternalClient": internal_ip, "NewEnabled": 1, "NewPortMappingDescription": DESCRIPTION,
                   "NewLeaseDuration": LEASE_SEC}
        try:
            try:
                _soap(url, st, "AddPortMapping", mapping)
            except UPnPError as e:
                if e.code != 725:                   # OnlyPermanentLeasesSupported
                    raise
                _soap(url, st, "AddPortMapping", dict(mapping, NewLeaseDuration=0))
            external = _soap(url, st, "GetExternalIPAddress", {}).get("NewExternalIPAddress", "")
        except UPnPError as e:
            why = ("another device already uses that port on your router — change SAINT Link's port in Settings"
                   if e.code == 718 else f"your router refused ({e})")
            return self._set("error", f"Couldn't open the port: {why}.")
        except Exception as e:
            log.info("link.remote.upnp_failed %s", e)
            return self._set("error", f"Couldn't talk to your router: {e}")
        with self._lock:
            self._svc, self._port = svc, port
        if not is_public(external):
            log.info("link.remote.not_public external=%r", external)
            return self._set("blocked",
                             f"Your router's own internet address ({external or 'unknown'}) isn't a public one — "
                             "your provider uses carrier-grade NAT, or there's a second router in front. Devices "
                             "away from home can't reach this PC directly; Tailscale still works.")
        log.info("link.remote.open %s:%d", external, port)
        return self._set("open", f"Reachable from anywhere at {external}:{port}. Your phone learns this address "
                                 f"the next time it connects at home (or pair it again).", external, port)

    def close(self):
        with self._lock:
            svc, port = self._svc, self._port
            self._svc, self._port = None, 0
        if svc and port:
            try:
                _soap(svc[0], svc[1], "DeletePortMapping",
                      {"NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": "TCP"})
            except Exception as e:
                log.debug("link.remote.delete_failed %s", e)
        self._set("off", "")

    # ------------------------------------------------------------------ #
    def start(self, port: int, internal_ip: str, on_change=None):
        """Open now and keep it open (renewing the lease, noticing a new public address)."""
        self.stop()
        self._stop.clear()

        def run():
            last = None
            while True:
                st = self.open(port, internal_ip)
                if on_change and (st["state"], st["address"]) != last:
                    last = (st["state"], st["address"])
                    try:
                        on_change(st)
                    except Exception:
                        log.exception("link.remote.on_change_failed")
                if self._stop.wait(RENEW_SEC if st["state"] == "open" else 300):
                    return
        self._thread = threading.Thread(target=run, daemon=True, name="link-remote")
        self._thread.start()

    def stop(self):
        """Stop renewing and take the mapping off the router (a few seconds at most)."""
        self._stop.set()
        if self._thread is not None:
            self._thread = None
            self.close()

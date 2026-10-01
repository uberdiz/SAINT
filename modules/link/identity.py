"""
modules/link/identity.py

Who this device is, who it has paired with, and what each of them may do.

Identity
    A long-term X25519 key pair. The device id is the first 16 hex characters of
    SHA-256(public key), so an id can't be claimed without holding its key. The
    private key lives in the Windows Credential Manager (like the Spotify
    login); if there is no keyring it falls back to a file in data/link/ that
    is git-ignored with the rest of ``data/``.

Peers (data/link/peers.json)
    Every paired device, with the role you gave it and a permission per feature:

        role "own"          another device of yours (phone, laptop). Sync, chat
                            and files on by default.
        role "collaborator" someone else's SAINT (a friend's PC). Almost
                            everything off; each automation you allow is a
                            separate switch, and "ask" means SAINT asks you
                            aloud (or on screen) every single time.

    A permission is "allow", "ask" or "deny". Revoking a peer removes it and
    its key: it can't reconnect.

Pairing offers
    A one-time 128-bit token, valid for a few minutes, shown as a QR code
    (``saint://pair?...``) or as a code to type. It is the pre-shared key of the
    pairing handshake (modules/link/noise.py) — a wrong guess never gets far,
    and after a handful of failed attempts the offer is cancelled.
"""

import base64
import hashlib
import json
import logging
import os
import socket
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from core.paths import data_path
from modules.link import crypto

log = logging.getLogger("saint.link")

ALLOW, ASK, DENY = "allow", "ask", "deny"
OWN, COLLABORATOR = "own", "collaborator"

# Every permission a peer can hold. "auto.*" are the remote automations
# (modules/link/automations.py); the rest are built-in features.
PERMISSIONS = {
    "sync": "Share what I learn (memories, learned skills, aliases, scenes, reminders)",
    "chat": "Talk to my SAINT and control this device",
    "status": "See what's playing and whether I'm online",
    "files": "Send me files",
    "notify": "Show me messages and notifications",
    "context": "Share what I'm doing right now (so answers stay in context)",
    "share": "Share individual things with me (a fact, a skill, a scene)",
    "auto.send_prompt": "Type a prompt into an AI app on this PC (Claude, ChatGPT, ...)",
    "auto.message": "Send me a message SAINT reads out",
    "auto.open_url": "Open a link on this PC",
    "auto.run_scene": "Run one of my shared scenes",
    "auto.play_music": "Play music on my Spotify",
    "auto.ask": "Ask my SAINT a question",
}

ROLE_DEFAULTS = {
    OWN: {"sync": ALLOW, "chat": ALLOW, "status": ALLOW, "files": ALLOW, "notify": ALLOW, "context": ALLOW,
          "share": ALLOW, "auto.send_prompt": ALLOW, "auto.message": ALLOW, "auto.open_url": ALLOW,
          "auto.run_scene": ALLOW, "auto.play_music": ALLOW, "auto.ask": ALLOW},
    COLLABORATOR: {"sync": DENY, "chat": DENY, "status": ASK, "files": ALLOW, "notify": ALLOW, "context": DENY,
                   "share": ALLOW, "auto.send_prompt": ASK, "auto.message": ALLOW, "auto.open_url": ASK,
                   "auto.run_scene": DENY, "auto.play_music": ASK, "auto.ask": ASK},
}


def device_id_for(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()[:16]


# ---------------------------------------------------------------------- #
# Peers
# ---------------------------------------------------------------------- #
@dataclass
class Peer:
    id: str
    name: str
    public_key: str                     # hex
    role: str = OWN
    platform: str = ""
    nicknames: List[str] = field(default_factory=list)
    host: str = ""
    port: int = 0
    perms: Dict[str, str] = field(default_factory=dict)
    added: float = field(default_factory=time.time)
    last_seen: float = 0.0
    auto_connect: bool = True

    @property
    def key(self) -> bytes:
        return bytes.fromhex(self.public_key)

    def permission(self, perm: str) -> str:
        if perm in self.perms:
            return self.perms[perm]
        return ROLE_DEFAULTS.get(self.role, ROLE_DEFAULTS[COLLABORATOR]).get(perm, DENY)

    def names(self) -> List[str]:
        """Everything this peer can be called by voice ("Gian", "Gian's PC", "gian pc")."""
        base = [self.name] + list(self.nicknames)
        out = []
        for n in base:
            n = n.strip().lower()
            if n:
                out += [n, n + "'s pc", n + "'s phone", n + "'s computer", n + " pc"]
        return list(dict.fromkeys(out))

    def public(self) -> dict:
        d = asdict(self)
        d["perms"] = {p: self.permission(p) for p in PERMISSIONS}
        return d


class PeerStore:
    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.RLock()

    @property
    def path(self) -> str:
        return self._path or str(data_path("link", "peers.json"))

    def all(self) -> List[Peer]:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, ValueError):
                return []
        known = Peer.__dataclass_fields__
        out = []
        for p in raw if isinstance(raw, list) else []:
            if isinstance(p, dict) and p.get("id") and p.get("public_key"):
                out.append(Peer(**{k: v for k, v in p.items() if k in known}))
        return out

    def _write(self, peers: List[Peer]):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([asdict(p) for p in peers], f, indent=2)
        os.replace(tmp, self.path)

    def get(self, peer_id: str) -> Optional[Peer]:
        return next((p for p in self.all() if p.id == peer_id), None)

    def by_key(self, public_key: bytes) -> Optional[Peer]:
        return self.get(device_id_for(public_key))

    def save(self, peer: Peer) -> Peer:
        with self._lock:
            peers = [p for p in self.all() if p.id != peer.id]
            peers.append(peer)
            self._write(peers)
        return peer

    def update(self, peer_id: str, **fields) -> Optional[Peer]:
        with self._lock:
            peer = self.get(peer_id)
            if peer is None:
                return None
            for k, v in fields.items():
                if k == "perms" and isinstance(v, dict):
                    merged = dict(peer.perms)
                    merged.update({p: s for p, s in v.items() if p in PERMISSIONS and s in (ALLOW, ASK, DENY)})
                    peer.perms = merged
                elif k in Peer.__dataclass_fields__ and k not in ("id", "public_key"):
                    setattr(peer, k, v)
            self.save(peer)
            return peer

    def remove(self, peer_id: str) -> bool:
        with self._lock:
            peers = self.all()
            rest = [p for p in peers if p.id != peer_id]
            if len(rest) == len(peers):
                return False
            self._write(rest)
            return True

    def find_by_name(self, spoken: str) -> Optional[Peer]:
        """"gian", "gian's pc", "my phone" -> the peer it means (None if unsure)."""
        q = " ".join((spoken or "").lower().replace("’", "'").split())
        q = q.removeprefix("my ").strip()
        if not q:
            return None
        peers = self.all()
        exact = [p for p in peers if q in p.names() or q == p.name.lower()]
        if len(exact) == 1:
            return exact[0]
        if not exact and q in ("phone", "iphone", "ios"):
            phones = [p for p in peers if p.platform in ("ios", "android")]
            if len(phones) == 1:
                return phones[0]
        if not exact and q in ("pc", "computer", "laptop", "desktop"):
            pcs = [p for p in peers if p.platform in ("windows", "mac", "linux") and p.role == OWN]
            if len(pcs) == 1:
                return pcs[0]
        return None            # nothing matched, or more than one did: don't guess


# ---------------------------------------------------------------------- #
# This device
# ---------------------------------------------------------------------- #
class Identity:
    """The device's key pair, id and display name."""

    KEYRING_SERVICE, KEYRING_USER = "SAINT", "link-private-key"

    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.Lock()
        self._data: Optional[dict] = None
        self._private: Optional[bytes] = None

    @property
    def path(self) -> str:
        return self._path or str(data_path("link", "identity.json"))

    # ------------------------------------------------------------------ #
    def _load(self):
        with self._lock:
            if self._data is not None:
                return
            data = {}
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            private = self._read_private(data)
            if not data.get("public_key") or private is None:
                private, public = crypto.generate_keypair()
                data = {"public_key": public.hex(), "created": time.time(),
                        "name": data.get("name") or socket.gethostname() or "SAINT PC"}
                data["stored_in"] = self._store_private(private, data)
                self._save(data)
            data.setdefault("name", socket.gethostname() or "SAINT PC")
            self._data, self._private = data, private

    def _read_private(self, data: dict) -> Optional[bytes]:
        if data.get("private_key"):
            return bytes.fromhex(data["private_key"])
        try:
            import keyring
            raw = keyring.get_password(self.KEYRING_SERVICE, self.KEYRING_USER)
            if raw:
                key = bytes.fromhex(raw)
                if len(key) == 32 and data.get("public_key") and crypto.public_key(key).hex() == data["public_key"]:
                    return key
        except Exception:
            log.debug("link.keyring_read_failed", exc_info=True)
        return None

    def _store_private(self, private: bytes, data: dict) -> str:
        try:
            import keyring
            keyring.set_password(self.KEYRING_SERVICE, self.KEYRING_USER, private.hex())
            return "keyring"
        except Exception:
            log.warning("link.keyring_unavailable: keeping the device key in data/link/identity.json")
            data["private_key"] = private.hex()
            return "file"

    def _save(self, data: dict):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ #
    @property
    def private_key(self) -> bytes:
        self._load()
        return self._private

    @property
    def public_key(self) -> bytes:
        self._load()
        return bytes.fromhex(self._data["public_key"])

    @property
    def device_id(self) -> str:
        return device_id_for(self.public_key)

    @property
    def name(self) -> str:
        self._load()
        return self._data.get("name") or "SAINT PC"

    def set_name(self, name: str):
        self._load()
        name = " ".join((name or "").split())[:40]
        if name:
            self._data["name"] = name
            self._save({k: v for k, v in self._data.items()})

    @property
    def platform(self) -> str:
        import sys
        return {"win32": "windows", "darwin": "mac"}.get(sys.platform, "linux")

    def hello(self) -> dict:
        """What this device says about itself inside the handshake."""
        return {"id": self.device_id, "name": self.name, "platform": self.platform, "v": 1}


# ---------------------------------------------------------------------- #
# Pairing offers
# ---------------------------------------------------------------------- #
_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def encode_code(token: bytes) -> str:
    """16 bytes -> 'ABCD-EFGH-...' (26 base32 characters in groups of four)."""
    raw = base64.b32encode(token).decode().rstrip("=")
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def decode_code(code: str) -> bytes:
    """Forgiving: spaces, dashes, lowercase, 0/1 typed for O/I."""
    s = "".join(c for c in (code or "").upper() if c not in " -_.")
    s = s.replace("0", "O").replace("1", "I").replace("8", "B")
    if not s or any(c not in _B32 for c in s):
        raise ValueError("That code has characters that aren't in a SAINT pairing code.")
    s += "=" * (-len(s) % 8)
    token = base64.b32decode(s)
    if len(token) != 16:
        raise ValueError("That pairing code is the wrong length.")
    return token


def psk_for(token: bytes) -> bytes:
    return crypto.hkdf(token, b"SAINT-LINK-PAIRING", b"psk", 32)


@dataclass
class PairingOffer:
    token: bytes
    role: str
    expires: float
    label: str = ""
    failures: int = 0
    used: bool = False
    created: float = field(default_factory=time.time)

    @property
    def psk(self) -> bytes:
        return psk_for(self.token)

    @property
    def code(self) -> str:
        return encode_code(self.token)

    @property
    def valid(self) -> bool:
        return not self.used and self.failures < 10 and time.time() < self.expires

    def uri(self, host: str, port: int, name: str = "", alternates=()) -> str:
        """``alternates``: this PC's other addresses (its Tailscale 100.x address), which the
        phone tries when ``host`` doesn't answer — that's how it reaches the PC away from home."""
        q = {"h": host, "p": str(port), "t": base64.b32encode(self.token).decode().rstrip("="),
             "r": self.role, "n": name}
        alt = [a for a in alternates if a and a != host]
        if alt:
            q["a"] = ",".join(alt)
        return "saint://pair?" + urllib.parse.urlencode(q)


def parse_pair_uri(uri: str) -> dict:
    """saint://pair?h=..&p=..&t=..&r=..&n=.. -> {host, port, token, role, name}."""
    parsed = urllib.parse.urlparse((uri or "").strip())
    if parsed.scheme != "saint" or parsed.netloc != "pair":
        raise ValueError("That isn't a SAINT pairing link.")
    q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items() if v}
    try:
        host, port = q["h"], int(q["p"])
        token = decode_code(q["t"])
    except (KeyError, ValueError) as e:
        raise ValueError("That pairing link is incomplete.") from e
    role = q.get("r", OWN)
    alternates = [a.strip() for a in q.get("a", "").split(",") if a.strip() and a.strip() != host]
    return {"host": host, "port": port, "token": token, "role": role if role in (OWN, COLLABORATOR) else OWN,
            "name": q.get("n", ""), "alternates": alternates}


def parse_address(text: str, default_port: int = 8765) -> tuple:
    """'192.168.1.20:8765' / '192.168.1.20' / '[fe80::1]:8765' -> (host, port)."""
    t = (text or "").strip()
    if t.startswith("["):
        host, _, rest = t[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
    if t.count(":") == 1:
        host, _, port = t.partition(":")
        if port.isdigit():
            return host, int(port)
    return t, default_port


class PairingManager:
    """Holds the one open pairing offer (a new one replaces the old)."""

    TTL = 300.0

    def __init__(self):
        self._lock = threading.Lock()
        self._offer: Optional[PairingOffer] = None
        self.failures_by_ip: Dict[str, List[float]] = {}

    def create(self, role: str = OWN, label: str = "", ttl: Optional[float] = None) -> PairingOffer:
        if role not in (OWN, COLLABORATOR):
            raise ValueError("role must be 'own' or 'collaborator'")
        offer = PairingOffer(os.urandom(16), role, time.time() + (ttl or self.TTL), label)
        with self._lock:
            self._offer = offer
        log.info("link.pairing_offer role=%s ttl=%ds", role, int(ttl or self.TTL))
        return offer

    @property
    def current(self) -> Optional[PairingOffer]:
        with self._lock:
            offer = self._offer
        return offer if offer is not None and offer.valid else None

    def cancel(self):
        with self._lock:
            self._offer = None

    def consume(self, offer: PairingOffer):
        offer.used = True
        with self._lock:
            if self._offer is offer:
                self._offer = None

    def too_many_failures(self, ip: str, limit: int = 8, window: float = 60.0) -> bool:
        now = time.time()
        recent = [t for t in self.failures_by_ip.get(ip, []) if now - t < window]
        self.failures_by_ip[ip] = recent
        return len(recent) >= limit

    def note_failure(self, ip: str, offer: Optional[PairingOffer] = None):
        self.failures_by_ip.setdefault(ip, []).append(time.time())
        if offer is not None:
            offer.failures += 1

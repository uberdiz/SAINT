"""
modules/link/wire.py

SAINT Link on the wire: TCP, every frame ``<uint16 length><bytes>``.

    connection start (clear):  'S' 'L' <version=1> <mode>
        mode 1 = reconnect (Noise IK)   mode 2 = pairing (Noise XXpsk3)
    handshake:                 2 or 3 Noise messages, one frame each
    afterwards:                each frame is one ChaCha20-Poly1305 message whose
                               plaintext is  <type byte> <body>:

        0x01  a whole JSON message
        0x02  a JSON fragment: <flags: 1 = last> <bytes>   (messages over 60 kB)
        0x03  a file chunk: <transfer id: 16> <offset: 8, big endian> <bytes>

The mode byte and protocol version are part of the Noise prologue, so changing
either on the way makes the handshake fail instead of downgrading anything.
"""

import json
import socket
import struct
import threading
from typing import Optional, Tuple

from modules.link import noise
from modules.link.noise import CipherState, HandshakeState, NoiseError

VERSION = 1
MODE_RECONNECT = 1
MODE_PAIR = 2
PREAMBLE = b"SL"

T_JSON, T_FRAGMENT, T_CHUNK = 1, 2, 3
MAX_PLAIN = noise.MAX_MESSAGE - noise.TAG_LEN - 1       # room for the type byte
FRAGMENT = 60000
MAX_MESSAGE_BYTES = 32 * 1024 * 1024                    # a reassembled JSON message
CHUNK = 32 * 1024


class LinkError(Exception):
    """Anything that goes wrong on a link, with a short machine-readable code."""

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


class LinkClosed(LinkError):
    def __init__(self, message: str = "the connection closed"):
        super().__init__(message, "closed")


def prologue(mode: int) -> bytes:
    return b"SAINT-LINK/" + str(VERSION).encode() + b"/" + bytes([mode])


# ---------------------------------------------------------------------- #
# Frames
# ---------------------------------------------------------------------- #
def read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        try:
            part = sock.recv(n - len(buf))
        except (ConnectionError, OSError) as e:
            raise LinkClosed(str(e)) from e
        if not part:
            raise LinkClosed()
        buf += part
    return bytes(buf)


def read_frame(sock: socket.socket) -> bytes:
    (length,) = struct.unpack(">H", read_exact(sock, 2))
    return read_exact(sock, length) if length else b""


def write_frame(sock: socket.socket, data: bytes):
    if len(data) > noise.MAX_MESSAGE:
        raise LinkError("frame too large", "too_large")
    try:
        sock.sendall(struct.pack(">H", len(data)) + data)
    except (ConnectionError, OSError) as e:
        raise LinkClosed(str(e)) from e


# ---------------------------------------------------------------------- #
# Channel: the encrypted stream after the handshake
# ---------------------------------------------------------------------- #
class Channel:
    def __init__(self, sock: socket.socket, send: CipherState, recv: CipherState, remote_static: bytes):
        self.sock = sock
        self._send, self._recv = send, recv
        self.remote_static = remote_static
        self._send_lock = threading.Lock()
        self._fragments = bytearray()
        self.closed = False

    # -- sending ---------------------------------------------------------
    def _send_plain(self, plain: bytes):
        with self._send_lock:
            write_frame(self.sock, self._send.encrypt(b"", plain))

    def send_message(self, obj: dict):
        body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_MESSAGE_BYTES:
            raise LinkError("message too large", "too_large")
        if len(body) <= MAX_PLAIN:
            self._send_plain(bytes([T_JSON]) + body)
            return
        # Fragments of one message must not interleave with another sender's frames.
        with self._send_lock:
            for i in range(0, len(body), FRAGMENT):
                last = i + FRAGMENT >= len(body)
                plain = bytes([T_FRAGMENT, 1 if last else 0]) + body[i:i + FRAGMENT]
                write_frame(self.sock, self._send.encrypt(b"", plain))

    def send_chunk(self, transfer_id: bytes, offset: int, data: bytes):
        if len(transfer_id) != 16 or len(data) > CHUNK * 2:
            raise LinkError("bad chunk", "bad_chunk")
        self._send_plain(bytes([T_CHUNK]) + transfer_id + struct.pack(">Q", offset) + data)

    # -- receiving -------------------------------------------------------
    def recv(self) -> Tuple[str, object]:
        """('json', dict) or ('chunk', (transfer_id, offset, data))."""
        while True:
            try:
                plain = self._recv.decrypt(b"", read_frame(self.sock))
            except NoiseError as e:
                raise LinkError("a message failed authentication", "auth") from e
            if not plain:
                raise LinkError("empty message", "bad_message")
            kind, body = plain[0], plain[1:]
            if kind == T_JSON:
                return "json", self._parse(body)
            if kind == T_FRAGMENT:
                if len(body) < 1:
                    raise LinkError("bad fragment", "bad_message")
                self._fragments += body[1:]
                if len(self._fragments) > MAX_MESSAGE_BYTES:
                    raise LinkError("message too large", "too_large")
                if body[0] & 1:
                    whole, self._fragments = bytes(self._fragments), bytearray()
                    return "json", self._parse(whole)
                continue
            if kind == T_CHUNK:
                if len(body) < 24:
                    raise LinkError("bad chunk", "bad_message")
                return "chunk", (body[:16], struct.unpack(">Q", body[16:24])[0], body[24:])
            raise LinkError("unknown message type", "bad_message")

    @staticmethod
    def _parse(body: bytes) -> dict:
        try:
            obj = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise LinkError("unreadable message", "bad_message") from e
        if not isinstance(obj, dict):
            raise LinkError("unreadable message", "bad_message")
        return obj

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------- #
# Handshakes
# ---------------------------------------------------------------------- #
def _hello_bytes(hello: dict) -> bytes:
    return json.dumps(hello, separators=(",", ":")).encode("utf-8")


def _hello_from(payload: bytes) -> dict:
    try:
        hello = json.loads(payload.decode("utf-8"))
        if isinstance(hello, dict):
            return hello
    except (ValueError, UnicodeDecodeError):
        pass
    raise LinkError("the other device sent an unreadable hello", "bad_hello")


def client_handshake(sock: socket.socket, static_private: bytes, hello: dict, *,
                     remote_static: Optional[bytes] = None, psk: Optional[bytes] = None,
                     timeout: float = 10.0) -> Tuple[Channel, dict]:
    """Run the initiator side. ``psk`` given: the pairing handshake (XXpsk3).
    Otherwise a reconnect (IK) to ``remote_static``. Returns the encrypted
    Channel and the other side's hello; ``channel.remote_static`` is its key.
    After a pairing handshake the caller must read the server's first message
    before trusting (pinning) that key."""
    mode = MODE_PAIR if psk is not None else MODE_RECONNECT
    hs = HandshakeState("XXpsk3" if psk is not None else "IK", True, static_private,
                        remote_static=remote_static, psk=psk, prologue=prologue(mode))
    sock.settimeout(timeout)
    try:
        sock.sendall(PREAMBLE + bytes([VERSION, mode]))
        if mode == MODE_RECONNECT:
            write_frame(sock, hs.write_message(_hello_bytes(hello)))
            server_hello = _hello_from(hs.read_message(read_frame(sock)))
        else:
            write_frame(sock, hs.write_message())
            hs.read_message(read_frame(sock))
            write_frame(sock, hs.write_message(_hello_bytes(hello)))
            server_hello = {}
    except NoiseError as e:
        raise LinkError(f"handshake failed: {e}", "handshake") from e
    except socket.timeout as e:
        raise LinkError("the other device didn't answer", "timeout") from e
    send, recv = hs.split()
    sock.settimeout(None)
    return Channel(sock, send, recv, hs.rs), server_hello


class ServerHandshake:
    """The responder side, in two steps so the caller can decide in between.

    ``begin()`` reads the preamble and returns the mode (reconnect / pair).
    ``finish(...)`` completes the handshake. For a reconnect ``authorize`` is
    called with the initiator's static key after message 1 and may return False
    to hang up without answering."""

    def __init__(self, sock: socket.socket, static_private: bytes, timeout: float = 10.0):
        self.sock = sock
        self.static_private = static_private
        self.timeout = timeout
        self.mode = 0

    def begin(self) -> int:
        self.sock.settimeout(self.timeout)
        head = read_exact(self.sock, 4)
        if head[:2] != PREAMBLE or head[2] != VERSION or head[3] not in (MODE_RECONNECT, MODE_PAIR):
            raise LinkError("not a SAINT Link connection", "bad_preamble")
        self.mode = head[3]
        return self.mode

    def finish(self, hello: dict, *, psk: Optional[bytes] = None, authorize=None) -> Tuple[Channel, dict]:
        try:
            if self.mode == MODE_RECONNECT:
                hs = HandshakeState("IK", False, self.static_private, prologue=prologue(self.mode))
                client_hello = _hello_from(hs.read_message(read_frame(self.sock)))
                if authorize is not None and not authorize(hs.rs, client_hello):
                    raise LinkError("unknown device", "unauthorized")
                write_frame(self.sock, hs.write_message(_hello_bytes(hello)))
            else:
                if psk is None:
                    raise LinkError("no pairing is open", "no_offer")
                hs = HandshakeState("XXpsk3", False, self.static_private, psk=psk, prologue=prologue(self.mode))
                hs.read_message(read_frame(self.sock))
                write_frame(self.sock, hs.write_message())
                client_hello = _hello_from(hs.read_message(read_frame(self.sock)))
        except NoiseError as e:
            raise LinkError(f"handshake failed: {e}", "handshake") from e
        except socket.timeout as e:
            raise LinkError("the other device stopped answering", "timeout") from e
        send, recv = hs.split()
        self.sock.settimeout(None)
        return Channel(self.sock, send, recv, hs.rs), client_hello

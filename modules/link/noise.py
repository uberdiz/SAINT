"""
modules/link/noise.py

The Noise Protocol Framework (revision 34), reduced to what SAINT Link needs.

    Noise_IK_25519_ChaChaPoly_SHA256          a paired device reconnecting
        <- s
        -> e, es, s, ss
        <- e, ee, se

    Noise_XXpsk3_25519_ChaChaPoly_SHA256      first pairing; psk = one-time pairing token
        -> e
        <- e, ee, s, es
        -> s, se, psk

Every identity is a long-term X25519 key and nothing but ephemeral public keys
travels in the clear. In the pairing handshake the two devices exchange their
static keys *inside* the encrypted messages, and the pairing token is mixed in
last: a man in the middle who doesn't hold the token can't produce a message
the other side accepts, so the pairing code's entropy (128 bits) is what
protects it — not a short PIN. Each side pins the other's static key only after
the first authenticated message arrives.

Reconnects use IK: the initiator already knows the responder's static key (it
was pinned at pairing), the responder learns and checks the initiator's in
message 1 and can drop the connection without answering when it isn't a paired
device.

The Swift side (ios/Sources/SaintCore/Noise.swift) implements the same
handshakes on CryptoKit; tests/test_link.py holds known-answer vectors both
sides check, and the Python implementation was cross-checked against the
independent ``noiseprotocol`` package.
"""

import hashlib
import os
import struct
from typing import List, Optional, Tuple

from modules.link import crypto

PROTOCOL_IK = b"Noise_IK_25519_ChaChaPoly_SHA256"
PROTOCOL_XXPSK3 = b"Noise_XXpsk3_25519_ChaChaPoly_SHA256"
DH_LEN = 32
TAG_LEN = 16
MAX_MESSAGE = 65535


class NoiseError(Exception):
    pass


def _hash(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _hkdf(chaining_key: bytes, ikm: bytes, outputs: int):
    """Noise's HKDF: HMAC-based, no info string, 2 or 3 outputs."""
    out = crypto.hkdf(ikm, chaining_key, b"", 32 * outputs)
    return tuple(out[i * 32:(i + 1) * 32] for i in range(outputs))


def _nonce(n: int) -> bytes:
    return b"\x00\x00\x00\x00" + struct.pack("<Q", n)


class CipherState:
    """A key and a counter. Encrypting or decrypting advances the counter, so
    a message can never be replayed, reordered or dropped unnoticed."""

    def __init__(self, key: Optional[bytes] = None):
        self.k = key
        self.n = 0

    def has_key(self) -> bool:
        return self.k is not None

    def encrypt(self, ad: bytes, plaintext: bytes) -> bytes:
        if self.k is None:
            return plaintext
        if self.n >= 2 ** 64 - 1:
            raise NoiseError("nonce exhausted")
        out = crypto.seal(self.k, _nonce(self.n), plaintext, ad)
        self.n += 1
        return out

    def decrypt(self, ad: bytes, ciphertext: bytes) -> bytes:
        if self.k is None:
            return ciphertext
        try:
            out = crypto.open_sealed(self.k, _nonce(self.n), ciphertext, ad)
        except crypto.CryptoError as e:
            raise NoiseError("decryption failed") from e
        self.n += 1
        return out


class SymmetricState:
    def __init__(self, protocol_name: bytes):
        self.h = protocol_name.ljust(32, b"\x00") if len(protocol_name) <= 32 else _hash(protocol_name)
        self.ck = self.h
        self.cipher = CipherState()

    def mix_key(self, ikm: bytes):
        self.ck, temp_k = _hkdf(self.ck, ikm, 2)
        self.cipher = CipherState(temp_k)

    def mix_hash(self, data: bytes):
        self.h = _hash(self.h + data)

    def mix_key_and_hash(self, ikm: bytes):
        self.ck, temp_h, temp_k = _hkdf(self.ck, ikm, 3)
        self.mix_hash(temp_h)
        self.cipher = CipherState(temp_k)

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ct = self.cipher.encrypt(self.h, plaintext)
        self.mix_hash(ct)
        return ct

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        pt = self.cipher.decrypt(self.h, ciphertext)
        self.mix_hash(ciphertext)
        return pt

    def split(self) -> Tuple[CipherState, CipherState]:
        k1, k2 = _hkdf(self.ck, b"", 2)
        return CipherState(k1), CipherState(k2)


# Message patterns: one list of tokens per message, initiator first.
_PATTERNS = {
    "IK": [["e", "es", "s", "ss"], ["e", "ee", "se"]],
    "XXpsk3": [["e"], ["e", "ee", "s", "es"], ["s", "se", "psk"]],
}
_NAMES = {"IK": PROTOCOL_IK, "XXpsk3": PROTOCOL_XXPSK3}


class HandshakeState:
    """One side of a handshake. Alternate write_message / read_message in
    pattern order (the initiator writes first); when ``finished`` call split()."""

    def __init__(self, pattern: str, initiator: bool, static_private: bytes,
                 remote_static: Optional[bytes] = None, psk: Optional[bytes] = None,
                 prologue: bytes = b"", ephemeral_private: Optional[bytes] = None):
        if pattern not in _PATTERNS:
            raise NoiseError(f"unknown handshake pattern {pattern}")
        self.pattern = pattern
        self.messages: List[List[str]] = _PATTERNS[pattern]
        self.initiator = initiator
        self.psk = psk
        self.psk_mode = "psk" in pattern
        if self.psk_mode and (psk is None or len(psk) != 32):
            raise NoiseError("a psk handshake needs a 32-byte psk")
        self.ss = SymmetricState(_NAMES[pattern])
        self.ss.mix_hash(prologue)
        self.s = static_private
        self.s_pub = crypto.public_key(static_private)
        self.e: Optional[bytes] = None
        self.e_pub: Optional[bytes] = None
        self._fixed_e = ephemeral_private            # tests only
        self.rs = remote_static
        self.re: Optional[bytes] = None
        self._index = 0
        if pattern == "IK":                          # <- s  (pre-message)
            if initiator:
                if remote_static is None:
                    raise NoiseError("IK needs the responder's static key up front")
                self.ss.mix_hash(remote_static)
            else:
                self.ss.mix_hash(self.s_pub)

    # ------------------------------------------------------------------ #
    @property
    def finished(self) -> bool:
        return self._index >= len(self.messages)

    @property
    def my_turn_to_write(self) -> bool:
        return not self.finished and (self._index % 2 == 0) == self.initiator

    @property
    def handshake_hash(self) -> bytes:
        return self.ss.h

    def _dh(self, token: str) -> bytes:
        if token == "ee":
            return crypto.shared_secret(self.e, self.re)
        if token == "ss":
            return crypto.shared_secret(self.s, self.rs)
        if token == "es":       # initiator's ephemeral x responder's static
            return crypto.shared_secret(self.e, self.rs) if self.initiator else \
                crypto.shared_secret(self.s, self.re)
        if token == "se":       # initiator's static x responder's ephemeral
            return crypto.shared_secret(self.s, self.re) if self.initiator else \
                crypto.shared_secret(self.e, self.rs)
        raise NoiseError(f"bad token {token}")

    # ------------------------------------------------------------------ #
    def write_message(self, payload: bytes = b"") -> bytes:
        if not self.my_turn_to_write:
            raise NoiseError("out of turn")
        ss, out = self.ss, b""
        try:
            for token in self.messages[self._index]:
                if token == "e":
                    self.e = self._fixed_e or os.urandom(32)
                    self.e_pub = crypto.public_key(self.e)
                    out += self.e_pub
                    ss.mix_hash(self.e_pub)
                    if self.psk_mode:
                        ss.mix_key(self.e_pub)
                elif token == "s":
                    out += ss.encrypt_and_hash(self.s_pub)
                elif token == "psk":
                    ss.mix_key_and_hash(self.psk)
                else:
                    ss.mix_key(self._dh(token))
        except crypto.CryptoError as e:
            raise NoiseError(str(e)) from e
        out += ss.encrypt_and_hash(payload)
        if len(out) > MAX_MESSAGE:
            raise NoiseError("handshake message too large")
        self._index += 1
        return out

    def read_message(self, message: bytes) -> bytes:
        if self.finished or self.my_turn_to_write:
            raise NoiseError("out of turn")
        if len(message) > MAX_MESSAGE:
            raise NoiseError("handshake message too large")
        ss, pos = self.ss, 0
        try:
            for token in self.messages[self._index]:
                if token == "e":
                    if len(message) - pos < DH_LEN:
                        raise NoiseError("short handshake message")
                    self.re = message[pos:pos + DH_LEN]
                    pos += DH_LEN
                    ss.mix_hash(self.re)
                    if self.psk_mode:
                        ss.mix_key(self.re)
                elif token == "s":
                    size = DH_LEN + (TAG_LEN if ss.cipher.has_key() else 0)
                    if len(message) - pos < size:
                        raise NoiseError("short handshake message")
                    self.rs = ss.decrypt_and_hash(message[pos:pos + size])
                    pos += size
                elif token == "psk":
                    ss.mix_key_and_hash(self.psk)
                else:
                    ss.mix_key(self._dh(token))
            payload = ss.decrypt_and_hash(message[pos:])
        except crypto.CryptoError as e:
            raise NoiseError(str(e)) from e
        self._index += 1
        return payload

    def split(self) -> Tuple[CipherState, CipherState]:
        """(send, receive) cipher states for this side."""
        if not self.finished:
            raise NoiseError("handshake isn't finished")
        c1, c2 = self.ss.split()
        return (c1, c2) if self.initiator else (c2, c1)

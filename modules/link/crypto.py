"""
modules/link/crypto.py

The cryptography SAINT Link needs, with no extra dependency:

    X25519             key agreement while pairing            (RFC 7748)
    HKDF-SHA256        the shared key from that agreement     (RFC 5869)
    ChaCha20-Poly1305  every message after pairing            (RFC 8439)

The iPhone app uses the same three through Apple's CryptoKit, so both sides
speak plain RFC algorithms. When the ``cryptography`` package is installed it
is used (faster); otherwise the implementations below run — ChaCha20 over
numpy (SAINT already needs numpy) and the rest in plain Python. Both paths are
checked against the RFC test vectors in tests/test_link.py.
"""

import hashlib
import hmac
import os
import struct
from typing import Tuple

try:                                    # optional accelerator
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives import serialization
    HAVE_CRYPTOGRAPHY = True
except Exception:                       # not installed, or blocked by Application Control
    HAVE_CRYPTOGRAPHY = False

try:
    import numpy as np
except Exception:                       # pragma: no cover - numpy is a SAINT requirement
    np = None

KEY_LEN = 32
NONCE_LEN = 12
TAG_LEN = 16


class CryptoError(Exception):
    """A message that doesn't authenticate (wrong key, tampered, truncated)."""


# ---------------------------------------------------------------------- #
# X25519
# ---------------------------------------------------------------------- #
_P = 2 ** 255 - 19
_A24 = 121665
_BASE = (9).to_bytes(32, "little")


def _x25519(k: bytes, u: bytes) -> bytes:
    if len(k) != 32 or len(u) != 32:
        raise ValueError("X25519 keys are 32 bytes")
    kb = bytearray(k)
    kb[0] &= 248
    kb[31] &= 127
    kb[31] |= 64
    scalar = int.from_bytes(kb, "little")
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        bit = (scalar >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = (da + cb) % _P
        x3 = x3 * x3 % _P
        z3 = (da - cb) % _P
        z3 = x1 * (z3 * z3 % _P) % _P
        x2 = aa * bb % _P
        z2 = e * ((aa + _A24 * e) % _P) % _P
    if swap:
        x2, x3, z2, z3 = x3, x2, z3, z2
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def generate_keypair() -> Tuple[bytes, bytes]:
    """(private, public), 32 bytes each."""
    if HAVE_CRYPTOGRAPHY:
        priv = X25519PrivateKey.generate()
        return (priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                   serialization.NoEncryption()),
                priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    private = os.urandom(32)
    return private, _x25519(private, _BASE)


def public_key(private: bytes) -> bytes:
    return _x25519(private, _BASE)


def shared_secret(private: bytes, peer_public: bytes) -> bytes:
    if HAVE_CRYPTOGRAPHY:
        try:
            secret = X25519PrivateKey.from_private_bytes(private).exchange(
                X25519PublicKey.from_public_bytes(peer_public))
        except Exception as e:
            raise CryptoError(f"key agreement failed: {e}") from e
    else:
        secret = _x25519(private, peer_public)
    if secret == bytes(32):
        raise CryptoError("key agreement produced an all-zero secret")
    return secret


# ---------------------------------------------------------------------- #
# HMAC / HKDF
# ---------------------------------------------------------------------- #
def hmac256(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, hashlib.sha256).digest()


def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = KEY_LEN) -> bytes:
    prk = hmac256(salt or bytes(32), ikm)
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac256(prk, block + info + bytes([counter]))
        out += block
        counter += 1
    return out[:length]


def constant_time_equal(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


# ---------------------------------------------------------------------- #
# ChaCha20 (numpy across blocks; plain ints when numpy is missing)
# ---------------------------------------------------------------------- #
_SIGMA = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)


def _keystream_np(key: bytes, counter: int, nonce: bytes, blocks: int) -> bytes:
    k = struct.unpack("<8I", key)
    n = struct.unpack("<3I", nonce)
    init = np.empty((16, blocks), dtype=np.uint32)
    for i, v in enumerate(_SIGMA + k):
        init[i] = v
    init[12] = (np.arange(blocks, dtype=np.uint64) + counter).astype(np.uint32)
    for i, v in enumerate(n):
        init[13 + i] = v
    x = init.copy()

    def qr(a, b, c, d):
        x[a] += x[b]
        x[d] ^= x[a]
        x[d] = (x[d] << 16) | (x[d] >> 16)
        x[c] += x[d]
        x[b] ^= x[c]
        x[b] = (x[b] << 12) | (x[b] >> 20)
        x[a] += x[b]
        x[d] ^= x[a]
        x[d] = (x[d] << 8) | (x[d] >> 24)
        x[c] += x[d]
        x[b] ^= x[c]
        x[b] = (x[b] << 7) | (x[b] >> 25)

    for _ in range(10):
        qr(0, 4, 8, 12); qr(1, 5, 9, 13); qr(2, 6, 10, 14); qr(3, 7, 11, 15)
        qr(0, 5, 10, 15); qr(1, 6, 11, 12); qr(2, 7, 8, 13); qr(3, 4, 9, 14)
    x += init
    return x.T.astype("<u4").tobytes()


def _keystream_py(key: bytes, counter: int, nonce: bytes, blocks: int) -> bytes:
    mask = 0xFFFFFFFF
    k = struct.unpack("<8I", key)
    n = struct.unpack("<3I", nonce)
    out = bytearray()

    def rotl(v, c):
        return ((v << c) & mask) | (v >> (32 - c))

    def qr(s, a, b, c, d):
        s[a] = (s[a] + s[b]) & mask; s[d] = rotl(s[d] ^ s[a], 16)
        s[c] = (s[c] + s[d]) & mask; s[b] = rotl(s[b] ^ s[c], 12)
        s[a] = (s[a] + s[b]) & mask; s[d] = rotl(s[d] ^ s[a], 8)
        s[c] = (s[c] + s[d]) & mask; s[b] = rotl(s[b] ^ s[c], 7)

    for i in range(blocks):
        init = list(_SIGMA + k + ((counter + i) & mask,) + n)
        s = list(init)
        for _ in range(10):
            qr(s, 0, 4, 8, 12); qr(s, 1, 5, 9, 13); qr(s, 2, 6, 10, 14); qr(s, 3, 7, 11, 15)
            qr(s, 0, 5, 10, 15); qr(s, 1, 6, 11, 12); qr(s, 2, 7, 8, 13); qr(s, 3, 4, 9, 14)
        out += struct.pack("<16I", *((a + b) & mask for a, b in zip(s, init)))
    return bytes(out)


def _keystream(key: bytes, counter: int, nonce: bytes, length: int) -> bytes:
    blocks = (length + 63) // 64
    if blocks == 0:
        return b""
    stream = _keystream_np(key, counter, nonce, blocks) if np is not None else \
        _keystream_py(key, counter, nonce, blocks)
    return stream[:length]


def _xor(data: bytes, stream: bytes) -> bytes:
    if not data:
        return b""
    if np is not None:
        return (np.frombuffer(data, dtype=np.uint8) ^ np.frombuffer(stream, dtype=np.uint8)).tobytes()
    return (int.from_bytes(data, "little") ^ int.from_bytes(stream, "little")).to_bytes(len(data), "little")


# ---------------------------------------------------------------------- #
# Poly1305
# ---------------------------------------------------------------------- #
_P1305 = (1 << 130) - 5


def _poly1305(otk: bytes, message: bytes) -> bytes:
    r = int.from_bytes(otk[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(otk[16:32], "little")
    acc = 0
    for i in range(0, len(message), 16):
        chunk = message[i:i + 16]
        acc = (acc + int.from_bytes(chunk, "little") + (1 << (8 * len(chunk)))) * r % _P1305
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _pad16(b: bytes) -> bytes:
    return b"\x00" * (-len(b) % 16)


def _tag(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    otk = _keystream(key, 0, nonce, 32)
    mac_data = aad + _pad16(aad) + ciphertext + _pad16(ciphertext) + \
        struct.pack("<QQ", len(aad), len(ciphertext))
    return _poly1305(otk, mac_data)


# ---------------------------------------------------------------------- #
# AEAD
# ---------------------------------------------------------------------- #
def seal(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """ciphertext || 16-byte tag."""
    if len(key) != KEY_LEN or len(nonce) != NONCE_LEN:
        raise ValueError("ChaCha20-Poly1305 needs a 32-byte key and a 12-byte nonce")
    if HAVE_CRYPTOGRAPHY:
        return ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad or None)
    ciphertext = _xor(plaintext, _keystream(key, 1, nonce, len(plaintext)))
    return ciphertext + _tag(key, nonce, ciphertext, aad)


def open_sealed(key: bytes, nonce: bytes, sealed: bytes, aad: bytes = b"") -> bytes:
    """The plaintext, or CryptoError when the message doesn't authenticate."""
    if len(key) != KEY_LEN or len(nonce) != NONCE_LEN or len(sealed) < TAG_LEN:
        raise CryptoError("malformed message")
    if HAVE_CRYPTOGRAPHY:
        try:
            return ChaCha20Poly1305(key).decrypt(nonce, sealed, aad or None)
        except Exception as e:
            raise CryptoError("message failed authentication") from e
    ciphertext, tag = sealed[:-TAG_LEN], sealed[-TAG_LEN:]
    if not hmac.compare_digest(tag, _tag(key, nonce, ciphertext, aad)):
        raise CryptoError("message failed authentication")
    return _xor(ciphertext, _keystream(key, 1, nonce, len(ciphertext)))


def new_nonce() -> bytes:
    return os.urandom(NONCE_LEN)

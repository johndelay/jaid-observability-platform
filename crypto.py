"""Encrypt-before-write for exports (V13 Slice 3).

AES-256-GCM with a scrypt-derived key from the user's passphrase. We encrypt the in-memory (gzipped) bytes,
so PLAINTEXT NEVER TOUCHES DISK — only the ciphertext is written. The passphrase/key is never stored; lose it
and the file is unrecoverable (the UI tells the user to keep it in 1Password). The `cryptography` wheel ships
musllinux so it installs on alpine without a compiler (verified).

File layout:  MAGIC(8) | version(1) | salt(16) | nonce(12) | AESGCM-ciphertext(+tag)
"""
import os

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    _HAVE = True
except ImportError:
    _HAVE = False

MAGIC = b"CCOBSENC"
VERSION = 1
_N, _R, _P = 2 ** 14, 8, 1   # scrypt cost params (interactive-grade)


def available():
    return _HAVE


def _derive(passphrase, salt):
    return Scrypt(salt=salt, length=32, n=_N, r=_R, p=_P).derive(passphrase.encode("utf-8"))


def encrypt(plaintext, passphrase):
    """bytes + passphrase → encrypted blob (MAGIC header + AES-256-GCM)."""
    if not _HAVE:
        raise RuntimeError("cryptography not available")
    if not passphrase:
        raise ValueError("passphrase required")
    salt, nonce = os.urandom(16), os.urandom(12)
    key = _derive(passphrase, salt)
    ct = AESGCM(key).encrypt(nonce, plaintext, MAGIC)   # MAGIC as AAD
    return MAGIC + bytes([VERSION]) + salt + nonce + ct


def is_encrypted(blob):
    return isinstance(blob, (bytes, bytearray)) and blob[:8] == MAGIC


def decrypt(blob, passphrase):
    """encrypted blob + passphrase → plaintext bytes. Raises on a wrong passphrase / tampered file."""
    if not _HAVE:
        raise RuntimeError("cryptography not available")
    if blob[:8] != MAGIC:
        raise ValueError("not a cc-observability encrypted file")
    salt, nonce, ct = blob[9:25], blob[25:37], blob[37:]
    key = _derive(passphrase, salt)
    return AESGCM(key).decrypt(nonce, ct, MAGIC)

"""
utils/crypto.py
────────────────
AES-256-GCM encryption helpers for the Token Vault.

Tokens stored in Auth0 app_metadata are encrypted before storage
so that even Auth0 staff / log infrastructure never sees plaintext.

Key derivation:
  The encryption key is read from settings.encryption_key (base64-encoded
  32-byte key).  If not set, a per-process ephemeral key is generated
  (tokens will be unreadable after restart — acceptable for development).
"""
from __future__ import annotations

import base64
import logging
import os
import secrets

logger = logging.getLogger("washfix.utils.crypto")

_KEY: bytes | None = None


def _get_key() -> bytes:
    global _KEY
    if _KEY is not None:
        return _KEY

    from config.settings import get_settings
    raw = get_settings().encryption_key
    if raw:
        try:
            key = base64.urlsafe_b64decode(raw + "==")  # tolerate missing padding
            if len(key) >= 32:
                _KEY = key[:32]
                return _KEY
        except Exception:
            pass

    # Ephemeral key (dev mode)
    logger.warning(
        "ENCRYPTION_KEY not set — using ephemeral per-process key. "
        "Stored tokens will be unreadable after restart."
    )
    _KEY = secrets.token_bytes(32)
    return _KEY


def encrypt_value(plaintext: str) -> str:
    """
    Encrypt a string with AES-256-GCM.
    Returns a base64url-encoded string: nonce(12) || ciphertext || tag(16).
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key   = _get_key()
        nonce = os.urandom(12)
        ct    = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
        blob  = nonce + ct
        return base64.urlsafe_b64encode(blob).decode()
    except ImportError:
        # Fallback: simple base64 (not secure — for environments without cryptography)
        logger.warning("cryptography package not available — using plain base64 (NOT secure).")
        return base64.urlsafe_b64encode(plaintext.encode()).decode()


def decrypt_value(encoded: str) -> str:
    """
    Decrypt a value produced by encrypt_value().
    Returns the original plaintext string.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        blob  = base64.urlsafe_b64decode(encoded + "==")
        key   = _get_key()
        nonce = blob[:12]
        ct    = blob[12:]
        plain = AESGCM(key).decrypt(nonce, ct, None)
        return plain.decode()
    except ImportError:
        # Matching fallback
        return base64.urlsafe_b64decode(encoded + "==").decode()
    except Exception as exc:
        logger.error(f"Decryption failed: {exc}")
        return ""


def generate_idempotency_key(session_id: str, action: str) -> str:
    """Generate a deterministic but unguessable idempotency key."""
    import hashlib
    salt = _get_key()[:8].hex()
    return hashlib.sha256(f"{salt}:{session_id}:{action}".encode()).hexdigest()[:32]

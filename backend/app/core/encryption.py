"""
TokenPool — Cryptographic Credential Vault
Ensures secure storage and retrieval of provider credentials using symmetric encryption (Fernet / AES-128-CBC + HMAC-SHA256).

Strict Security Rules:
- Never log raw credentials.
- Never expose raw credentials in API responses, HTML, JS, or logs.
- Derive a cryptographically sound key from ENCRYPTION_KEY or SECRET_KEY.
- Support deterministic masking for user interface display.
"""

import os
import base64
import hashlib
from typing import Optional
from cryptography.fernet import Fernet
from backend.app.core.config import settings


def _get_fernet() -> Fernet:
    """Derive a valid 32-byte urlsafe base64 key for Fernet."""
    custom_key = (settings.ENCRYPTION_KEY or "").strip()
    if custom_key:
        try:
            # Check if it's already a valid Fernet key (32 urlsafe base64 bytes)
            return Fernet(custom_key.encode())
        except Exception:
            # Hash to 32 bytes and base64 encode
            derived = base64.urlsafe_b64encode(hashlib.sha256(custom_key.encode()).digest())
            return Fernet(derived)
    
    if settings.ENVIRONMENT.lower() == "production":
        raise ValueError(
            "FATAL SECURITY ERROR: FERNET_KEY is not configured in production. "
            "Credential encryption cannot fall back to development secret in production."
        )

    # Fallback to SECRET_KEY derived key (Development only)
    secret = (settings.SECRET_KEY or "tokenpool-default-encryption-salt-2026").encode()
    derived = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(derived)


def encrypt_credential(plain_text: str) -> str:
    """Encrypt a raw API key or credential string."""
    if not plain_text:
        return ""
    fernet = _get_fernet()
    encrypted = fernet.encrypt(plain_text.strip().encode())
    return encrypted.decode()


def decrypt_credential(cipher_text: str) -> str:
    """Decrypt an encrypted credential string back to plain text."""
    if not cipher_text:
        return ""
    fernet = _get_fernet()
    try:
        decrypted = fernet.decrypt(cipher_text.strip().encode())
        return decrypted.decode()
    except Exception as e:
        raise ValueError(f"Failed to decrypt credential: invalid key or corrupted ciphertext ({e})")


def mask_credential(key: str) -> str:
    """Mask an API key for safe UI display (e.g. sk-••••••••9842 or AIza••••••••3981)."""
    if not key:
        return "••••••••"
    key = key.strip()
    if len(key) <= 8:
        return "••••••••"
    
    # Check for known prefixes
    prefix = ""
    for known_pfx in ("sk-live-", "sk-proj-", "sk-ant-", "sk-", "AIza"):
        if key.startswith(known_pfx):
            prefix = known_pfx
            break
            
    if not prefix:
        prefix = key[:4]
        
    suffix = key[-4:]
    return f"{prefix}••••••••{suffix}"

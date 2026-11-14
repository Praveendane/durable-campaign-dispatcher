
import hashlib
import hmac
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from cryptography.fernet import Fernet


# Load secrets from the project's root .env file.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def get_encryption_key() -> bytes:
    key = os.getenv("ENCRYPTION_KEY")

    if not key:
        raise RuntimeError("ENCRYPTION_KEY is not configured")

    try:
        key_bytes = key.encode("utf-8")
        Fernet(key_bytes)  # Validate the key format.
        return key_bytes
    except (ValueError, TypeError):
        raise RuntimeError("ENCRYPTION_KEY is invalid")


def get_hash_secret() -> bytes:
    secret = os.getenv("PHONE_HASH_SECRET")

    if not secret:
        raise RuntimeError("PHONE_HASH_SECRET is not configured")

    return secret.encode("utf-8")


def encrypt_value(value: str) -> str:
    fernet = Fernet(get_encryption_key())
    return fernet.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_value(encrypted_value: str) -> str:
    fernet = Fernet(get_encryption_key())
    return fernet.decrypt(encrypted_value.encode("utf-8")).decode("utf-8")


def normalize_phone(phone: str) -> str:
    """Normalize phone formatting before hashing."""
    normalized = re.sub(r"[\s()-]", "", phone)

    if not re.fullmatch(r"\+?\d+", normalized):
        raise ValueError("Phone number contains invalid characters")

    return normalized


def hash_phone(phone: str) -> str:
    normalized = normalize_phone(phone)
    secret = get_hash_secret()

    return hmac.new(
        secret,
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

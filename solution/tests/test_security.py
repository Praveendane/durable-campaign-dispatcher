
from solution.app.security import (
    encrypt_value,
    decrypt_value,
    hash_phone,
    normalize_phone,
)


def test_encrypt_and_decrypt():
    original = "Test Customer"

    encrypted = encrypt_value(original)

    assert encrypted != original
    assert decrypt_value(encrypted) == original


def test_phone_hash_is_consistent():
    phone1 = "+91 9876543210"
    phone2 = "+91-9876543210"

    assert hash_phone(phone1) == hash_phone(phone2)


def test_phone_normalization():
    phone = "+91 (987) 654-3210"

    assert normalize_phone(phone) == "+919876543210"


def test_different_phones_have_different_hashes():
    assert hash_phone("+919876543210") != hash_phone("+919876543211")

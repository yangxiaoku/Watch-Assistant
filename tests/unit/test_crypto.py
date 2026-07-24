import pytest

from watch_assistant.crypto import SecretCrypto


def test_secret_round_trip(crypto: SecretCrypto):
    token = "magnet:?xt=urn:btih:ABC"

    encrypted = crypto.encrypt(token)

    assert token not in encrypted
    assert crypto.decrypt(encrypted) == token


def test_invalid_encryption_key_has_named_error():
    with pytest.raises(ValueError, match="ENCRYPTION_KEY"):
        SecretCrypto("invalid")

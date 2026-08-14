import pytest
from cryptography.fernet import Fernet

from watch_assistant.crypto import SecretCrypto


def test_secret_round_trip(crypto: SecretCrypto):
    token = "magnet:?xt=urn:btih:ABC"

    encrypted = crypto.encrypt(token)

    assert token not in encrypted
    assert crypto.decrypt(encrypted) == token


def test_invalid_encryption_key_has_named_error():
    with pytest.raises(ValueError, match="ENCRYPTION_KEY"):
        SecretCrypto("invalid")


def test_domain_ciphertexts_are_scoped_per_domain():
    """F6:不同域的密文不可互相解密(域分隔)。"""
    master = Fernet.generate_key().decode("ascii")
    domain_a = SecretCrypto(master, domain="webhook")
    domain_b = SecretCrypto(master, domain="tmdb")
    encrypted = domain_a.encrypt("secret-value")
    assert domain_a.decrypt(encrypted) == "secret-value"
    with pytest.raises(ValueError):
        domain_b.decrypt(encrypted)


def test_domain_encrypt_uses_derived_key_and_legacy_fallback():
    """F6:域实例加密用派生钥;存量 master 密文仍可经回退解密。"""
    master = Fernet.generate_key().decode("ascii")
    legacy = SecretCrypto(master)
    domain = SecretCrypto(master, domain="tmdb")
    value = "legacy-token"
    legacy_encrypted = legacy.encrypt(value)
    # 域实例能解历史 master 密文(迁移回退)
    assert domain.decrypt(legacy_encrypted) == value
    # 域实例加密的新密文与 master 直接加密的不同
    domain_encrypted = domain.encrypt(value)
    assert domain_encrypted != legacy_encrypted
    assert domain.decrypt(domain_encrypted) == value
    # 无域实例不能解域密文(域分隔)
    with pytest.raises(ValueError):
        legacy.decrypt(domain_encrypted)


def test_domain_override_key_rotates_encryption_but_keeps_derived_fallback():
    """F6:设置 ENCRYPTION_KEY_<DOMAIN> 轮换后,新密文用新钥,旧派生密文仍可读。"""
    master = Fernet.generate_key().decode("ascii")
    override = Fernet.generate_key().decode("ascii")
    original = SecretCrypto(master, domain="webhook")
    rotated = SecretCrypto(override, domain="webhook", master_key=master)
    value = "whsec_secret"
    old = original.encrypt(value)
    new = rotated.encrypt(value)
    assert rotated.decrypt(old) == value
    assert rotated.decrypt(new) == value
    # 未轮换的实例无法解新钥密文
    with pytest.raises(ValueError):
        original.decrypt(new)


def test_no_domain_instance_keeps_legacy_single_key_behavior(crypto: SecretCrypto):
    encrypted = crypto.encrypt("value")
    assert crypto.decrypt(encrypted) == "value"
    assert crypto._domain_fernet is None

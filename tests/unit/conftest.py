import pytest
from cryptography.fernet import Fernet

from watch_assistant.crypto import SecretCrypto


@pytest.fixture
def crypto() -> SecretCrypto:
    return SecretCrypto(Fernet.generate_key().decode("ascii"))

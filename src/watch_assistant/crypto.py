"""Application-level encryption for resource URLs and share passwords."""

from cryptography.fernet import Fernet


class SecretCrypto:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (TypeError, ValueError) as exc:
            raise ValueError("ENCRYPTION_KEY is not a valid Fernet key") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")

"""Application-level encryption for resource URLs and share passwords.

密钥域分隔:同一 ENCRYPTION_KEY 按业务域派生独立 Fernet 密钥,任一服务的
密文只能由同域实例解密(一处服务进程泄露只暴露本域密钥)。解密链保留
legacy master 回退,使存量密文(历史版本用裸 master 加密)平滑迁移,
不因升级而丢失凭据;设置 ENCRYPTION_KEY_<DOMAIN> 环境变量可为单个域
轮换密钥(新密文用新钥,旧密文仍可经派生链解密)。
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

_DERIVATION_CONTEXT = b"watch-assistant/v1/domain/"


def _derive_domain_key(master_key: str, domain: str) -> str:
    """Derive a deterministic per-domain Fernet key from the master key."""
    raw = base64.urlsafe_b64decode(master_key.encode("ascii"))
    derived = hmac.new(
        raw, _DERIVATION_CONTEXT + domain.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(derived).decode("ascii")


class SecretCrypto:
    def __init__(
        self,
        key: str,
        *,
        domain: str | None = None,
        master_key: str | None = None,
    ) -> None:
        """
        :param key: 主密钥(ENCRYPTION_KEY)或该域的轮换密钥(ENCRYPTION_KEY_<DOMAIN>)。
        :param domain: 业务域标签;None 表示历史单钥模式。
        :param master_key: 原主密钥,用于派生链回退;设置轮换密钥时必须提供。
        """
        try:
            self._legacy_fernet = Fernet(key.encode("ascii"))
        except (TypeError, ValueError) as exc:
            raise ValueError("ENCRYPTION_KEY is not a valid Fernet key") from exc
        self._domain_fernet: Fernet | None = None
        self._derived_fernet: Fernet | None = None
        if domain is None:
            return
        master = master_key or key
        try:
            if master_key is None:
                self._domain_fernet = Fernet(
                    _derive_domain_key(master, domain).encode("ascii")
                )
            else:
                self._domain_fernet = Fernet(key.encode("ascii"))
                self._derived_fernet = Fernet(
                    _derive_domain_key(master, domain).encode("ascii")
                )
        except (TypeError, ValueError) as exc:
            raise ValueError("ENCRYPTION_KEY is not a valid Fernet key") from exc

    def encrypt(self, value: str) -> str:
        fernet = self._domain_fernet or self._legacy_fernet
        return fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if self._domain_fernet is not None:
            try:
                return self._domain_fernet.decrypt(value.encode("ascii")).decode("utf-8")
            except InvalidToken:
                pass
        if self._derived_fernet is not None:
            try:
                return self._derived_fernet.decrypt(value.encode("ascii")).decode("utf-8")
            except InvalidToken:
                pass
        try:
            return self._legacy_fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("secret could not be decrypted") from exc


__all__ = ["SecretCrypto"]

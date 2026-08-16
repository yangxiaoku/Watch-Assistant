"""115 请求画像:模拟浏览器特征,降低批量/高频访问触发 405 风控的概率。

2026-08 实测:批量分页扫描(组织/库扫描、清理核对)在 115 侧高频请求下
返回 HTTP 405(风控,要求重新验证),浏览器 UA 直连同一接口正常。
p115client 默认请求不带浏览器 UA(部分接口 UA 为空),脚本特征明显。

本模块对 ``P115Client.request`` 做一次幂等补丁:所有请求注入浏览器
UA(以及常见浏览器头),不影响其它行为;版本升级后需重新核对签名。
"""

from __future__ import annotations

from typing import Any

P115_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_PATCHED = False


def ensure_browser_request_profile() -> None:
    """Power-idempotently patch ``P115Client.request`` to send browser headers.

    仅注入 ``user-agent``/``referer``/``accept`` 等浏览器常规请求头,
    不改动 URL、参数、超时或重试语义。
    """
    global _PATCHED
    if _PATCHED:
        return
    from p115client import P115Client

    original = P115Client.request

    def patched(
        self,
        /,
        url: str,
        method: str = "GET",
        payload: Any = None,
        *,
        check: bool = False,
        ecdh_encrypt: bool = False,
        request: Any = None,
        async_: bool = False,
        **request_kwargs: Any,
    ) -> Any:
        headers = dict(request_kwargs.get("headers") or {})
        headers.setdefault("user-agent", P115_BROWSER_USER_AGENT)
        headers.setdefault("referer", "https://115.com/")
        headers.setdefault("accept", "application/json, text/plain, */*")
        request_kwargs["headers"] = headers
        return original(
            self,
            url,
            method=method,
            payload=payload,
            check=check,
            ecdh_encrypt=ecdh_encrypt,
            request=request,
            async_=async_,
            **request_kwargs,
        )

    P115Client.request = patched  # type: ignore[method-assign]
    _PATCHED = True

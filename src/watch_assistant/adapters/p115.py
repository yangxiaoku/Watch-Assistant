"""Minimal, guarded adapter for the fixed p115client release."""

from __future__ import annotations

import asyncio
import base64
import inspect
import re
from collections.abc import Callable, Mapping
from importlib.metadata import version
from typing import Any
from urllib.parse import parse_qs, urlsplit

from watch_assistant.adapters.p115_library_gateway import (
    P115ReadOnlyDirectoryGateway,
    P115ReadOnlyGatewayError,
)
from watch_assistant.schemas import RemoteObservation, RemoteStatus, SubmissionResult
from watch_assistant.services.p115_credentials import (
    CookieProvider,
    normalize_cookie_text,
)
from watch_assistant.services.p115_settings import (
    P115NeedsAuthError,
    P115UnavailableError,
)

P115CLIENT_VERSION = "0.0.9.6.5.1"
INFOHASH_REMOTE_REF_PREFIX = "infohash:"
MAX_SHARE_ITEMS = 1000
# 服务端"仍在处理上次提交"的 busy 错误码(与只读路径约定一致,
# 见 AGENTS.md「_p115client_timeout_executor 对 errno=990009 使用 3 秒重试」)。
_P115_BUSY_OPERATION_ERRNO = 990009
_P115_BUSY_RETRY_DELAY_SECONDS = 3.0
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
_BTIH_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[a-z2-7]{32})$", re.IGNORECASE)
_SHARE_CODE_PATTERN = re.compile(r"^/(?:s|share)/([A-Za-z0-9_-]+)/?$", re.IGNORECASE)
_INFOHASH_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_SHARE_HOSTS = frozenset(("115.com", "115cdn.com", "anxia.com"))
_AUTH_ERRNOS = frozenset((99, 911, 990001, 40101004, 40101017, 40101032))
# 认证失败文本判定:不再用短子串("授权"/"认证"/"cookie" 等字样出现在
# 业务错误里也会命中,把文件/分享权限提示误判成登录失效)。英文按整词
# 匹配,中文只匹配明确的认证短语;结构化 errno 判定见 _response_auth。
_AUTH_TOKEN_PATTERN = re.compile(
    r"(?<![a-z0-9])"
    r"(?:"
    r"authentication|unauthori[sz]ed|not\s+logged\s+in|logged\s+out|"
    r"log\s*in|logout|cookie|session\s+expired|session\s+invalid|"
    r"sign\s+in|token"
    r")"
    r"(?![a-z0-9])",
    re.IGNORECASE,
)
_AUTH_PHRASES = (
    "登录已过期",
    "登录失效",
    "登录状态无效",
    "登录状态已失效",
    "未登录",
    "请重新登录",
    "请先登录",
    "请登录",
    "登录异常",
    "认证失败",
    "认证已过期",
    "认证已失效",
    "凭证已过期",
    "登录凭证无效",
    "授权已失效",
    "授权已过期",
    "已解除授权",
)


def _has_busy_errno(error: BaseException) -> bool:
    """服务端仍在处理上次提交(errno=990009),幂等重试一次是安全的。"""
    if getattr(error, "errno", None) == _P115_BUSY_OPERATION_ERRNO:
        return True
    return any(
        isinstance(argument, Mapping)
        and argument.get("errno") == _P115_BUSY_OPERATION_ERRNO
        for argument in getattr(error, "args", ())
    )
_IDEMPOTENT_MARKERS = (
    "already received",
    "already exist",
    "already added",
    "duplicate",
    "无需重复",
    "无需再次",
    "已经接收",
    "已接收",
    "已存在",
    "重复",
    "重复接收",
)
_FAILED_MARKERS = (
    "failed",
    "failure",
    "error",
    "invalid",
    "rejected",
    "失败",
    "错误",
    "拒绝",
)
_CANONICAL_TASK_STATUS = {
    RemoteStatus.ACCEPTED.value: RemoteStatus.ACCEPTED,
    RemoteStatus.SUBMITTED.value: RemoteStatus.SUBMITTED,
    RemoteStatus.DOWNLOADING.value: RemoteStatus.DOWNLOADING,
    RemoteStatus.AVAILABLE.value: RemoteStatus.AVAILABLE,
}
_AVAILABILITY_FILE_ID_UNAVAILABLE = "availability_file_id_unavailable"
_AVAILABILITY_PARENT_MISMATCH = "availability_parent_mismatch"
_AVAILABILITY_OBSERVER_UNAVAILABLE = "availability_observer_unavailable"
_AVAILABILITY_OBSERVER_TIMEOUT = "availability_observer_timeout"


class _AuthFailure(Exception):
    """Internal marker for a known authentication response."""


class _ShareListingFailure(Exception):
    """Internal marker carrying a stable pre-receive failure code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class P115Adapter:
    """Serialize p115client calls and rebuild its client when credentials change."""

    def __init__(
        self,
        cookie_provider: CookieProvider,
        target_cid: int | str,
        *,
        max_concurrency: int = 1,
        client_factory: Callable[[str], Any] | None = None,
        readonly_gateway: P115ReadOnlyDirectoryGateway | None = None,
        readonly_gateway_factory: Callable[
            [str], P115ReadOnlyDirectoryGateway
        ] | None = None,
        file_id_resolver: Callable[[Mapping[str, Any]], object] | None = None,
        request_timeout_seconds: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(target_cid, bool):
            raise TypeError("target_cid must be a non-negative integer")
        if isinstance(target_cid, int):
            if target_cid < 0:
                raise ValueError("target_cid must be a non-negative integer")
        elif not isinstance(target_cid, str) or not target_cid.isdigit():
            raise ValueError("target_cid must be a non-negative integer")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        self._request_timeout_seconds = request_timeout_seconds
        self._cookie_provider = cookie_provider
        self._target_cid = target_cid
        self._client_factory = client_factory or _default_client_factory
        if readonly_gateway is not None and readonly_gateway_factory is not None:
            raise ValueError("readonly_gateway_sources_conflict")
        self._readonly_gateway = readonly_gateway
        self._readonly_gateway_factory = readonly_gateway_factory
        self._file_id_resolver = file_id_resolver
        self._client: Any = None
        self._cookie: str | None = None
        self._client_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def submit_magnet(
        self, url: str, *, target_cid: str | None = None
    ) -> SubmissionResult:
        infohash = _magnet_infohash(url)
        if infohash is None:
            return _failed("invalid_magnet", "magnet URL is invalid")
        async with self._semaphore:
            client, missing = await self._client_for_operation()
            if missing:
                return _needs_auth()
            if client is None:
                return _failed("adapter_unavailable", "115 client unavailable")
            try:
                response = await self._call(
                    client,
                    "clouddownload_task_add_url",
                    {"url": url, "wp_path_id": self._resolve_target_cid(target_cid)},
                    timeout_seconds=self._request_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - remote outcome is opaque
                return _exception_result(error)
            return _submission_result(
                response, fallback_ref=f"{INFOHASH_REMOTE_REF_PREFIX}{infohash}"
            )

    async def save_share(
        self,
        url: str,
        password: str | None,
        *,
        target_cid: str | None = None,
    ) -> SubmissionResult:
        share = _share_target(url, password)
        if share is None:
            return _failed("invalid_share", "115 share URL is invalid")
        async with self._semaphore:
            client, missing = await self._client_for_operation()
            if missing:
                return _needs_auth()
            if client is None:
                return _failed("adapter_unavailable", "115 client unavailable")
            try:
                file_ids = await self._share_file_ids(client, share[0], share[1])
            except asyncio.CancelledError:
                raise
            except _AuthFailure:
                return _needs_auth()
            except _ShareListingFailure as error:
                return _failed(error.code, "115 share listing failed")
            except Exception:  # noqa: BLE001 - listing failure is not submission ambiguity
                return _failed("share_listing_failed", "115 share listing failed")
            if not file_ids:
                return _failed("empty_share", "115 share is empty")
            try:
                response = await self._call(
                    client,
                    "share_receive",
                    {
                        "share_code": share[0],
                        "receive_code": share[1],
                        "file_id": ",".join(file_ids),
                        "cid": self._resolve_target_cid(target_cid),
                    },
                    timeout_seconds=self._request_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - remote outcome is opaque
                return _exception_result(error)
            return _submission_result(response, allow_missing_ref=True)

    def _resolve_target_cid(self, target_cid: str | None) -> int | str:
        if target_cid is None:
            return self._target_cid
        if (
            not isinstance(target_cid, str)
            or not target_cid.isdigit()
            or target_cid.startswith("0")
        ):
            raise ValueError("target_cid must be a positive integer")
        return target_cid

    async def get_status(
        self, remote_ref: str, *, target_directory_id: str | None = None
    ) -> RemoteStatus | RemoteObservation | None:
        if not isinstance(remote_ref, str) or not remote_ref or len(remote_ref) > 255:
            return None
        async with self._semaphore:
            client, missing = await self._client_for_operation()
            if missing:
                return RemoteStatus.NEEDS_AUTH
            if client is None:
                return None
            expected_hash = _remote_infohash(remote_ref)
            if (
                remote_ref.startswith(INFOHASH_REMOTE_REF_PREFIX)
                and expected_hash is None
            ):
                return None
            for page in range(1, 101):
                try:
                    response = await self._call(
                        client,
                        "clouddownload_task_list",
                        {"page": page},
                        timeout_seconds=self._request_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - status is uncertain
                    return RemoteStatus.NEEDS_AUTH if _auth_exception(error) else None
                if _response_auth(response):
                    return RemoteStatus.NEEDS_AUTH
                if not isinstance(response, Mapping) or not _response_ok(response):
                    return None
                tasks = _task_records(response)
                for task in tasks:
                    if _task_matches(task, remote_ref, expected_hash):
                        status = _task_status(task)
                        if status is RemoteStatus.AVAILABLE:
                            return await self._observe_available_task(
                                task, target_directory_id=target_directory_id
                            )
                        return status
                if not tasks or _task_page_count(response) <= page:
                    break
            return None

    async def get_status_for_task(
        self, remote_ref: str, *, target_directory_id: str | None = None
    ) -> RemoteStatus | RemoteObservation | None:
        """Read status with the task's persisted target scope when available."""

        return await self.get_status(
            remote_ref, target_directory_id=target_directory_id
        )

    async def _observe_available_task(
        self,
        task: Mapping[str, Any],
        *,
        target_directory_id: str | None,
    ) -> RemoteObservation:
        file_id = _task_file_id(task, resolver=self._file_id_resolver)
        if file_id is None:
            return _uncertain_observation(_AVAILABILITY_FILE_ID_UNAVAILABLE)
        parent_id = _stable_directory_id(
            self._target_cid if target_directory_id is None else target_directory_id
        )
        if parent_id is None:
            return _uncertain_observation(_AVAILABILITY_PARENT_MISMATCH)
        try:
            gateway = self._readonly_gateway_for(parent_id, file_id)
        except Exception:  # noqa: BLE001 - gateway construction stays opaque
            return _uncertain_observation(_AVAILABILITY_OBSERVER_UNAVAILABLE)
        if gateway is None:
            return _uncertain_observation(_AVAILABILITY_OBSERVER_UNAVAILABLE)
        try:
            detail = await gateway.get_file_detail(file_id)
            parent_detail = await gateway.get_directory_detail(parent_id)
        except asyncio.CancelledError:
            raise
        except P115ReadOnlyGatewayError as error:
            return _uncertain_observation(
                _availability_observer_error_code(error.code)
            )
        except TimeoutError:
            return _uncertain_observation(_AVAILABILITY_OBSERVER_TIMEOUT)
        except Exception:  # noqa: BLE001 - remote detail stays redacted
            return _uncertain_observation(_AVAILABILITY_OBSERVER_UNAVAILABLE)
        if (
            getattr(detail, "file_id", None) != file_id
            or getattr(detail, "parent_id", None) != parent_id
            or getattr(detail, "is_directory", None) is not False
            or getattr(parent_detail, "directory_id", None) != parent_id
            or getattr(parent_detail, "is_directory", None) is not True
        ):
            return _uncertain_observation(_AVAILABILITY_PARENT_MISMATCH)
        return RemoteObservation(
            status=RemoteStatus.AVAILABLE,
            file_id=detail.file_id,
            parent_id=detail.parent_id,
            is_directory=detail.is_directory,
        )

    def _readonly_gateway_for(
        self, parent_id: str, file_id: str
    ) -> P115ReadOnlyDirectoryGateway | None:
        if self._readonly_gateway is not None:
            return self._readonly_gateway
        if self._readonly_gateway_factory is not None:
            return self._readonly_gateway_factory(parent_id)
        return P115ReadOnlyDirectoryGateway(
            self._cookie_provider,
            authorized_directory_ids=(parent_id,),
            authorized_file_ids=(file_id,),
        )

    async def aclose(self) -> None:
        async with self._client_lock:
            client = self._client
            self._client = None
            self._cookie = None
        await self._close_client(client)

    async def ensure_available(self) -> bool:
        """Build the client without making a remote API request."""
        async with self._semaphore:
            try:
                client, missing = await self._client_for_operation()
            except Exception:  # noqa: BLE001 - readiness must stay non-sensitive
                return False
            return not missing and client is not None

    async def validate_read_only(self) -> None:
        """Check credentials with one read-only task-list request."""
        async with self._semaphore:
            client, missing = await self._client_for_operation()
            if missing:
                raise P115NeedsAuthError
            if client is None:
                raise P115UnavailableError
            try:
                method = getattr(client, "clouddownload_task_list", None)
                if not callable(method):
                    raise P115UnavailableError
                response = method({"page": 1}, async_=True)
                if not inspect.isawaitable(response):
                    raise P115UnavailableError
                response = await self._await_native_probe(
                    response, timeout_seconds=self._request_timeout_seconds
                )
            except asyncio.CancelledError:
                raise
            except P115UnavailableError:
                raise
            except Exception as error:  # noqa: BLE001 - map remote failure
                if _auth_exception(error):
                    raise P115NeedsAuthError from None
                raise P115UnavailableError from None
            if not isinstance(response, Mapping):
                raise P115UnavailableError
            if _response_auth(response):
                raise P115NeedsAuthError
            if not _response_ok(response):
                raise P115UnavailableError

    @staticmethod
    async def _await_native_probe(response: Any, *, timeout_seconds: float) -> Any:
        """Drain a cancelled native probe before releasing the operation slot."""
        probe_task = asyncio.ensure_future(response)
        try:
            # 只读探针同样要有超时上限:原生客户端连接挂起时不得拖住调用方
            return await asyncio.wait_for(probe_task, timeout=timeout_seconds)
        except asyncio.CancelledError:
            probe_task.cancel()
            await asyncio.gather(probe_task, return_exceptions=True)
            raise

    async def validate_cookie(self, cookie: str) -> None:
        """Validate a candidate cookie with one native async read-only request."""
        normalized = normalize_cookie_text(cookie)
        if normalized is None:
            raise P115NeedsAuthError
        async with self._semaphore:
            try:
                client = await asyncio.to_thread(self._client_factory, normalized)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - stable validation mapping
                if _auth_exception(error):
                    raise P115NeedsAuthError from None
                raise P115UnavailableError from None
            try:
                method = getattr(client, "clouddownload_task_list", None)
                if not callable(method):
                    raise P115UnavailableError
                response = method({"page": 1}, async_=True)
                if not inspect.isawaitable(response):
                    raise P115UnavailableError
                response = await asyncio.wait_for(
                    response, timeout=self._request_timeout_seconds
                )
                if not isinstance(response, Mapping):
                    raise P115UnavailableError
                if _response_auth(response):
                    raise P115NeedsAuthError
                if not _response_ok(response):
                    raise P115UnavailableError
            except asyncio.CancelledError:
                raise
            except P115UnavailableError:
                raise
            except Exception as error:  # noqa: BLE001 - stable validation mapping
                if _auth_exception(error):
                    raise P115NeedsAuthError from None
                raise P115UnavailableError from None
            finally:
                await self._close_client(client)

    async def _client_for_operation(self) -> tuple[Any, bool]:
        cookie = self._cookie_provider.load()
        if not cookie:
            async with self._client_lock:
                old_client = self._client
                self._client = None
                self._cookie = None
            await self._close_client(old_client)
            return None, True
        async with self._client_lock:
            if self._client is not None and self._cookie == cookie:
                return self._client, False
            try:
                client = await asyncio.to_thread(self._client_factory, cookie)
            except Exception:  # noqa: BLE001 - client setup is intentionally opaque
                return None, False
            old_client = self._client
            self._client = client
            self._cookie = cookie
        await self._close_client(old_client)
        return client, False

    @staticmethod
    async def _close_client(client: Any) -> None:
        if client is None:
            return
        close = getattr(client, "close", None)
        if close is None:
            close = getattr(client, "aclose", None)
        if close is None:
            return
        try:
            result = await asyncio.to_thread(close)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - cleanup must not expose credentials
            return

    @staticmethod
    async def _call(
        client: Any,
        method_name: str,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> Any:
        """带超时与 990009 幂等重试的写/状态调用。

        修复前是裸 ``asyncio.to_thread``:115 侧连接挂起(无 RST)时线程永不返回,
        worker 的续租会持续续租、lease 永不过期,任务永久卡 SUBMITTING 且整个
        流水线停摆。990009 表示服务端仍在处理上次提交,重试一次即可拿到
        确定性结果(服务端按 infohash 去重,幂等安全)。
        """
        method = getattr(client, method_name)
        for attempt in range(2):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(method, payload),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                # 超时 = 服务端状态未知(可能已处理)。不重试提交,避免重复
                # 副作用;由调用方标记不确定,交给只读核对兜底。
                raise
            except Exception as error:
                if attempt or not _has_busy_errno(error):
                    raise
                await asyncio.sleep(_P115_BUSY_RETRY_DELAY_SECONDS)
        raise RuntimeError("unreachable")

    async def _share_file_ids(
        self, client: Any, share_code: str, receive_code: str
    ) -> list[str]:
        file_ids: list[str] = []
        offset = 0
        limit = 100
        had_explicit_more = False
        while offset <= MAX_SHARE_ITEMS:
            try:
                response = await self._call(
                    client,
                    "share_snap",
                    {
                        "share_code": share_code,
                        "receive_code": receive_code,
                        "cid": 0,
                        "limit": limit,
                        "offset": offset,
                    },
                    timeout_seconds=self._request_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - classify before receive
                if _auth_exception(error):
                    raise _AuthFailure from None
                raise _ShareListingFailure("share_listing_failed") from None
            if _response_auth(response):
                raise _AuthFailure
            if not isinstance(response, Mapping) or not _response_ok(response):
                raise _ShareListingFailure("share_listing_failed")
            total, has_more, next_offset = _share_page_info(response)
            if total is not None and total > MAX_SHARE_ITEMS:
                raise _ShareListingFailure("share_too_large")
            records = _share_records(response)
            if records is None:
                if total == 0:
                    return []
                raise _ShareListingFailure("malformed_share_listing")
            if not records:
                if offset == 0:
                    return []
                if had_explicit_more:
                    raise _ShareListingFailure("malformed_share_listing")
                return file_ids
            for record in records:
                if not isinstance(record, Mapping):
                    raise _ShareListingFailure("malformed_share_listing")
                item_id = _share_item_id(record)
                if item_id is None:
                    raise _ShareListingFailure("malformed_share_listing")
                if item_id not in file_ids:
                    file_ids.append(item_id)
            if len(file_ids) > MAX_SHARE_ITEMS:
                raise _ShareListingFailure("share_too_large")
            calculated_offset = offset + len(records)
            if total is not None and calculated_offset > total:
                raise _ShareListingFailure("malformed_share_listing")
            if next_offset is not None:
                if next_offset <= offset:
                    raise _ShareListingFailure("malformed_share_listing")
                if next_offset > MAX_SHARE_ITEMS:
                    raise _ShareListingFailure("share_too_large")
                next_cursor = next_offset
                had_explicit_more = True
            else:
                next_cursor = calculated_offset
                if has_more is True:
                    had_explicit_more = True
            known_continuation = (
                (has_more is True)
                or next_offset is not None
                or (total is not None and calculated_offset < total)
            )
            if known_continuation:
                had_explicit_more = True
            continuation = known_continuation or (has_more is None and total is None)
            if len(file_ids) == MAX_SHARE_ITEMS:
                if known_continuation:
                    raise _ShareListingFailure("share_too_large")
                if total is not None or has_more is False:
                    return file_ids
                offset = next_cursor
                continue
            if not continuation:
                return file_ids
            if next_cursor <= offset:
                raise _ShareListingFailure("malformed_share_listing")
            offset = next_cursor
        raise _ShareListingFailure("share_too_large")


P115ClientAdapter = P115Adapter


def _default_client_factory(cookie: str) -> Any:
    if version("p115client") != P115CLIENT_VERSION:
        raise RuntimeError("unsupported p115client version")
    from p115client import P115Client

    return P115Client(cookie, console_qrcode=False)


def _magnet_infohash(url: object) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    try:
        parsed = urlsplit(url)
        values = parse_qs(parsed.query, keep_blank_values=True).get("xt", ())
    except ValueError:
        return None
    if parsed.scheme.casefold() != "magnet" or parsed.netloc or parsed.fragment:
        return None
    for value in values:
        prefix, separator, value = value.partition(":")
        if (
            prefix.casefold() == "urn"
            and separator
            and value.casefold().startswith("btih:")
        ):
            infohash = value[5:]
            if _BTIH_PATTERN.fullmatch(infohash):
                if len(infohash) == 40:
                    return infohash.casefold()
                try:
                    return base64.b32decode(infohash, casefold=True).hex()
                except ValueError:
                    return None
    return None


def _share_target(url: object, password: str | None) -> tuple[str, str] | None:
    if not isinstance(url, str) or not isinstance(password, (str, type(None))):
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
        query = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or host is None
        or host.casefold() not in _SHARE_HOSTS
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    match = _SHARE_CODE_PATTERN.fullmatch(parsed.path)
    if match is None:
        return None
    embedded_password = query.get("password", [""])[0]
    receive_code = password if password is not None else embedded_password
    if any(ord(char) < 0x21 or char in "&;" for char in receive_code):
        return None
    return match.group(1), receive_code


def _submission_result(
    response: object,
    *,
    fallback_ref: str | None = None,
    allow_missing_ref: bool = False,
) -> SubmissionResult:
    if not isinstance(response, Mapping):
        return _uncertain("invalid_response")
    if _response_auth(response):
        return _needs_auth()
    remote_ref = _remote_reference(response)
    if _response_idempotent(response):
        if remote_ref is None:
            remote_ref = fallback_ref
        return SubmissionResult(status=RemoteStatus.ACCEPTED, remote_ref=remote_ref)
    if not _response_ok(response):
        return _failed("submit_rejected", "115 rejected the submission")
    if remote_ref is None and fallback_ref is not None:
        remote_ref = fallback_ref
    if remote_ref is None and not allow_missing_ref:
        return _uncertain("missing_remote_reference")
    return SubmissionResult(status=RemoteStatus.ACCEPTED, remote_ref=remote_ref)


def _response_ok(response: Mapping[str, Any]) -> bool:
    if not isinstance(response, Mapping):
        return False
    state = response.get("state")
    if state in (False, 0, "0", "false", "False"):
        return False
    if response.get("success") is False:
        return False
    code = response.get("code")
    return code in (None, 0, "0", 200, "200", True)


def _response_auth(response: Mapping[str, Any]) -> bool:
    if not isinstance(response, Mapping):
        return False
    if _known_message_has_auth_markers(response):
        return True
    return any(
        response.get(key) in _AUTH_ERRNOS
        or str(response.get(key)) in {str(value) for value in _AUTH_ERRNOS}
        for key in (
            "errno",
            "errNo",
            "errcode",
            "errCode",
            "code",
            "msg_code",
        )
    )


def _response_idempotent(response: Mapping[str, Any]) -> bool:
    return _known_message_has_markers(response, _IDEMPOTENT_MARKERS)


def _known_message_has_markers(
    response: Mapping[str, Any], markers: tuple[str, ...]
) -> bool:
    keys = (
        "error",
        "message",
        "msg",
        "error_msg",
        "error_message",
    )
    candidates: list[Mapping[str, Any]] = [response]
    data = response.get("data")
    if isinstance(data, Mapping):
        candidates.append(data)
    for candidate in candidates:
        for key in keys:
            value = candidate.get(key)
            if isinstance(value, str):
                lowered = value.casefold()
                if any(marker in lowered for marker in markers):
                    return True
    return False


def _text_has_auth_markers(text: str) -> bool:
    """精确的认证文本判定:英文整词 + 中文明确认证短语。"""
    lowered = text.casefold()
    if _AUTH_TOKEN_PATTERN.search(lowered):
        return True
    return any(phrase in lowered for phrase in _AUTH_PHRASES)


def _known_message_has_auth_markers(response: Mapping[str, Any]) -> bool:
    keys = (
        "error",
        "message",
        "msg",
        "error_msg",
        "error_message",
    )
    candidates: list[Mapping[str, Any]] = [response]
    data = response.get("data")
    if isinstance(data, Mapping):
        candidates.append(data)
    for candidate in candidates:
        for key in keys:
            value = candidate.get(key)
            if isinstance(value, str) and _text_has_auth_markers(value):
                return True
    return False


def _field_has_auth_markers(
    mapping: Mapping[str, Any], keys: tuple[str, ...]
) -> bool:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and _text_has_auth_markers(value):
            return True
    return False


def _remote_reference(response: Mapping[str, Any]) -> str | None:
    for item in _mapping_candidates(response):
        for key in ("task_id", "taskid", "taskId", "id"):
            value = item.get(key)
            if isinstance(value, (str, int)) and str(value):
                return str(value)
        for key in ("info_hash", "infohash", "hash"):
            value = item.get(key)
            if isinstance(value, str) and _INFOHASH_PATTERN.fullmatch(value):
                # Frozen fallback contract when p115 returns no task identifier.
                return f"{INFOHASH_REMOTE_REF_PREFIX}{value.casefold()}"
    return None


def _mapping_candidates(response: Mapping[str, Any]):
    yield response
    data = response.get("data")
    if isinstance(data, Mapping):
        yield data
        for key in ("task", "result", "item"):
            item = data.get(key)
            if isinstance(item, Mapping):
                yield item
    elif isinstance(data, list):
        yield from (item for item in data if isinstance(item, Mapping))


def _remote_infohash(remote_ref: str) -> str | None:
    if not remote_ref.startswith(INFOHASH_REMOTE_REF_PREFIX):
        return None
    value = remote_ref[len(INFOHASH_REMOTE_REF_PREFIX) :]
    return value.casefold() if _INFOHASH_PATTERN.fullmatch(value) else None


# 115 云下载任务列表接口未返回分页信息时的默认每页条数。
_TASK_LIST_PAGE_SIZE = 30


def _task_total_count(response: Mapping[str, Any]) -> int | None:
    """从响应 total/count 字段取任务总数;缺省时返回 None。"""
    candidates: list[Mapping[str, Any]] = [response]
    data = response.get("data")
    if isinstance(data, Mapping):
        candidates.append(data)
    for candidate in candidates:
        for key in ("total", "count", "total_count", "totalCount"):
            value = candidate.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
    return None


def _task_page_size(response: Mapping[str, Any]) -> int | None:
    """取响应里的每页条数;缺省时返回 None(由调用方用默认值兜底)。"""
    candidates: list[Mapping[str, Any]] = [response]
    data = response.get("data")
    if isinstance(data, Mapping):
        candidates.append(data)
    for candidate in candidates:
        for key in ("page_size", "pageSize", "limit"):
            value = candidate.get(key)
            if isinstance(value, int) and value > 0:
                return value
            if isinstance(value, str) and value.isdigit() and int(value) > 0:
                return int(value)
    return None


def _task_page_count(response: Mapping[str, Any]) -> int:
    candidates: list[Mapping[str, Any]] = [response]
    data = response.get("data")
    if isinstance(data, Mapping):
        candidates.append(data)
    for candidate in candidates:
        for key in ("page_count", "pageCount", "total_pages", "totalPages"):
            value = candidate.get(key)
            if isinstance(value, int) and value > 0:
                return value
            if isinstance(value, str) and value.isdigit() and int(value) > 0:
                return int(value)
    # 接口未返回页数字段时,按 total/count 推断总页数并及早截断,
    # 避免 get_status 逐页串行请求一直打到 101 页硬上限。
    total = _task_total_count(response)
    if total is not None:
        page_size = _task_page_size(response) or _TASK_LIST_PAGE_SIZE
        return max(1, (total + page_size - 1) // page_size)
    return 101


def _task_records(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = response.get("data")
    candidates: object = data if data is not None else response.get("tasks")
    if isinstance(candidates, Mapping):
        for key in ("tasks", "list", "items", "task_list", "data"):
            nested = candidates.get(key)
            if isinstance(nested, list):
                candidates = nested
                break
    if not isinstance(candidates, list):
        return []
    return [item for item in candidates if isinstance(item, Mapping)]


def _share_records(response: Mapping[str, Any]) -> list[object] | None:
    candidates: object = response.get("data")
    if isinstance(candidates, Mapping):
        found = False
        for key in ("list", "items", "files", "data"):
            if key in candidates:
                found = True
                candidates = candidates[key]
                break
        if not found:
            return None
    if not isinstance(candidates, list):
        found = False
        for key in ("list", "items", "files"):
            if key in response:
                found = True
                candidates = response[key]
                break
        if not found:
            return None
    return candidates if isinstance(candidates, list) else None


def _share_item_id(record: Mapping[str, Any]) -> str | None:
    value = record.get("fid")
    if value is None:
        value = record.get("cid")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    value = str(value)
    return value if value.isdigit() else None


def _share_page_info(
    response: Mapping[str, Any],
) -> tuple[int | None, bool | None, int | None]:
    candidates: list[Mapping[str, Any]] = [response]
    data = response.get("data")
    if isinstance(data, Mapping):
        candidates.append(data)
    total: int | None = None
    has_more: bool | None = None
    next_offset: int | None = None
    for candidate in candidates:
        for key in ("total", "count", "total_count", "totalCount"):
            if key not in candidate:
                continue
            value = candidate[key]
            if isinstance(value, bool):
                raise _ShareListingFailure("malformed_share_listing")
            if isinstance(value, int) and value >= 0:
                parsed_total = value
            elif isinstance(value, str) and value.isdigit():
                parsed_total = int(value)
            else:
                raise _ShareListingFailure("malformed_share_listing")
            if parsed_total > MAX_SHARE_ITEMS:
                raise _ShareListingFailure("share_too_large")
            if total is not None and total != parsed_total:
                raise _ShareListingFailure("malformed_share_listing")
            total = parsed_total
        for key in ("has_more", "hasMore", "more"):
            if key not in candidate:
                continue
            value = candidate.get(key)
            if not isinstance(value, bool):
                raise _ShareListingFailure("malformed_share_listing")
            if has_more is not None and has_more != value:
                raise _ShareListingFailure("malformed_share_listing")
            has_more = value
        for key in ("next_offset", "nextOffset"):
            if key not in candidate:
                continue
            value = candidate[key]
            if isinstance(value, bool):
                raise _ShareListingFailure("malformed_share_listing")
            if isinstance(value, int) and value >= 0:
                parsed_offset = value
            elif isinstance(value, str) and value.isdigit():
                parsed_offset = int(value)
            elif value is not None:
                raise _ShareListingFailure("malformed_share_listing")
            else:
                parsed_offset = None
            if next_offset is not None and next_offset != parsed_offset:
                raise _ShareListingFailure("malformed_share_listing")
            next_offset = parsed_offset
    return total, has_more, next_offset


def _task_matches(
    task: Mapping[str, Any], remote_ref: str, expected_hash: str | None = None
) -> bool:
    if remote_ref.startswith(INFOHASH_REMOTE_REF_PREFIX):
        expected = (
            expected_hash or remote_ref[len(INFOHASH_REMOTE_REF_PREFIX) :].casefold()
        )
        return any(
            isinstance(task.get(key), str) and task[key].casefold() == expected
            for key in ("info_hash", "infohash", "hash")
        )
    return any(
        isinstance(task.get(key), (str, int)) and str(task[key]) == remote_ref
        for key in ("task_id", "taskid", "taskId", "id")
    )


def _task_file_id(
    task: Mapping[str, Any],
    *,
    resolver: Callable[[Mapping[str, Any]], object] | None,
) -> str | None:
    if resolver is not None:
        try:
            resolved = resolver(task)
        except Exception:  # noqa: BLE001 - identity resolver is fail-closed
            return None
        return _stable_directory_id(resolved)
    values: list[str] = []
    for key in ("file_id", "fid"):
        if key not in task:
            continue
        value = _stable_directory_id(task[key])
        if value is None:
            return None
        values.append(value)
    if not values or any(value != values[0] for value in values[1:]):
        return None
    return values[0]


def _stable_directory_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    normalized = str(value)
    # "0" is the 115 root directory and is a legal target; the library gateway
    # already accepts it via allow_virtual_root, so availability verification
    # must not reject it here or root-targeted downloads can never become
    # AVAILABLE.
    if not normalized.isdigit():
        return None
    if normalized == "0":
        return normalized
    if normalized.startswith("0"):
        return None
    return normalized


def _uncertain_observation(error_code: str) -> RemoteObservation:
    return RemoteObservation(status=RemoteStatus.UNCERTAIN, error_code=error_code)


def _availability_observer_error_code(error_code: object) -> str:
    if isinstance(error_code, str) and "timeout" in error_code.casefold():
        return _AVAILABILITY_OBSERVER_TIMEOUT
    return _AVAILABILITY_OBSERVER_UNAVAILABLE


def _task_status(task: Mapping[str, Any]) -> RemoteStatus:
    if _known_message_has_auth_markers(task) or _field_has_auth_markers(
        task, ("status", "state")
    ):
        return RemoteStatus.NEEDS_AUTH
    if task.get("status") in (-1, "-1") or task.get("move") in (-1, "-1"):
        return RemoteStatus.FAILED
    if _known_message_has_markers(task, _FAILED_MARKERS) or _field_has_markers(
        task, ("status", "state"), _FAILED_MARKERS
    ):
        return RemoteStatus.FAILED
    for key in ("status", "state"):
        value = task.get(key)
        if isinstance(value, str):
            normalized = value.strip().casefold()
            status = _CANONICAL_TASK_STATUS.get(normalized)
            if status is not None:
                return status
    return RemoteStatus.UNCERTAIN


def _field_has_markers(
    mapping: Mapping[str, Any], keys: tuple[str, ...], markers: tuple[str, ...]
) -> bool:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str):
            lowered = value.casefold()
            if any(marker in lowered for marker in markers):
                return True
    return False


def _exception_result(error: Exception) -> SubmissionResult:
    if _auth_exception(error):
        return _needs_auth()
    if isinstance(error, TimeoutError):
        # 写调用挂起超过上限:远端可能已处理。与 submit_ambiguous 区分,
        # 便于只读核对路径优先对超时任务做确定性确认。
        return _uncertain("timeout")
    return _uncertain("submit_ambiguous")


def _auth_exception(error: Exception) -> bool:
    # 异常类名按 CamelCase 分词后整词匹配:裸 "auth" 子串会把
    # P115OpenAppAuthLimitExceeded(授权应用数上限,非登录失效)误判为
    # 认证失败;分词后 "Authentication"/"Login"/"Token" 仍能命中
    # p115client 的真实认证异常类。
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", type(error).__name__)
    return _text_has_auth_markers(name)


def _needs_auth() -> SubmissionResult:
    return SubmissionResult(
        status=RemoteStatus.NEEDS_AUTH,
        error_code="needs_auth",
        error_message="115 credentials are unavailable",
    )


def _failed(error_code: str, message: str) -> SubmissionResult:
    return SubmissionResult(
        status=RemoteStatus.FAILED,
        error_code=error_code,
        error_message=message,
    )


def _uncertain(error_code: str) -> SubmissionResult:
    return SubmissionResult(
        status=RemoteStatus.UNCERTAIN,
        error_code=error_code,
        error_message="115 submission outcome is uncertain",
    )

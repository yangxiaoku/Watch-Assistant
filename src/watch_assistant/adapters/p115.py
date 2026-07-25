"""Minimal, guarded adapter for the fixed p115client release."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable, Mapping
from importlib.metadata import version
from typing import Any
from urllib.parse import parse_qs, urlsplit

from watch_assistant.schemas import RemoteStatus, SubmissionResult
from watch_assistant.services.p115_credentials import CookieProvider

P115CLIENT_VERSION = "0.0.9.6.5.1"
INFOHASH_REMOTE_REF_PREFIX = "infohash:"
_BTIH_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[a-z2-7]{32})$", re.IGNORECASE)
_SHARE_CODE_PATTERN = re.compile(r"^/(?:s|share)/([A-Za-z0-9_-]+)/?$", re.IGNORECASE)
_INFOHASH_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_SHARE_HOSTS = frozenset(("115.com", "115cdn.com", "anxia.com"))
_AUTH_ERRNOS = frozenset((99, 911, 990001, 40101004, 40101017, 40101032))
_AUTH_MARKERS = (
    "auth",
    "login",
    "unauthor",
    "cookie",
    "登录",
    "认证",
    "授权",
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


class P115Adapter:
    """Serialize p115client calls and rebuild its client when credentials change."""

    def __init__(
        self,
        cookie_provider: CookieProvider,
        target_cid: int | str,
        *,
        max_concurrency: int = 1,
        client_factory: Callable[[str], Any] | None = None,
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
        self._cookie_provider = cookie_provider
        self._target_cid = target_cid
        self._client_factory = client_factory or _default_client_factory
        self._client: Any = None
        self._cookie: str | None = None
        self._client_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def submit_magnet(self, url: str) -> SubmissionResult:
        if _magnet_infohash(url) is None:
            return _failed("invalid_magnet", "magnet URL is invalid")
        async with self._semaphore:
            client, missing = await self._client_for_operation()
            if missing:
                return _needs_auth()
            if client is None:
                return _uncertain("adapter_unavailable")
            try:
                response = await self._call(
                    client,
                    "clouddownload_task_add_urls",
                    {"urls": url, "wp_path_id": self._target_cid},
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - remote outcome is opaque
                return _exception_result(error)
            return _submission_result(response)

    async def save_share(self, url: str, password: str | None) -> SubmissionResult:
        share = _share_target(url, password)
        if share is None:
            return _failed("invalid_share", "115 share URL is invalid")
        async with self._semaphore:
            client, missing = await self._client_for_operation()
            if missing:
                return _needs_auth()
            if client is None:
                return _uncertain("adapter_unavailable")
            payload = {
                "share_code": share[0],
                "receive_code": share[1],
                "file_id": "0",
                "cid": self._target_cid,
            }
            try:
                response = await self._call(client, "share_receive", payload)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - remote outcome is opaque
                return _exception_result(error)
            return _submission_result(response)

    async def get_status(self, remote_ref: str) -> RemoteStatus | None:
        if not isinstance(remote_ref, str) or not remote_ref or len(remote_ref) > 255:
            return None
        async with self._semaphore:
            client, missing = await self._client_for_operation()
            if missing:
                return RemoteStatus.NEEDS_AUTH
            if client is None:
                return None
            try:
                response = await self._call(
                    client, "clouddownload_task_list", {"page": 1}
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - status is uncertain
                return RemoteStatus.NEEDS_AUTH if _auth_exception(error) else None
            if _response_auth(response):
                return RemoteStatus.NEEDS_AUTH
            if not _response_ok(response):
                return None
            for task in _task_records(response):
                if _task_matches(task, remote_ref):
                    return _task_status(task)
            return None

    async def aclose(self) -> None:
        async with self._client_lock:
            client = self._client
            self._client = None
            self._cookie = None
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

    async def _client_for_operation(self) -> tuple[Any, bool]:
        cookie = self._cookie_provider.load()
        if not cookie:
            return None, True
        async with self._client_lock:
            if self._client is not None and self._cookie == cookie:
                return self._client, False
            try:
                client = await asyncio.to_thread(self._client_factory, cookie)
            except Exception:  # noqa: BLE001 - client setup is intentionally opaque
                return None, False
            self._client = client
            self._cookie = cookie
            return client, False

    @staticmethod
    async def _call(client: Any, method_name: str, payload: Mapping[str, Any]) -> Any:
        method = getattr(client, method_name)
        return await asyncio.to_thread(method, payload)


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
                return infohash.casefold()
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


def _submission_result(response: object) -> SubmissionResult:
    if not isinstance(response, Mapping):
        return _uncertain("invalid_response")
    if _response_auth(response):
        return _needs_auth()
    remote_ref = _remote_reference(response)
    if _response_idempotent(response):
        return SubmissionResult(status=RemoteStatus.ACCEPTED, remote_ref=remote_ref)
    if not _response_ok(response):
        return _failed("submit_rejected", "115 rejected the submission")
    if remote_ref is None:
        return _uncertain("missing_remote_reference")
    return SubmissionResult(status=RemoteStatus.ACCEPTED, remote_ref=remote_ref)


def _response_ok(response: Mapping[str, Any]) -> bool:
    state = response.get("state")
    if state in (False, 0, "0", "false", "False"):
        return False
    if response.get("success") is False:
        return False
    code = response.get("code")
    return code in (None, 0, "0", 200, "200", True)


def _response_auth(response: Mapping[str, Any]) -> bool:
    if _contains_markers(response, _AUTH_MARKERS):
        return True
    return any(
        response.get(key) in _AUTH_ERRNOS
        or str(response.get(key)) in {str(value) for value in _AUTH_ERRNOS}
        for key in ("errno", "errNo", "errcode", "errCode", "msg_code")
    )


def _response_idempotent(response: Mapping[str, Any]) -> bool:
    return _contains_markers(response, _IDEMPOTENT_MARKERS)


def _contains_markers(value: object, markers: tuple[str, ...]) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_markers(item, markers) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_markers(item, markers) for item in value)
    if isinstance(value, str):
        lowered = value.casefold()
        return any(marker in lowered for marker in markers)
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


def _task_matches(task: Mapping[str, Any], remote_ref: str) -> bool:
    if remote_ref.startswith(INFOHASH_REMOTE_REF_PREFIX):
        expected = remote_ref[len(INFOHASH_REMOTE_REF_PREFIX) :].casefold()
        return any(
            isinstance(task.get(key), str) and task[key].casefold() == expected
            for key in ("info_hash", "infohash", "hash")
        )
    return any(
        isinstance(task.get(key), (str, int)) and str(task[key]) == remote_ref
        for key in ("task_id", "taskid", "taskId", "id")
    )


def _task_status(task: Mapping[str, Any]) -> RemoteStatus:
    if _contains_markers(task, _AUTH_MARKERS):
        return RemoteStatus.NEEDS_AUTH
    if task.get("move") == -1 or task.get("status") == 2:
        return RemoteStatus.FAILED
    if _contains_markers(task, _FAILED_MARKERS):
        return RemoteStatus.FAILED
    return RemoteStatus.ACCEPTED


def _exception_result(error: Exception) -> SubmissionResult:
    if _auth_exception(error):
        return _needs_auth()
    return _uncertain("submit_ambiguous")


def _auth_exception(error: Exception) -> bool:
    return any(marker in type(error).__name__.casefold() for marker in _AUTH_MARKERS)


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

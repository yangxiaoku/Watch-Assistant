"""Explicit, one-shot C03 live runner.

This command is disabled unless ``--live`` and all four C03 environment gates
are present.  It reads only the explicitly supplied cookie path, creates one
fixed-version client, and prints only the probe's redacted public report.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import math
import os
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from importlib.metadata import version
from pathlib import Path

from watch_assistant.adapters.p115_c03_fixture_probe import (
    C03_CLEANUP_PLAN_ENV,
    C03_LIVE_ENV,
    C03_MANAGED_FIXTURE_ENV,
    C03_WRITE_ENABLED_ENV,
    MAX_LIST_CALLS,
    MAX_READ_CALLS,
    MAX_RUN_TIMEOUT_SECONDS,
    MAX_TOTAL_CALLS,
    C03CallBudget,
    C03ProbeReport,
    C03ProbeStatus,
    P115C03Transport,
    normalize_parent_id,
    run_p115_c03_fixture_probe,
)
from watch_assistant.adapters.p115_c03_live_transport import (
    EXPECTED_P115CLIENT_VERSION,
    P115C03CallExecutor,
    P115C03LiveTransport,
    P115ClientLike,
)
from watch_assistant.adapters.p115_library_write_contract import (
    OrganizationContractEvidence,
    OrganizationWriteCapability,
    P115OrganizationContract,
)

MAX_COOKIE_BYTES = 16 * 1024
MAX_AUTHORIZATION_BYTES = 8 * 1024
AUTHORIZATION_VERSION = 1
P115_BUSY_OPERATION_ERRNO = 990009
P115_BUSY_OPERATION_RETRY_DELAY_SECONDS = 3.0
_SAFE_NONCE = re.compile(r"[A-Za-z0-9._-]+")


def _c03_organization_contract() -> P115OrganizationContract:
    """Return the explicit contract used by the bounded C03 write probes."""

    capabilities = frozenset(
        {
            OrganizationWriteCapability.READ_SCOPE,
            OrganizationWriteCapability.MOVE,
            OrganizationWriteCapability.RENAME,
            OrganizationWriteCapability.RECYCLE,
            OrganizationWriteCapability.POSTCONDITION,
        }
    )
    return P115OrganizationContract(
        verified=True,
        timeout_enforced=True,
        capabilities=capabilities,
        evidence=OrganizationContractEvidence(
            evidence_id="c03-fixture-organization-v1",
            capabilities=capabilities,
            timeout_enforced=True,
        ),
    )


def run_live_probe(
    *,
    parent_id: object,
    cookie_path: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None = None,
    client_factory: Callable[..., P115ClientLike] | None = None,
    authorization_path: str | os.PathLike[str] | None = None,
    managed_parent_ids: Collection[object] | None = None,
    scope_validator: Callable[[str], bool] | None = None,
    call_executor: P115C03CallExecutor | None = None,
    clock: Callable[[], float] | None = None,
) -> C03ProbeReport:
    """Build exactly one injected live transport after all preflight gates."""

    environment = os.environ if env is None else env
    now = time.time if clock is None else clock
    try:
        normalized_parent_id = normalize_parent_id(parent_id)
    except (TypeError, ValueError):
        return _blocked("blocked_environment")
    gate_error = _gate_error(environment)
    if gate_error is not None:
        return _blocked("blocked_environment")
    if not _managed_scope_allows(
        normalized_parent_id,
        managed_parent_ids=managed_parent_ids,
        scope_validator=scope_validator,
    ):
        return _blocked("blocked_environment")
    if authorization_path is None or not _authorization_matches(
        authorization_path, normalized_parent_id, now=now
    ):
        return _blocked("blocked_environment")
    if not _supports_call_timeout(call_executor):
        return _blocked("blocked_environment")
    if _p115client_version() != EXPECTED_P115CLIENT_VERSION:
        return _blocked("blocked_environment")
    if client_factory is None:
        try:
            from p115client import P115Client
        except Exception:  # noqa: BLE001 - import details never cross the boundary
            return _blocked("blocked_environment")
        client_factory = P115Client
    if not _consume_authorization(authorization_path):
        return _blocked("blocked_environment")
    if cookie_path is None:
        return _blocked("blocked_environment")
    cookie = _read_cookie(cookie_path)
    if cookie is None:
        return _blocked("blocked_environment")
    try:
        client = client_factory(cookie, console_qrcode=False)
    except Exception:  # noqa: BLE001 - client details never cross the boundary
        return _blocked("blocked_environment")
    transport: P115C03Transport = P115C03LiveTransport(
        client,
        call_executor=(
            _p115client_timeout_executor if call_executor is None else call_executor
        ),
    )
    try:
        return asyncio.run(
            run_p115_c03_fixture_probe(
                transport=transport,
                parent_id=normalized_parent_id,
                env=environment,
                timeout_seconds=MAX_RUN_TIMEOUT_SECONDS,
                budget=C03CallBudget(
                    max_read_calls=MAX_READ_CALLS + 24,
                    max_list_calls=MAX_LIST_CALLS + 6,
                    max_total_calls=MAX_TOTAL_CALLS + 24,
                    allow_cleanup_reserve=True,
                ),
                live=True,
            )
        )
    except asyncio.CancelledError:
        return _blocked("cancelled", status=C03ProbeStatus.UNCERTAIN)
    except Exception:  # noqa: BLE001 - runner details never cross the boundary
        return _blocked("live_runner_failed", status=C03ProbeStatus.UNCERTAIN)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the explicitly gated C03 probe")
    parser.add_argument("--parent-id", required=True)
    parser.add_argument("--cookie-path")
    parser.add_argument("--authorization-path")
    parser.add_argument("--managed-scope-path")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    if not args.live:
        report = _blocked("live_flag_required")
    else:
        report = run_live_probe(
            parent_id=args.parent_id,
            cookie_path=args.cookie_path,
            authorization_path=args.authorization_path,
            managed_parent_ids=_read_managed_scope(args.managed_scope_path),
            call_executor=_p115client_timeout_executor,
        )
    print(json.dumps(report.to_public_dict(), ensure_ascii=True, sort_keys=True))
    return 0 if report.status is C03ProbeStatus.SUCCESS else 1


def _gate_error(env: Mapping[str, str]) -> str | None:
    checks = (
        (C03_WRITE_ENABLED_ENV, "write_disabled"),
        (C03_MANAGED_FIXTURE_ENV, "managed_fixture_required"),
        (C03_CLEANUP_PLAN_ENV, "cleanup_plan_required"),
        (C03_LIVE_ENV, "live_disabled"),
    )
    for name, error_code in checks:
        if env.get(name) != "1":
            return error_code
    return None


def _read_cookie(path: str | os.PathLike[str]) -> str | None:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if not raw or len(raw) > MAX_COOKIE_BYTES or b"\x00" in raw:
        return None
    try:
        cookie = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not cookie or "\n" in cookie or "\r" in cookie:
        return None
    return cookie


def _read_managed_scope(path: str | os.PathLike[str] | None) -> tuple[str, ...] | None:
    if path is None:
        return None
    try:
        raw = Path(path).read_bytes()
        if not raw or len(raw) > MAX_AUTHORIZATION_BYTES:
            return None
        value = json.loads(raw.decode("utf-8"))
        if isinstance(value, Mapping):
            value = value.get("parent_ids")
        if isinstance(value, (str, bytes)) or not isinstance(value, list):
            return None
        return tuple(value)
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None


def _managed_scope_allows(
    parent_id: str,
    *,
    managed_parent_ids: Collection[object] | None,
    scope_validator: Callable[[str], bool] | None,
) -> bool:
    if managed_parent_ids is None and scope_validator is None:
        return False
    if managed_parent_ids is not None:
        if isinstance(managed_parent_ids, (str, bytes)):
            return False
        try:
            normalized = {
                normalize_parent_id(candidate) for candidate in managed_parent_ids
            }
        except (TypeError, ValueError):
            return False
        if parent_id not in normalized:
            return False
    if scope_validator is not None:
        try:
            if scope_validator(parent_id) is not True:
                return False
        except Exception:  # noqa: BLE001 - validator details never cross boundary
            return False
    return True


def _supports_call_timeout(call_executor: P115C03CallExecutor | None) -> bool:
    if call_executor is None:
        return False
    try:
        parameter = inspect.signature(call_executor).parameters.get("timeout_seconds")
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }


def _authorization_matches(
    path: str | os.PathLike[str], parent_id: str, *, now: Callable[[], float]
) -> bool:
    try:
        raw = Path(path).read_bytes()
        if not raw or len(raw) > MAX_AUTHORIZATION_BYTES:
            return False
        artifact = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return False
    if not isinstance(artifact, Mapping) or set(artifact) != {
        "version",
        "parent_id",
        "expires_at",
        "nonce",
    }:
        return False
    try:
        version_value = artifact["version"]
        artifact_parent = normalize_parent_id(artifact["parent_id"])
        expires_at = artifact["expires_at"]
        nonce = artifact["nonce"]
        current_time = now()
        if (
            isinstance(version_value, bool)
            or version_value != AUTHORIZATION_VERSION
            or artifact_parent != parent_id
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
            or not isinstance(current_time, (int, float))
            or isinstance(current_time, bool)
            or not math.isfinite(float(current_time))
            or float(expires_at) <= float(current_time)
            or not isinstance(nonce, str)
            or not nonce
            or len(nonce) > 256
            or _SAFE_NONCE.fullmatch(nonce) is None
        ):
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    return True


def _consume_authorization(path: str | os.PathLike[str]) -> bool:
    marker = Path(f"{path}.consumed")
    try:
        descriptor = os.open(
            marker,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
    except OSError:
        return False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(b"consumed\n")
    except OSError:
        return False
    return True


def _p115client_version() -> str | None:
    try:
        return version("p115client")
    except Exception:  # noqa: BLE001 - package details stay behind the gate
        return None


def _p115client_timeout_executor(method, payload, *, timeout_seconds: float):
    """Use p115client's documented request hook with a per-call urllib3 timeout."""

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise RuntimeError("invalid_timeout")
    try:
        from urllib3_future_request import request as urllib3_request
    except Exception as error:
        raise RuntimeError("timeout_transport_unavailable") from error

    def request_with_timeout(*, async_: bool = False, **request_kwargs):
        if async_:
            raise RuntimeError("async_transport_unsupported")
        request_kwargs["timeout"] = float(timeout_seconds)
        request_kwargs["retries"] = False
        return urllib3_request(async_=False, **request_kwargs)

    for attempt in range(2):
        try:
            return method(payload, async_=False, request=request_with_timeout)
        except Exception as error:
            if attempt or not _has_p115_errno(error, P115_BUSY_OPERATION_ERRNO):
                raise
            time.sleep(P115_BUSY_OPERATION_RETRY_DELAY_SECONDS)


def _has_p115_errno(error: BaseException, expected: int) -> bool:
    """Read only the structured errno field, never exception text."""

    try:
        structured_errno = getattr(error, "errno", None)
    except Exception:  # noqa: BLE001 - error details stay behind the boundary
        structured_errno = None
    if structured_errno == expected:
        return True
    for argument in getattr(error, "args", ()):
        if isinstance(argument, Mapping) and argument.get("errno") == expected:
            return True
    return False


def _blocked(
    error_code: str, *, status: C03ProbeStatus = C03ProbeStatus.BLOCKED
) -> C03ProbeReport:
    return C03ProbeReport(
        status=status,
        fixture_fingerprint=None,
        steps=(),
        write_calls=0,
        read_calls=0,
        list_calls=0,
        page_calls=0,
        cleanup="not_started"
        if status is C03ProbeStatus.BLOCKED
        else "not_attempted_uncertain",
        error_code=error_code,
    )


if __name__ == "__main__":
    raise SystemExit(main())

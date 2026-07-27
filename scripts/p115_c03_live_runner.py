"""Explicit, one-shot C03 live runner.

This command is disabled unless ``--live`` and all four C03 environment gates
are present.  It reads only the explicitly supplied cookie path, creates one
fixed-version client, and prints only the probe's redacted public report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from watch_assistant.adapters.p115_c03_fixture_probe import (
    C03_CLEANUP_PLAN_ENV,
    C03_LIVE_ENV,
    C03_MANAGED_FIXTURE_ENV,
    C03_WRITE_ENABLED_ENV,
    C03ProbeReport,
    C03ProbeStatus,
    P115C03Transport,
    normalize_parent_id,
    run_p115_c03_fixture_probe,
)
from watch_assistant.adapters.p115_c03_live_transport import (
    EXPECTED_P115CLIENT_VERSION,
    P115C03LiveTransport,
    P115ClientLike,
)

MAX_COOKIE_BYTES = 16 * 1024


def run_live_probe(
    *,
    parent_id: object,
    cookie_path: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None = None,
    client_factory: Callable[..., P115ClientLike] | None = None,
) -> C03ProbeReport:
    """Build exactly one injected live transport after all preflight gates."""

    environment = os.environ if env is None else env
    try:
        normalized_parent_id = normalize_parent_id(parent_id)
    except (TypeError, ValueError):
        return _blocked("invalid_parent_id")
    gate_error = _gate_error(environment)
    if gate_error is not None:
        return _blocked(gate_error)
    if cookie_path is None:
        return _blocked("cookie_path_required")
    if _p115client_version() != EXPECTED_P115CLIENT_VERSION:
        return _blocked("unsupported_p115client_version")
    cookie = _read_cookie(cookie_path)
    if cookie is None:
        return _blocked("cookie_unavailable")
    if client_factory is None:
        try:
            from p115client import P115Client
        except Exception:  # noqa: BLE001 - import details never cross the boundary
            return _blocked("p115client_unavailable")
        client_factory = P115Client
    try:
        client = client_factory(cookie, console_qrcode=False)
    except Exception:  # noqa: BLE001 - client details never cross the boundary
        return _blocked("client_unavailable")
    transport: P115C03Transport = P115C03LiveTransport(client)
    try:
        return asyncio.run(
            run_p115_c03_fixture_probe(
                transport=transport,
                parent_id=normalized_parent_id,
                env=environment,
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
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    if not args.live:
        report = _blocked("live_flag_required")
    else:
        report = run_live_probe(
            parent_id=args.parent_id,
            cookie_path=args.cookie_path,
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


def _p115client_version() -> str | None:
    try:
        return version("p115client")
    except PackageNotFoundError:
        return None


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

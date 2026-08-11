"""Gated read-only live STRM output acceptance.

The runner is deliberately separate from the application API and worker.  It
uses only the bounded P115 read gateway, stores its index and manifest in a
temporary SQLite database, and writes STRM files below a temporary directory.
No remote write-capable client or cleanup operation is imported here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from importlib.metadata import version
from pathlib import Path

from watch_assistant.adapters.p115_library_gateway import (
    P115ReadOnlyDirectoryGateway,
    P115TransportFactory,
)
from watch_assistant.adapters.p115_library_transport import (
    EXPECTED_P115CLIENT_VERSION,
    P115ReadOnlyTransportProtocol,
    create_p115_readonly_transport,
)
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import MediaLibrary
from watch_assistant.services.library_index import (
    LibraryIndexService,
    LibraryScanResult,
    ScanRunState,
)
from watch_assistant.services.p115_credentials import CookieProvider
from watch_assistant.services.strm_manifest import (
    StrmGenerationSummary,
    StrmManifestService,
)

LIVE_ENV = "WATCH_ASSISTANT_P115_STRM_READONLY_LIVE"
LIBRARY_ID = "strm-readonly-live"
PAGE_SIZE = 1
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_RUN_TIMEOUT_SECONDS = 2 * 60 * 60
DEFAULT_MAX_DIRECTORIES = 100_000
PLAYBACK_URL_PREFIX = "http://127.0.0.1:8115/api/v1/strm/play"
# 只读验收路径使用 app-first 读接口(fs_files_app/fs_info_app);旧接口
# 仅在 app 端点返回 405 时回退。两种接口都纳入已核验的读方法集合。
ALLOWED_REMOTE_METHODS = (
    "fs_files",
    "fs_info",
    "fs_files_app",
    "fs_info_app",
)

_ERROR_MESSAGES = {
    "live_gate_closed": "只读 STRM 实时验收开关未开启。",
    "invalid_scope": "远端根目录标识不符合稳定范围要求。",
    "credential_unavailable": "只读验收凭据不可用。",
    "client_version_unverified": "p115client 固定版本未验证。",
    "invalid_configuration": "只读 STRM 验收参数无效。",
    "run_timeout": "只读 STRM 验收超时，已阻止后续输出。",
    "scan_incomplete": "远端递归扫描未完成，已阻止 STRM 输出。",
    "scan_evidence_invalid": "远端扫描完整性证据无效，已阻止 STRM 输出。",
    "full_generation_failed": "STRM 全量临时输出失败，已 fail-closed。",
    "incremental_generation_failed": "STRM 增量临时输出失败，已 fail-closed。",
    "readonly_method_violation": "远端调用超出只读方法白名单，已 fail-closed。",
    "temporary_boundary_invalid": "临时数据库或输出根边界校验失败，已 fail-closed。",
    "readonly_acceptance_failed": "只读 STRM 验收失败，已 fail-closed。",
}


class _ReadonlyAcceptanceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _ReadCallLedger:
    """Count calls crossing the only transport methods used by this runner."""

    def __init__(self) -> None:
        self._method_counts: dict[str, int] = {}

    def record(self, method: str) -> None:
        self._method_counts[method] = self._method_counts.get(method, 0) + 1

    @property
    def read_calls(self) -> int:
        return sum(self._method_counts.values())

    @property
    def observed_methods(self) -> tuple[str, ...]:
        return tuple(sorted(self._method_counts))

    @property
    def method_counts(self) -> dict[str, int]:
        return dict(self._method_counts)

    @property
    def write_calls(self) -> int:
        return 0


class _CountingReadOnlyTransport:
    """Expose no method other than the verified read operations."""

    def __init__(
        self, transport: P115ReadOnlyTransportProtocol, ledger: _ReadCallLedger
    ) -> None:
        self._transport = transport
        self._ledger = ledger

    def __repr__(self) -> str:
        return (
            "<CountingReadOnlyTransport "
            "methods='fs_files,fs_info,fs_files_app,fs_info_app'>"
        )

    async def fs_files_app(self, payload, *, timeout_seconds: float) -> object:
        self._ledger.record("fs_files_app")
        return await self._transport.fs_files_app(
            payload, timeout_seconds=timeout_seconds
        )

    async def fs_files(self, payload, *, timeout_seconds: float) -> object:
        self._ledger.record("fs_files")
        return await self._transport.fs_files(
            payload, timeout_seconds=timeout_seconds
        )

    async def fs_info_app(self, payload, *, timeout_seconds: float) -> object:
        self._ledger.record("fs_info_app")
        return await self._transport.fs_info_app(
            payload, timeout_seconds=timeout_seconds
        )

    async def fs_info(self, payload, *, timeout_seconds: float) -> object:
        self._ledger.record("fs_info")
        return await self._transport.fs_info(
            payload, timeout_seconds=timeout_seconds
        )


def run_acceptance(
    *,
    root_id: object,
    cookie_path: str | os.PathLike[str] | None,
    environment: Mapping[str, str] | None = None,
    credential_source: object | None = None,
    transport_factory: P115TransportFactory = create_p115_readonly_transport,
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS,
    max_directories: int = DEFAULT_MAX_DIRECTORIES,
) -> dict[str, object]:
    """Run the temporary, read-only STRM acceptance sequence.

    ``credential_source`` and ``transport_factory`` are injection seams for
    contract tests.  The default path still constructs only the fixed
    read-only transport after all gates have passed.
    """

    env = os.environ if environment is None else environment
    normalized_root_id = _stable_root_id(root_id)
    if env.get(LIVE_ENV) != "1":
        return _blocked("live_gate_closed", root_id=normalized_root_id)
    if normalized_root_id is None:
        return _blocked("invalid_scope")
    if cookie_path is None and credential_source is None:
        return _blocked("credential_unavailable", root_id=normalized_root_id)
    if (
        not _positive_timeout(request_timeout_seconds)
        or not _positive_timeout(run_timeout_seconds)
        or not _valid_directory_limit(max_directories)
    ):
        return _blocked("invalid_configuration", root_id=normalized_root_id)
    try:
        if version("p115client") != EXPECTED_P115CLIENT_VERSION:
            return _blocked(
                "client_version_unverified", root_id=normalized_root_id
            )
    except Exception:  # noqa: BLE001 - package metadata stays behind the boundary
        return _blocked("client_version_unverified", root_id=normalized_root_id)

    ledger = _ReadCallLedger()

    def counted_factory(credential: str) -> P115ReadOnlyTransportProtocol:
        transport = transport_factory(credential)
        return _CountingReadOnlyTransport(transport, ledger)

    source = (
        credential_source
        if credential_source is not None
        else CookieProvider(cookie_path)  # type: ignore[arg-type]
    )
    try:
        return asyncio.run(
            asyncio.wait_for(
                _run(
                    root_id=normalized_root_id,
                    source=source,
                    transport_factory=counted_factory,
                    ledger=ledger,
                    request_timeout_seconds=float(request_timeout_seconds),
                    max_directories=max_directories,
                ),
                timeout=float(run_timeout_seconds),
            )
        )
    except TimeoutError:
        return _blocked("run_timeout", root_id=normalized_root_id, ledger=ledger)
    except Exception:  # noqa: BLE001 - provider and local details stay private
        return _blocked(
            "readonly_acceptance_failed", root_id=normalized_root_id, ledger=ledger
        )


async def _run(
    *,
    root_id: str,
    source: object,
    transport_factory: P115TransportFactory,
    ledger: _ReadCallLedger,
    request_timeout_seconds: float,
    max_directories: int,
) -> dict[str, object]:
    temporary = tempfile.TemporaryDirectory(prefix="wa-strm-readonly-live-")
    database = None
    try:
        base = Path(temporary.name).resolve()
        database_path = base / "acceptance.db"
        output_root = base / "strm-output"
        database = create_database(f"sqlite+aiosqlite:///{database_path}")
        await initialize_database(database.engine)
        async with database.session_factory() as session:
            session.add(
                MediaLibrary(
                    id=LIBRARY_ID,
                    name="STRM 只读临时验收",
                    root_directory_id=root_id,
                    scope_verified=True,
                    enabled=True,
                    revision=1,
                )
            )
            await session.commit()

        gateway = P115ReadOnlyDirectoryGateway(
            source,  # type: ignore[arg-type]
            transport_factory,
            authorized_directory_ids=(root_id,),
            request_timeout_seconds=request_timeout_seconds,
        )
        index = LibraryIndexService(
            database.session_factory,
            gateway,
            library_id=LIBRARY_ID,
            root_directory_id=root_id,
            page_size=PAGE_SIZE,
            hydrate_file_details=True,
        )
        manifest = StrmManifestService(database.session_factory)

        initial_scan = await index.scan_tree("strm-readonly-initial")
        _require_complete_scan(initial_scan)
        full = await manifest.generate(
            LIBRARY_ID,
            source_scan_run_id=initial_scan.run_id,
            output_root=output_root,
            playback_url_prefix=PLAYBACK_URL_PREFIX,
        )
        if full.failed:
            raise _ReadonlyAcceptanceError("full_generation_failed")

        current_scan = await index.scan_tree("strm-readonly-incremental")
        _require_complete_scan(current_scan)
        incremental = await manifest.incremental(
            LIBRARY_ID,
            source_scan_run_id=current_scan.run_id,
            output_root=output_root,
            playback_url_prefix=PLAYBACK_URL_PREFIX,
            retire_removed=False,
        )

        if incremental.failed:
            raise _ReadonlyAcceptanceError("incremental_generation_failed")
        if any(
            method not in ALLOWED_REMOTE_METHODS
            for method in ledger.observed_methods
        ):
            raise _ReadonlyAcceptanceError("readonly_method_violation")
        if not _temporary_path(output_root, base) or not _temporary_path(
            database_path, base
        ):
            raise _ReadonlyAcceptanceError("temporary_boundary_invalid")
        return _success_report(
            root_id=root_id,
            initial_scan=initial_scan,
            current_scan=current_scan,
            full=full,
            incremental=incremental,
            ledger=ledger,
        )
    except _ReadonlyAcceptanceError as error:
        return _blocked(error.code, root_id=root_id, ledger=ledger)
    except Exception:  # noqa: BLE001 - provider and local details stay private
        return _blocked(
            "readonly_acceptance_failed", root_id=root_id, ledger=ledger
        )
    finally:
        try:
            if database is not None:
                await database.engine.dispose()
        finally:
            temporary.cleanup()


def _require_complete_scan(result: LibraryScanResult) -> None:
    if (
        result.state is not ScanRunState.COMPLETED
        or result.complete is not True
        or result.snapshot_revision is None
        or result.pages_read < 1
    ):
        raise _ReadonlyAcceptanceError("scan_incomplete")


def _success_report(
    *,
    root_id: str,
    initial_scan: LibraryScanResult,
    current_scan: LibraryScanResult,
    full: StrmGenerationSummary,
    incremental: StrmGenerationSummary,
    ledger: _ReadCallLedger,
) -> dict[str, object]:
    return {
        "status": "success",
        "write_started": False,
        "error_code": None,
        "error_message": None,
        "complete": True,
        "root_identity_verified": True,
        "scope": {
            "mode": "single_configured_root_recursive",
            "root_id": root_id,
            "recursive": True,
            "page_size": PAGE_SIZE,
        },
        "initial_scan": _scan_evidence(initial_scan),
        "incremental_scan": _scan_evidence(current_scan),
        "full_output": _generation_evidence(full, retire_removed=False),
        "incremental_output": _generation_evidence(
            incremental, retire_removed=False
        ),
        "remote": _remote_evidence(ledger),
        "read_calls": ledger.read_calls,
        "write_calls": ledger.write_calls,
        "remote_write_calls": ledger.write_calls,
        "output_root_is_temporary": True,
        "database_is_temporary": True,
        "remote_rename": False,
        "remote_restore": False,
        "remote_cleanup": False,
        "remote_playback": False,
        "cleanup_preview": False,
        "write_boundary": "read_only_fail_closed",
        "fail_closed": True,
    }


def _scan_evidence(result: LibraryScanResult) -> dict[str, object]:
    return {
        "state": result.state.value,
        "complete": result.complete,
        "pages_read": result.pages_read,
        "items_seen": result.items_seen,
        "snapshot_revision": result.snapshot_revision,
        "added_count": result.added_count,
        "changed_count": result.changed_count,
        "removed_count": result.removed_count,
        "error_code": result.error_code,
    }


def _generation_evidence(
    result: StrmGenerationSummary, *, retire_removed: bool
) -> dict[str, object]:
    return {
        "generated": result.generated,
        "unchanged": result.unchanged,
        "skipped": result.skipped,
        "failed": result.failed,
        "retired": result.retired,
        "retire_removed": retire_removed,
    }


def _remote_evidence(ledger: _ReadCallLedger) -> dict[str, object]:
    return {
        "allowed_methods": list(ALLOWED_REMOTE_METHODS),
        "observed_methods": list(ledger.observed_methods),
        "read_method_counts": ledger.method_counts,
        "read_calls": ledger.read_calls,
        "write_calls": ledger.write_calls,
        "remote_write_calls": ledger.write_calls,
    }


def _blocked(
    code: str,
    *,
    root_id: str | None = None,
    ledger: _ReadCallLedger | None = None,
) -> dict[str, object]:
    calls = ledger or _ReadCallLedger()
    return {
        "status": "blocked",
        "error_code": code,
        "error_message": _ERROR_MESSAGES.get(
            code, "只读 STRM 验收失败，已 fail-closed。"
        ),
        "complete": False,
        "root_identity_verified": False,
        "scope": {
            "mode": "single_configured_root_recursive",
            "root_id": root_id,
            "recursive": True,
            "page_size": PAGE_SIZE,
        },
        "remote": _remote_evidence(calls),
        "read_calls": calls.read_calls,
        "write_calls": calls.write_calls,
        "remote_write_calls": calls.write_calls,
        "output_root_is_temporary": True,
        "database_is_temporary": True,
        "remote_rename": False,
        "remote_restore": False,
        "remote_cleanup": False,
        "remote_playback": False,
        "cleanup_preview": False,
        "write_boundary": "read_only_fail_closed",
        "fail_closed": True,
    }


def _stable_root_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    normalized = str(value)
    if not normalized.isdigit() or normalized.startswith("0"):
        return None
    return normalized


def _positive_timeout(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _valid_directory_limit(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= DEFAULT_MAX_DIRECTORIES
    )


def _temporary_path(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the gated read-only live STRM temporary output acceptance"
    )
    parser.add_argument("--root-id", required=True)
    parser.add_argument("--cookie-path", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    environment = dict(os.environ)
    if args.live:
        environment[LIVE_ENV] = "1"
    else:
        environment.pop(LIVE_ENV, None)
    report = run_acceptance(
        root_id=args.root_id,
        cookie_path=args.cookie_path,
        environment=environment,
    )
    encoded = json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

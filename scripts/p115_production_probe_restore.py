"""Restore the one unresolved production probe name after read-only reconciliation.

This command has a preview phase and a separate one-shot write phase.  The
write phase requires a fresh authorization artifact and a matching digest.  It
renames exactly one uniquely discovered ``wa-prod-probe-*`` file back to the
single old name reported by the verified rename-history endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from collections import deque
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from watch_assistant.adapters.p115_c03_live_transport import EXPECTED_P115CLIENT_VERSION
from watch_assistant.adapters.p115_library_gateway import P115ReadOnlyDirectoryGateway
from watch_assistant.adapters.p115_organization_transport import (
    OrganizationObjectIntent,
    P115OrganizationTransportError,
    create_live_p115_organization_transport,
)
from watch_assistant.services.organization_execution_contract import RemoteObjectState
from watch_assistant.services.organization_executor import OrganizationTransportStatus

try:
    from scripts.p115_c03_live_runner import (
        C03_CLEANUP_PLAN_ENV,
        C03_LIVE_ENV,
        C03_MANAGED_FIXTURE_ENV,
        C03_WRITE_ENABLED_ENV,
        _authorization_matches,
        _consume_authorization,
        _p115client_timeout_executor,
        _read_cookie,
    )
except ModuleNotFoundError:
    from p115_c03_live_runner import (  # type: ignore[no-redef]
        C03_CLEANUP_PLAN_ENV,
        C03_LIVE_ENV,
        C03_MANAGED_FIXTURE_ENV,
        C03_WRITE_ENABLED_ENV,
        _authorization_matches,
        _consume_authorization,
        _p115client_timeout_executor,
        _read_cookie,
    )

MAX_PAGES = 100_000
MAX_HISTORY_RECORDS = 100
PROBE_PREFIX = "wa-prod-probe-"
SAFE_NAME = re.compile(r"[^/\\\x00]+")
_GATES = (
    C03_WRITE_ENABLED_ENV,
    C03_MANAGED_FIXTURE_ENV,
    C03_CLEANUP_PLAN_ENV,
    C03_LIVE_ENV,
)


class RestoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProbeCandidate:
    file_id: str
    parent_id: str
    name: str


class FileCredential:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> str:
        value = _read_cookie(self._path)
        if value is None:
            raise RestoreError("credential_unavailable")
        return value


async def _discover(root_id: str, cookie_path: Path) -> tuple[ProbeCandidate, int]:
    gateway = P115ReadOnlyDirectoryGateway(
        FileCredential(cookie_path),
        authorized_directory_ids=(root_id,),
        request_timeout_seconds=30,
    )
    queue: deque[str] = deque([root_id])
    visited: set[str] = set()
    candidates: list[ProbeCandidate] = []
    pages = 0
    while queue:
        directory_id = queue.popleft()
        if directory_id in visited:
            continue
        visited.add(directory_id)
        page_number = 1
        total: int | None = None
        while True:
            if pages >= MAX_PAGES:
                raise RestoreError("pagination_limit")
            page = await gateway.list_directory(
                directory_id, page=page_number, page_size=1
            )
            pages += 1
            if page.state.value != "complete" or page.scan_complete is False:
                raise RestoreError("inventory_incomplete")
            if total is None:
                total = page.total
            elif total != page.total:
                raise RestoreError("inventory_changed")
            for entry in page.items:
                if entry.is_directory and entry.directory_id is not None:
                    queue.append(entry.directory_id)
                elif (
                    not entry.is_directory
                    and entry.file_id is not None
                    and entry.parent_id is not None
                    and entry.name.startswith(PROBE_PREFIX)
                    and SAFE_NAME.fullmatch(entry.name) is not None
                ):
                    candidates.append(
                        ProbeCandidate(entry.file_id, entry.parent_id, entry.name)
                    )
            if page.terminal is True or page.has_more is False:
                break
            if page.terminal is False or page.has_more is True:
                page_number += 1
                continue
            raise RestoreError("pagination_unverified")
    if len(candidates) != 1:
        raise RestoreError("probe_candidate_not_unique")
    return candidates[0], pages


def _history_records(response: object) -> list[Mapping[str, object]] | None:
    if not isinstance(response, Mapping) or response.get("state") is not True:
        return None
    for key in ("errno", "errNo"):
        if key in response and response[key] not in (0, "0", ""):
            return None
    data = response.get("data")
    if isinstance(data, Mapping):
        data = data.get("list", data.get("items", data.get("records")))
    if not isinstance(data, list) or len(data) > MAX_HISTORY_RECORDS:
        return None
    if any(not isinstance(record, Mapping) for record in data):
        return None
    return data


def _single_id(record: Mapping[str, object], names: tuple[str, ...]) -> str | None:
    values: list[str] = []
    for name in names:
        if name not in record:
            continue
        value = record[name]
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return None
        normalized = str(value)
        if not normalized.isdigit() or normalized.startswith("0"):
            return None
        values.append(normalized)
    if not values or any(value != values[0] for value in values[1:]):
        return None
    return values[0]


def _history_original_name(
    response: object, *, file_id: str, temporary_name: str
) -> str | None:
    records = _history_records(response)
    if records is None:
        raise RestoreError("rename_history_unverified")
    names: set[str] = set()
    for record in records:
        record_id = _single_id(record, ("file_id", "fid", "object_id"))
        if any(key in record for key in ("file_id", "fid", "object_id")) and record_id != file_id:
            continue
        value = record.get("file_old_name")
        if (
            isinstance(value, str)
            and value != temporary_name
            and len(value) <= 255
            and SAFE_NAME.fullmatch(value) is not None
        ):
            names.add(value)
    if len(names) != 1:
        raise RestoreError("rename_history_not_unique")
    return next(iter(names))


def _plan_digest(candidate: ProbeCandidate, original_name: str) -> str:
    payload = {
        "file_id": candidate.file_id,
        "parent_id": candidate.parent_id,
        "temporary_name": candidate.name,
        "original_name": original_name,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()


def _scope_contains(path: Path, root_id: str) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    values = value.get("parent_ids") if isinstance(value, Mapping) else value
    return not isinstance(values, (str, bytes)) and isinstance(values, Collection) and root_id in {
        str(item) for item in values
    }


def _live_gate(root_id: str, cookie_path: Path, auth_path: Path, scope_path: Path) -> None:
    if any(os.environ.get(name) != "1" for name in _GATES):
        raise RestoreError("write_gate_closed")
    if not _scope_contains(scope_path, root_id):
        raise RestoreError("scope_unverified")
    if not _authorization_matches(auth_path, root_id, now=time.time):
        raise RestoreError("authorization_invalid")
    if _p115client_version() != EXPECTED_P115CLIENT_VERSION:
        raise RestoreError("client_version_unverified")
    if _read_cookie(cookie_path) is None:
        raise RestoreError("credential_unavailable")


async def _read_history(client: object, candidate: ProbeCandidate) -> str:
    method = getattr(client, "fs_history_rename_list_app", None)
    if not callable(method):
        raise RestoreError("rename_history_unavailable")
    response = _p115client_timeout_executor(
        method,
        {"file_ids": candidate.file_id, "offset": 0, "limit": MAX_HISTORY_RECORDS},
        timeout_seconds=30,
    )
    value = _history_original_name(
        response, file_id=candidate.file_id, temporary_name=candidate.name
    )
    if value is None:
        raise RestoreError("rename_history_not_unique")
    return value


async def _restore(
    client: object,
    candidate: ProbeCandidate,
    original_name: str,
    root_id: str,
    authorization_path: Path,
) -> dict[str, object]:
    intent = OrganizationObjectIntent(
        candidate.file_id,
        candidate.parent_id,
        candidate.name,
        candidate.parent_id,
        original_name,
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_p115client_timeout_executor,
        intents=(intent,),
        managed_directory_ids=(root_id, candidate.parent_id),
        scope_confirmed=True,
        live_enabled=True,
    )
    expected = RemoteObjectState(candidate.file_id, candidate.parent_id, candidate.name)
    current = await transport.read_object(candidate.file_id)
    target = await transport.read_target(candidate.parent_id, original_name)
    if current != expected or target is not None:
        raise RestoreError("restore_precondition_changed")
    return await _restore_after_precondition(
        transport, candidate, original_name, authorization_path
    )


async def _restore_after_precondition(
    transport: object,
    candidate: ProbeCandidate,
    original_name: str,
    authorization_path: Path,
) -> dict[str, object]:
    if not _consume_authorization(authorization_path):
        raise RestoreError("authorization_already_consumed")
    try:
        result = await transport.rename(candidate.file_id, original_name)  # type: ignore[attr-defined]
    except (asyncio.CancelledError, TimeoutError, P115OrganizationTransportError):
        return {
            "status": "uncertain",
            "error_code": "restore_outcome_unknown",
            "write_started": True,
        }
    except Exception:  # noqa: BLE001 - provider details stay behind the boundary
        return {
            "status": "uncertain",
            "error_code": "restore_outcome_unknown",
            "write_started": True,
        }
    if result.status is not OrganizationTransportStatus.SUCCESS:
        return {
            "status": "uncertain",
            "error_code": "restore_receipt_unconfirmed",
            "write_started": True,
        }
    try:
        final = await transport.read_object(candidate.file_id)  # type: ignore[attr-defined]
    except (asyncio.CancelledError, TimeoutError, P115OrganizationTransportError):
        return {
            "status": "uncertain",
            "error_code": "restore_postcondition_unconfirmed",
            "write_started": True,
        }
    except Exception:  # noqa: BLE001 - provider details stay behind the boundary
        return {
            "status": "uncertain",
            "error_code": "restore_postcondition_unconfirmed",
            "write_started": True,
        }
    expected = RemoteObjectState(candidate.file_id, candidate.parent_id, original_name)
    if final != expected:
        return {
            "status": "uncertain",
            "error_code": "restore_postcondition_unconfirmed",
            "write_started": True,
        }
    return {
        "status": "success",
        "cleanup": "restored",
        "receipt_count": len(transport.receipts),  # type: ignore[attr-defined]
        "write_started": True,
    }


def _p115client_version() -> str | None:
    try:
        return version("p115client")
    except Exception:  # noqa: BLE001 - package details stay behind the boundary
        return None


async def run(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    root_id = str(args.root_id)
    if not root_id.isdigit() or root_id.startswith("0"):
        raise RestoreError("invalid_root_id")
    candidate, pages = await _discover(root_id, args.cookie_path)
    cookie = _read_cookie(args.cookie_path)
    if cookie is None:
        raise RestoreError("credential_unavailable")
    from p115client import P115Client

    client = P115Client(cookie, console_qrcode=False)
    original_name = await _read_history(client, candidate)
    plan_digest = _plan_digest(candidate, original_name)
    preview = {
        "status": "preview",
        "plan_digest": plan_digest,
        "source_count": 1,
        "history_record_count": 1,
        "pages_read": pages,
        "inventory_complete": True,
        "scope_confirmed": True,
        "write_started": False,
    }
    if args.confirm_plan_digest is None:
        return 2, preview
    if args.confirm_plan_digest != plan_digest:
        raise RestoreError("plan_digest_mismatch")
    _live_gate(
        root_id,
        args.cookie_path,
        args.authorization_path,
        args.managed_scope_path,
    )
    try:
        result = await _restore(
            client,
            candidate,
            original_name,
            root_id,
            args.authorization_path,
        )
    except RestoreError:
        raise
    except (asyncio.CancelledError, TimeoutError, P115OrganizationTransportError):
        return 1, {"status": "uncertain", "error_code": "outcome_unknown", "write_started": True}
    result.update({"plan_digest": plan_digest, "source_count": 1, "pages_read": pages})
    return (0 if result["status"] == "success" else 1), result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore one gated production probe name")
    parser.add_argument("--root-id", required=True)
    parser.add_argument("--cookie-path", required=True, type=Path)
    parser.add_argument("--authorization-path", type=Path)
    parser.add_argument("--managed-scope-path", type=Path)
    parser.add_argument("--confirm-plan-digest")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    if args.confirm_plan_digest is not None and (
        args.authorization_path is None or args.managed_scope_path is None or not args.live
    ):
        parser.error("confirmed execution requires --live, authorization and managed scope")
    try:
        status, result = asyncio.run(run(args))
    except RestoreError as error:
        status, result = 1, {"status": "blocked", "error_code": error.code, "write_started": False}
    except (asyncio.CancelledError, TimeoutError, P115OrganizationTransportError):
        status, result = 1, {"status": "uncertain", "error_code": "outcome_unknown", "write_started": True}
    except Exception:  # noqa: BLE001 - provider details stay behind the boundary
        status, result = 1, {"status": "blocked", "error_code": "restore_failed", "write_started": False}
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())

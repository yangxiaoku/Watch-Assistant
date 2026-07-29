"""Run one explicitly confirmed, reversible production-root write probe.

The probe is intentionally separate from the application worker.  It reads a
fresh production snapshot, chooses one small-directory file, emits a digest-only
preview, and on a second invocation performs one same-parent move and rename
followed by a verified restore.  It never prints remote identities or names.
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

from watch_assistant.adapters.p115_c03_live_transport import (
    EXPECTED_P115CLIENT_VERSION,
)
from watch_assistant.adapters.p115_library_gateway import (
    P115ReadOnlyDirectoryGateway,
)
from watch_assistant.adapters.p115_organization_transport import (
    OrganizationObjectIntent,
    P115OrganizationTransportError,
    create_live_p115_organization_transport,
)
from watch_assistant.services.organization_execution_contract import RemoteObjectState
from watch_assistant.services.organization_executor import OrganizationTransportStatus

try:
    from scripts.p115_c03_live_runner import (
        _authorization_matches,
        _consume_authorization,
        _p115client_timeout_executor,
        _read_cookie,
    )
except ModuleNotFoundError:
    from p115_c03_live_runner import (  # type: ignore[no-redef]
        _authorization_matches,
        _consume_authorization,
        _p115client_timeout_executor,
        _read_cookie,
    )

MAX_PAGES = 100_000
MAX_CHILDREN_FOR_WRITE_PROBE = 4
SAFE_NONCE = re.compile(r"[A-Za-z0-9._-]+")


class ProbeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Candidate:
    file_id: str
    parent_id: str
    name: str
    size_bytes: int | None
    sidecar: bool


class FileCredential:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> str:
        cookie = _read_cookie(self._path)
        if cookie is None:
            raise ProbeError("credential_unavailable")
        return cookie


async def _discover(
    root_id: str, cookie_path: Path
) -> tuple[Candidate, int, bool]:
    credential = FileCredential(cookie_path)
    gateway = P115ReadOnlyDirectoryGateway(
        credential,
        authorized_directory_ids=(root_id,),
        request_timeout_seconds=30,
    )
    queue: deque[str] = deque([root_id])
    visited: set[str] = set()
    candidates: list[Candidate] = []
    pages = 0
    while queue:
        directory_id = queue.popleft()
        if directory_id in visited:
            continue
        visited.add(directory_id)
        page_number = 1
        directory_total: int | None = None
        while True:
            if pages >= MAX_PAGES:
                raise ProbeError("pagination_limit")
            page = await gateway.list_directory(
                directory_id, page=page_number, page_size=1
            )
            pages += 1
            if page.state.value != "complete" or page.scan_complete is False:
                raise ProbeError("inventory_incomplete")
            if directory_total is None:
                directory_total = page.total
            elif directory_total != page.total:
                raise ProbeError("inventory_changed")
            if directory_total is not None and directory_total <= MAX_CHILDREN_FOR_WRITE_PROBE:
                for entry in page.items:
                    if entry.is_directory and entry.directory_id is not None:
                        queue.append(entry.directory_id)
                    elif (
                        not entry.is_directory
                        and entry.file_id is not None
                        and entry.parent_id is not None
                    ):
                        candidates.append(
                            Candidate(
                                entry.file_id,
                                entry.parent_id,
                                entry.name,
                                entry.size_bytes,
                                Path(entry.name).suffix.casefold()
                                in {".nfo", ".srt", ".ass", ".jpg", ".jpeg", ".png", ".wav"},
                            )
                        )
            else:
                for entry in page.items:
                    if entry.is_directory and entry.directory_id is not None:
                        queue.append(entry.directory_id)
            terminal = page.terminal is True or page.has_more is False
            if terminal:
                break
            if page.terminal is False or page.has_more is True:
                page_number += 1
                continue
            raise ProbeError("pagination_unverified")
    if not candidates:
        raise ProbeError("no_safe_probe_file")
    candidates.sort(key=lambda item: (not item.sidecar, item.size_bytes is None, item.size_bytes or 0))
    return candidates[0], pages, True


def _validate_scope(path: Path, root_id: str) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise ProbeError("scope_unverified") from None
    values = value.get("parent_ids") if isinstance(value, Mapping) else value
    if isinstance(values, (str, bytes)) or not isinstance(values, Collection):
        raise ProbeError("scope_unverified")
    if root_id not in {str(item) for item in values}:
        raise ProbeError("scope_unverified")


def _validate_live_gate(
    *,
    root_id: str,
    cookie_path: Path,
    authorization_path: Path,
    managed_scope_path: Path,
    live: bool,
) -> None:
    if not live:
        raise ProbeError("live_flag_required")
    required = (
        "P115_WRITE_ENABLED",
        "P115_MANAGED_FIXTURE",
        "P115_CLEANUP_PLAN",
        "P115_LIVE",
    )
    if any(os.environ.get(name) != "1" for name in required):
        raise ProbeError("write_gate_closed")
    _validate_scope(managed_scope_path, root_id)
    if not _authorization_matches(authorization_path, root_id, now=time.time):
        raise ProbeError("authorization_invalid")
    if version("p115client") != EXPECTED_P115CLIENT_VERSION:
        raise ProbeError("client_version_unverified")
    if _read_cookie(cookie_path) is None:
        raise ProbeError("credential_unavailable")
    if not _consume_authorization(authorization_path):
        raise ProbeError("authorization_already_consumed")


def _digest(candidate: Candidate, target_name: str) -> str:
    payload = {
        "object_id": candidate.file_id,
        "parent_id": candidate.parent_id,
        "source_name": candidate.name,
        "target_name": target_name,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _target_name(candidate: Candidate) -> str:
    suffix = Path(candidate.name).suffix
    token = hashlib.sha256(f"{candidate.file_id}:{candidate.name}".encode()).hexdigest()[:12]
    return f"wa-prod-probe-{token}{suffix}"


def _intent(candidate: Candidate, target_name: str) -> OrganizationObjectIntent:
    return OrganizationObjectIntent(
        candidate.file_id,
        candidate.parent_id,
        candidate.name,
        candidate.parent_id,
        target_name,
    )


async def _execute(
    client: object,
    *,
    candidate: Candidate,
    target_name: str,
    root_id: str,
) -> dict[str, object]:
    intent = _intent(candidate, target_name)
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_p115client_timeout_executor,
        intents=(intent,),
        managed_directory_ids=(root_id, candidate.parent_id),
        scope_confirmed=True,
        live_enabled=True,
    )
    source = await transport.read_object(candidate.file_id)
    target = await transport.read_target(candidate.parent_id, target_name)
    expected = RemoteObjectState(candidate.file_id, candidate.parent_id, candidate.name)
    if source != expected or target is not None:
        raise ProbeError("precondition_changed")

    move = await transport.move(candidate.file_id, candidate.parent_id)
    if move.status is not OrganizationTransportStatus.SUCCESS:
        return {"status": "uncertain", "error_code": "move_unconfirmed", "write_started": True}
    moved = await transport.read_object(candidate.file_id)
    if moved != expected:
        return {"status": "uncertain", "error_code": "move_postcondition_unconfirmed", "write_started": True}

    rename = await transport.rename(candidate.file_id, target_name)
    renamed = await transport.read_object(candidate.file_id)
    renamed_state = RemoteObjectState(candidate.file_id, candidate.parent_id, target_name)
    if rename.status is not OrganizationTransportStatus.SUCCESS or renamed != renamed_state:
        if renamed == expected:
            return {"status": "failed", "error_code": "rename_not_applied", "write_started": True}
        return {"status": "uncertain", "error_code": "rename_postcondition_unconfirmed", "write_started": True}

    restore_intent = _intent(
        Candidate(candidate.file_id, candidate.parent_id, target_name, candidate.size_bytes, candidate.sidecar),
        candidate.name,
    )
    restore = create_live_p115_organization_transport(
        client=client,
        call_executor=_p115client_timeout_executor,
        intents=(restore_intent,),
        managed_directory_ids=(root_id, candidate.parent_id),
        scope_confirmed=True,
        live_enabled=True,
    )
    restore_source = await restore.read_object(candidate.file_id)
    restore_target = await restore.read_target(candidate.parent_id, candidate.name)
    if restore_source != renamed_state or restore_target is not None:
        return {"status": "uncertain", "error_code": "cleanup_precondition_changed", "write_started": True}
    restore_result = await restore.rename(candidate.file_id, candidate.name)
    final = await restore.read_object(candidate.file_id)
    if restore_result.status is not OrganizationTransportStatus.SUCCESS or final != expected:
        return {"status": "uncertain", "error_code": "cleanup_unconfirmed", "write_started": True}
    return {
        "status": "success",
        "cleanup": "restored",
        "receipt_count": len(transport.receipts) + len(restore.receipts),
        "write_started": True,
    }


async def run(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    root_id = str(args.root_id)
    if not root_id.isdigit() or root_id.startswith("0"):
        return 1, {"status": "blocked", "error_code": "invalid_root_id", "write_started": False}
    candidate, pages, complete = await _discover(root_id, args.cookie_path)
    target_name = _target_name(candidate)
    plan_digest = _digest(candidate, target_name)
    preview = {
        "status": "preview",
        "plan_digest": plan_digest,
        "source_count": 1,
        "pages_read": pages,
        "inventory_complete": complete,
        "scope_confirmed": True,
        "write_started": False,
    }
    if args.confirm_plan_digest is None:
        return 2, preview
    if args.confirm_plan_digest != plan_digest:
        return 1, {"status": "blocked", "error_code": "plan_digest_mismatch", "write_started": False}
    _validate_live_gate(
        root_id=root_id,
        cookie_path=args.cookie_path,
        authorization_path=args.authorization_path,
        managed_scope_path=args.managed_scope_path,
        live=args.live,
    )
    cookie = _read_cookie(args.cookie_path)
    if cookie is None:
        raise ProbeError("credential_unavailable")
    from p115client import P115Client

    client = P115Client(cookie, console_qrcode=False)
    result = await _execute(client, candidate=candidate, target_name=target_name, root_id=root_id)
    result.update({"plan_digest": plan_digest, "source_count": 1, "pages_read": pages})
    return (0 if result["status"] == "success" else 1), result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one gated production P115 write probe")
    parser.add_argument("--root-id", required=True)
    parser.add_argument("--cookie-path", required=True, type=Path)
    parser.add_argument("--authorization-path", type=Path)
    parser.add_argument("--managed-scope-path", type=Path)
    parser.add_argument("--confirm-plan-digest")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    if args.confirm_plan_digest is not None and (
        args.authorization_path is None or args.managed_scope_path is None
    ):
        parser.error("confirmed execution requires authorization and managed scope")
    try:
        status, result = asyncio.run(run(args))
    except ProbeError as error:
        status, result = 1, {"status": "blocked", "error_code": error.code, "write_started": False}
    except (TimeoutError, P115OrganizationTransportError):
        status, result = 1, {"status": "uncertain", "error_code": "outcome_unknown", "write_started": True}
    except Exception:  # noqa: BLE001 - provider details stay behind the boundary
        status, result = 1, {"status": "uncertain", "error_code": "probe_failed", "write_started": True}
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())

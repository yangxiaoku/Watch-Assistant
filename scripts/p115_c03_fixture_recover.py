"""Recover leaked C03 fixture roots from the explicitly managed test CID.

This command is read-only by default.  Recycle is allowed only for directory
objects whose names use the probe's fixed ``wa-c03-root-`` prefix and only
after the same four C03 gates are open.  It never accepts a path or a
production root as a scope.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections.abc import Sequence
from pathlib import Path

from watch_assistant.adapters.p115_c03_live_transport import P115C03LiveTransport
from watch_assistant.adapters.p115_library_write_contract import (
    WriteStatus,
    prepare_recycle,
)

try:
    from scripts.p115_c03_live_runner import (
        C03_CLEANUP_PLAN_ENV,
        C03_LIVE_ENV,
        C03_MANAGED_FIXTURE_ENV,
        C03_WRITE_ENABLED_ENV,
        _p115client_timeout_executor,
        _read_cookie,
    )
except ModuleNotFoundError:
    from p115_c03_live_runner import (  # type: ignore[no-redef]
        C03_CLEANUP_PLAN_ENV,
        C03_LIVE_ENV,
        C03_MANAGED_FIXTURE_ENV,
        C03_WRITE_ENABLED_ENV,
        _p115client_timeout_executor,
        _read_cookie,
    )
from watch_assistant.adapters.p115_c03_fixture_probe import normalize_parent_id
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
    )
    from scripts.p115_production_probe_restore import _history_original_name
except ModuleNotFoundError:
    from p115_c03_live_runner import (  # type: ignore[no-redef]
        _authorization_matches,
        _consume_authorization,
    )
    from p115_production_probe_restore import (  # type: ignore[no-redef]
        _history_original_name,
    )

ROOT_PREFIXES = ("wa-c03-root-", "wa-org-probe-dir-")


async def _run(
    client,
    parent_id: str,
    *,
    recycle: bool,
    restore_organization: bool,
    authorization_path: Path | None,
) -> dict[str, object]:
    transport = P115C03LiveTransport(
        client, call_executor=_p115client_timeout_executor
    )
    listing = await transport.list_children(parent_id, timeout_seconds=30)
    if listing.complete is not True:
        return {"status": "blocked", "error_code": "inventory_incomplete"}
    roots = tuple(
        entry
        for entry in listing.entries
        if entry.is_directory and entry.name.startswith(ROOT_PREFIXES)
    )
    if not recycle:
        if restore_organization:
            if len(roots) != 1 or authorization_path is None:
                return {"status": "blocked", "error_code": "recovery_scope_unverified"}
            return await _restore_organization(
                client, parent_id, roots[0], authorization_path
            )
        return {
            "status": "preview",
            "candidate_count": len(roots),
            "inventory_complete": True,
            "recycle_started": False,
        }
    if any(os.environ.get(name) != "1" for name in _GATES):
        return {"status": "blocked", "error_code": "write_gate_closed"}
    results: list[str] = []
    for root in roots:
        receipt = await transport.execute(
            prepare_recycle(root.file_id), timeout_seconds=30
        )
        if receipt.status is not WriteStatus.SUCCESS:
            return {
                "status": "uncertain",
                "error_code": "recycle_unconfirmed",
                "recycle_started": True,
                "processed_count": len(results),
            }
        results.append(root.file_id)
    after = await transport.list_children(parent_id, timeout_seconds=30)
    if after.complete is not True or any(
        entry.is_directory and entry.name.startswith(ROOT_PREFIXES)
        for entry in after.entries
    ):
        return {
            "status": "uncertain",
            "error_code": "recovery_postcondition_unconfirmed",
            "recycle_started": bool(results),
            "processed_count": len(results),
        }
    return {
        "status": "success",
        "candidate_count": len(roots),
        "processed_count": len(results),
        "recycle_started": bool(results),
    }


async def _restore_organization(
    client, parent_id: str, temporary_directory, authorization_path: Path
) -> dict[str, object]:
    if any(os.environ.get(name) != "1" for name in _GATES):
        return {"status": "blocked", "error_code": "write_gate_closed"}
    if not _authorization_matches(authorization_path, parent_id, now=time.time):
        return {"status": "blocked", "error_code": "authorization_invalid"}
    listing_transport = P115C03LiveTransport(
        client, call_executor=_p115client_timeout_executor
    )
    listing = await listing_transport.list_children(
        temporary_directory.file_id, timeout_seconds=30
    )
    files = [entry for entry in listing.entries if not entry.is_directory]
    if listing.complete is not True or len(files) != 1:
        return {"status": "blocked", "error_code": "recovery_fixture_unverified"}
    candidate = files[0]
    history = _p115client_timeout_executor(
        client.fs_history_rename_list_app,
        {"file_ids": candidate.file_id, "offset": 0, "limit": 100},
        timeout_seconds=30,
    )
    try:
        original_name = _history_original_name(
            history, file_id=candidate.file_id, temporary_name=candidate.name
        )
    except Exception:  # noqa: BLE001 - history details stay private
        return {"status": "blocked", "error_code": "rename_history_unverified"}
    intent = OrganizationObjectIntent(
        candidate.file_id,
        temporary_directory.file_id,
        candidate.name,
        parent_id,
        original_name,
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_p115client_timeout_executor,
        intents=(intent,),
        managed_directory_ids=(parent_id, temporary_directory.file_id),
        scope_confirmed=True,
        live_enabled=True,
    )
    try:
        source = await transport.read_object(candidate.file_id)
        target = await transport.read_target(parent_id, original_name)
        expected = RemoteObjectState(
            candidate.file_id, temporary_directory.file_id, candidate.name
        )
        if source != expected or target is not None:
            return {"status": "blocked", "error_code": "recovery_precondition_changed"}
        if not _consume_authorization(authorization_path):
            return {"status": "blocked", "error_code": "authorization_already_consumed"}
        move = await transport.move(candidate.file_id, parent_id)
        moved = await transport.read_object(candidate.file_id)
        if (
            move.status is not OrganizationTransportStatus.SUCCESS
            or moved != RemoteObjectState(candidate.file_id, parent_id, candidate.name)
        ):
            return {"status": "uncertain", "error_code": "recovery_move_unconfirmed"}
        rename = await transport.rename(candidate.file_id, original_name)
        final = await transport.read_object(candidate.file_id)
        if (
            rename.status is not OrganizationTransportStatus.SUCCESS
            or final != RemoteObjectState(candidate.file_id, parent_id, original_name)
        ):
            return {"status": "uncertain", "error_code": "recovery_rename_unconfirmed"}
        recycle = await listing_transport.execute(
            prepare_recycle(temporary_directory.file_id), timeout_seconds=30
        )
        after = await listing_transport.list_children(parent_id, timeout_seconds=30)
        if (
            recycle.status is not WriteStatus.SUCCESS
            or after.complete is not True
            or any(entry.file_id == temporary_directory.file_id for entry in after.entries)
        ):
            return {"status": "uncertain", "error_code": "recovery_cleanup_unconfirmed"}
    except (asyncio.CancelledError, TimeoutError, P115OrganizationTransportError):
        return {"status": "uncertain", "error_code": "recovery_outcome_unknown"}
    return {"status": "success", "cleanup": "restored", "write_started": True}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recover leaked C03 fixture roots")
    parser.add_argument("--parent-id", required=True)
    parser.add_argument("--cookie-path", required=True, type=Path)
    parser.add_argument("--recycle", action="store_true")
    parser.add_argument("--restore-organization", action="store_true")
    parser.add_argument("--authorization-path", type=Path)
    args = parser.parse_args(argv)
    try:
        parent_id = normalize_parent_id(args.parent_id)
        cookie = _read_cookie(args.cookie_path)
        if cookie is None:
            raise ValueError("credential_unavailable")
        from p115client import P115Client

        result = asyncio.run(
            _run(
                P115Client(cookie, console_qrcode=False),
                parent_id,
                recycle=args.recycle,
                restore_organization=args.restore_organization,
                authorization_path=args.authorization_path,
            )
        )
    except ValueError as error:
        result = {"status": "blocked", "error_code": str(error)}
    except Exception:  # noqa: BLE001 - provider details stay behind the boundary
        result = {"status": "uncertain", "error_code": "recovery_failed"}
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["status"] in {"preview", "success"} else 1


_GATES = (
    C03_WRITE_ENABLED_ENV,
    C03_MANAGED_FIXTURE_ENV,
    C03_CLEANUP_PLAN_ENV,
    C03_LIVE_ENV,
)


if __name__ == "__main__":
    raise SystemExit(main())

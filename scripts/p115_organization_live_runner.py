"""Run one explicitly approved organization move/rename probe on a test CID.

The runner is intentionally narrower than the application worker. It creates
one fresh plan from a complete child listing, requires a matching digest and a
one-shot authorization artifact, then restores the same object to its original
name. It never accepts a production root as an implicit scope.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path

try:
    from scripts.p115_c03_live_runner import (
        C03_CLEANUP_PLAN_ENV,
        C03_LIVE_ENV,
        C03_MANAGED_FIXTURE_ENV,
        C03_WRITE_ENABLED_ENV,
        _authorization_matches,
        _c03_organization_contract,
        _consume_authorization,
        _p115client_timeout_executor,
        _read_cookie,
    )
except ModuleNotFoundError:
    from p115_c03_live_runner import (
        C03_CLEANUP_PLAN_ENV,
        C03_LIVE_ENV,
        C03_MANAGED_FIXTURE_ENV,
        C03_WRITE_ENABLED_ENV,
        _authorization_matches,
        _c03_organization_contract,
        _consume_authorization,
        _p115client_timeout_executor,
        _read_cookie,
    )
from watch_assistant.adapters.p115_c03_fixture_probe import normalize_parent_id
from watch_assistant.adapters.p115_c03_live_transport import (
    EXPECTED_P115CLIENT_VERSION,
    P115C03LiveTransport,
)
from watch_assistant.adapters.p115_library_write_contract import (
    WriteStatus,
    prepare_mkdir,
    prepare_recycle,
)
from watch_assistant.adapters.p115_organization_transport import (
    OrganizationObjectIntent,
    P115OrganizationTransportError,
    create_live_p115_organization_transport,
)
from watch_assistant.services.organization_execution_contract import RemoteObjectState
from watch_assistant.services.organization_executor import OrganizationTransportStatus


def run_probe(
    *,
    parent_id: object,
    cookie_path: str | os.PathLike[str],
    authorization_path: str | os.PathLike[str],
    managed_scope_path: str | os.PathLike[str],
    confirm_digest: str | None,
    live: bool,
    env: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    environment = os.environ if env is None else env
    try:
        normalized_parent = normalize_parent_id(parent_id)
    except (TypeError, ValueError):
        return 1, _blocked("invalid_scope")
    if not live or any(environment.get(name) != "1" for name in _GATES):
        return 1, _blocked("write_gate_closed")
    if not _scope_contains(managed_scope_path, normalized_parent):
        return 1, _blocked("scope_unverified")
    if not _authorization_matches(authorization_path, normalized_parent, now=time.time):
        return 1, _blocked("authorization_invalid")
    if _p115client_version() != EXPECTED_P115CLIENT_VERSION:
        return 1, _blocked("client_version_unverified")
    cookie = _read_cookie(cookie_path)
    if cookie is None:
        return 1, _blocked("credential_unavailable")
    try:
        from p115client import P115Client

        client = P115Client(cookie, console_qrcode=False)
    except Exception:  # noqa: BLE001 - client details stay private
        return 1, _blocked("client_unavailable")
    return asyncio.run(
        _run_with_client(
            client,
            normalized_parent,
            authorization_path=authorization_path,
            confirm_digest=confirm_digest,
        )
    )


async def _run_with_client(
    client,
    parent_id: str,
    *,
    authorization_path: str | os.PathLike[str],
    confirm_digest: str | None,
) -> tuple[int, dict[str, object]]:
    listing_transport = P115C03LiveTransport(
        client, call_executor=_p115client_timeout_executor
    )
    try:
        listing = await listing_transport.list_children(parent_id, timeout_seconds=30)
    except Exception:  # noqa: BLE001 - live details stay private
        return 1, _blocked("inventory_read_failed")
    if listing.complete is not True:
        return 1, _blocked("inventory_incomplete")
    files = [entry for entry in listing.entries if not entry.is_directory]
    if len(files) != 1:
        return 1, _blocked("test_fixture_not_exactly_one_file")
    source = files[0]
    target_name = _target_name(source.name, source.file_id)
    plan_digest = _plan_digest(source.file_id, source.parent_id, source.name, target_name)
    preview = {
        "status": "preview",
        "plan_digest": plan_digest,
        "source_count": 1,
        "scope_confirmed": True,
        "write_started": False,
    }
    if confirm_digest is None:
        return 2, preview
    if confirm_digest != plan_digest:
        return 1, _blocked("plan_digest_mismatch", plan_digest=plan_digest)
    first = OrganizationObjectIntent(
        source.file_id, source.parent_id, source.name, parent_id, target_name
    )
    result = await _execute_and_restore(
        client,
        parent_id,
        first,
        original_name=source.name,
        authorization_path=authorization_path,
    )
    result.update({"plan_digest": plan_digest, "source_count": 1})
    return (0 if result["status"] == "success" else 1), result


async def _execute_and_restore(
    client,
    parent_id: str,
    intent: OrganizationObjectIntent,
    *,
    original_name: str,
    authorization_path: str | os.PathLike[str],
) -> dict[str, object]:
    listing_transport = P115C03LiveTransport(
        client, call_executor=_p115client_timeout_executor
    )
    temporary_name = _temporary_directory_name(intent.object_id, original_name)
    if not _consume_authorization(authorization_path):
        return _blocked("authorization_already_consumed")
    try:
        source_listing = await listing_transport.list_children(parent_id, timeout_seconds=30)
        if source_listing.complete is not True:
            return _blocked("inventory_incomplete")
        source_matches = [
            entry
            for entry in source_listing.entries
            if entry.file_id == intent.object_id
        ]
        if (
            len(source_matches) != 1
            or source_matches[0].parent_id != parent_id
            or source_matches[0].name != original_name
            or source_matches[0].is_directory
            or any(entry.name == temporary_name for entry in source_listing.entries)
        ):
            return _blocked("precondition_changed")
        mkdir = await listing_transport.execute(
            prepare_mkdir(parent_id, temporary_name), timeout_seconds=30
        )
        if mkdir.status is not WriteStatus.SUCCESS or mkdir.file_id is None:
            return _uncertain("temporary_directory_unconfirmed", listing_transport)
        temporary_id = mkdir.file_id
        created_listing = await listing_transport.list_children(parent_id, timeout_seconds=30)
        created = [
            entry
            for entry in created_listing.entries
            if entry.file_id == temporary_id
        ]
        if (
            created_listing.complete is not True
            or len(created) != 1
            or created[0].name != temporary_name
            or not created[0].is_directory
        ):
            return _uncertain("temporary_directory_postcondition_unconfirmed", listing_transport)
    except (asyncio.CancelledError, TimeoutError):
        return _uncertain("outcome_unknown", listing_transport)
    except P115OrganizationTransportError:
        return _uncertain("outcome_unknown", listing_transport)

    intent = OrganizationObjectIntent(
        intent.object_id,
        parent_id,
        original_name,
        temporary_id,
        intent.target_name,
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_p115client_timeout_executor,
        intents=(intent,),
        managed_directory_ids=(parent_id, temporary_id),
        scope_confirmed=True,
        live_enabled=True,
        organization_contract=_c03_organization_contract(),
    )
    try:
        source = await transport.read_object(intent.object_id)
        target = await transport.read_target(intent.target_parent_id, intent.target_name)
        if source != RemoteObjectState(intent.object_id, parent_id, original_name) or target is not None:
            return _blocked("precondition_changed")
        move = await transport.move(intent.object_id, temporary_id)
        if move.status is not OrganizationTransportStatus.SUCCESS:
            return _uncertain("move_unconfirmed", transport)
        moved = await transport.read_object(intent.object_id)
        if moved != RemoteObjectState(intent.object_id, temporary_id, original_name):
            return _uncertain("move_postcondition_unconfirmed", transport)
        rename = await transport.rename(intent.object_id, intent.target_name)
        if rename.status is not OrganizationTransportStatus.SUCCESS:
            return _uncertain("rename_unconfirmed", transport)
        renamed = await transport.read_object(intent.object_id)
        if renamed != RemoteObjectState(
            intent.object_id, temporary_id, intent.target_name
        ):
            return _uncertain("rename_postcondition_unconfirmed", transport)
    except (asyncio.CancelledError, TimeoutError):
        return _uncertain("outcome_unknown", transport)
    except P115OrganizationTransportError:
        return _uncertain("outcome_unknown", transport)

    restore_intent = OrganizationObjectIntent(
        intent.object_id,
        temporary_id,
        intent.target_name,
        parent_id,
        original_name,
    )
    restore = create_live_p115_organization_transport(
        client=client,
        call_executor=_p115client_timeout_executor,
        intents=(restore_intent,),
        managed_directory_ids=(parent_id, temporary_id),
        scope_confirmed=True,
        live_enabled=True,
        organization_contract=_c03_organization_contract(),
    )
    try:
        restored_source = await restore.read_object(intent.object_id)
        restored_target = await restore.read_target(parent_id, original_name)
        if restored_source != RemoteObjectState(intent.object_id, temporary_id, intent.target_name) or restored_target is not None:
            return _uncertain("cleanup_precondition_changed", transport, restore)
        move = await restore.move(intent.object_id, parent_id)
        rename = (
            await restore.rename(intent.object_id, original_name)
            if move.status is OrganizationTransportStatus.SUCCESS
            else None
        )
        final = await restore.read_object(intent.object_id)
        if (
            move.status is not OrganizationTransportStatus.SUCCESS
            or rename is None
            or rename.status is not OrganizationTransportStatus.SUCCESS
            or final != RemoteObjectState(intent.object_id, parent_id, original_name)
        ):
            return _uncertain("cleanup_unconfirmed", transport, restore)
    except (asyncio.CancelledError, TimeoutError, P115OrganizationTransportError):
        return _uncertain("cleanup_outcome_unknown", transport, restore)
    recycle = await listing_transport.execute(
        prepare_recycle(temporary_id), timeout_seconds=30
    )
    final_parent = await listing_transport.list_children(parent_id, timeout_seconds=30)
    if (
        recycle.status is not WriteStatus.SUCCESS
        or final_parent.complete is not True
        or any(entry.file_id == temporary_id for entry in final_parent.entries)
    ):
        return _uncertain("temporary_directory_cleanup_unconfirmed", transport, restore)
    return {
        "status": "success",
        "cleanup": "restored",
        "receipt_count": len(transport.receipts) + len(restore.receipts) + 2,
        "write_started": True,
    }


def _scope_contains(path: str | os.PathLike[str], parent_id: str) -> bool:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    if isinstance(value, Mapping):
        value = value.get("parent_ids")
    if isinstance(value, (str, bytes)) or not isinstance(value, Collection):
        return False
    try:
        return parent_id in {normalize_parent_id(item) for item in value}
    except (TypeError, ValueError):
        return False


def _target_name(source_name: str, object_id: str) -> str:
    suffix = Path(source_name).suffix
    token = hashlib.sha256(f"{object_id}:{source_name}".encode()).hexdigest()[:12]
    return f"wa-org-probe-{token}{suffix}"


def _temporary_directory_name(object_id: str, source_name: str) -> str:
    token = hashlib.sha256(f"{object_id}:{source_name}".encode()).hexdigest()[:12]
    return f"wa-org-probe-dir-{token}"


def _plan_digest(object_id: str, parent_id: str, source_name: str, target_name: str) -> str:
    payload = json.dumps(
        {"object_id": object_id, "parent_id": parent_id, "source_name": source_name, "target_name": target_name},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _uncertain(code: str, *transports) -> dict[str, object]:
    return {
        "status": "uncertain",
        "error_code": code,
        "cleanup": "not_confirmed",
        "receipt_count": sum(
            len(getattr(transport, "receipts", ())) for transport in transports
        ),
        "write_started": True,
    }


def _blocked(code: str, **extra: object) -> dict[str, object]:
    return {"status": "blocked", "error_code": code, "write_started": False, **extra}


def _p115client_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("p115client")
    except Exception:  # noqa: BLE001 - package details stay private
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one gated live organization probe")
    parser.add_argument("--parent-id", required=True)
    parser.add_argument("--cookie-path", required=True)
    parser.add_argument("--authorization-path", required=True)
    parser.add_argument("--managed-scope-path", required=True)
    parser.add_argument("--confirm-digest")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    status, report = run_probe(
        parent_id=args.parent_id,
        cookie_path=args.cookie_path,
        authorization_path=args.authorization_path,
        managed_scope_path=args.managed_scope_path,
        confirm_digest=args.confirm_digest,
        live=args.live,
    )
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return status


_GATES = (
    C03_WRITE_ENABLED_ENV,
    C03_MANAGED_FIXTURE_ENV,
    C03_CLEANUP_PLAN_ENV,
    C03_LIVE_ENV,
)


if __name__ == "__main__":
    raise SystemExit(main())

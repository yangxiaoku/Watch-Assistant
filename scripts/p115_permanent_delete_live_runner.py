"""Run one explicitly approved permanent-delete probe on the test CID.

The probe permanently removes only the single file in the documented clean
test directory.  It requires a fresh plan digest and a one-shot authorization,
and it distinguishes recycle-bin removal from permanent cleanup.
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

from watch_assistant.adapters.p115_c03_fixture_probe import normalize_parent_id
from watch_assistant.adapters.p115_c03_live_transport import EXPECTED_P115CLIENT_VERSION
from watch_assistant.adapters.p115_library_write_contract import (
    WriteStatus,
    prepare_delete,
)
from watch_assistant.adapters.p115_permanent_delete_transport import (
    P115PermanentDeleteTransport,
)

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

PERMANENT_DELETE_ENABLED_ENV = "WATCH_ASSISTANT_P115_PERMANENT_DELETE"
PERMANENT_DELETE_CONTRACT_ENV = "WATCH_ASSISTANT_P115_PERMANENT_DELETE_CONTRACT"
_GATES = (
    C03_WRITE_ENABLED_ENV,
    C03_MANAGED_FIXTURE_ENV,
    C03_CLEANUP_PLAN_ENV,
    C03_LIVE_ENV,
    PERMANENT_DELETE_ENABLED_ENV,
    PERMANENT_DELETE_CONTRACT_ENV,
)


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
    transport = P115PermanentDeleteTransport(
        client, call_executor=_p115client_timeout_executor
    )
    listing = await transport.list_children(parent_id, timeout_seconds=30)
    if listing.complete is not True:
        return 1, _blocked("inventory_incomplete")
    files = [entry for entry in listing.entries if not entry.is_directory]
    if len(files) != 1:
        return 1, _blocked("test_fixture_not_exactly_one_file")
    source = files[0]
    plan_digest = _plan_digest(source.file_id, source.parent_id, source.name)
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
    current = await transport.list_children(parent_id, timeout_seconds=30)
    matches = [entry for entry in current.entries if entry.file_id == source.file_id]
    if (
        current.complete is not True
        or len(matches) != 1
        or matches[0].parent_id != parent_id
        or matches[0].name != source.name
    ):
        return 1, _blocked("precondition_changed", plan_digest=plan_digest)
    if not _consume_authorization(authorization_path):
        return 1, _blocked("authorization_already_consumed", plan_digest=plan_digest)
    try:
        before_entries = await transport.list_entries(timeout_seconds=30)
        if before_entries is None:
            return 1, _uncertain("recycle_listing_unverified", plan_digest)
        before_recycle_ids = {item.recycle_id for item in before_entries}
        recycle = await transport.move_to_recycle(
            prepare_delete(source.file_id), timeout_seconds=30
        )
        if recycle.status is not WriteStatus.SUCCESS:
            return 1, _uncertain("recycle_outcome_unknown", plan_digest)
        candidates = await transport.find_new_entries(
            before_recycle_ids,
            parent_id=parent_id,
            name=source.name,
            size_bytes=_source_size(source),
            timeout_seconds=30,
        )
        if candidates is None:
            return 1, _uncertain("recycle_listing_unverified", plan_digest)
        if len(candidates) != 1:
            return 1, _uncertain("recycle_entry_unverified", plan_digest)
        cleaned = await transport.permanently_clean(
            candidates[0].recycle_id, timeout_seconds=30
        )
        if cleaned.status is not WriteStatus.SUCCESS:
            return 1, _uncertain("permanent_delete_unconfirmed", plan_digest)
        after = await transport.list_children(parent_id, timeout_seconds=30)
        recycle_absent = await transport.wait_until_absent(
            candidates[0].recycle_id, timeout_seconds=30
        )
        if (
            after.complete is not True
            or recycle_absent is not True
            or any(entry.file_id == source.file_id for entry in after.entries)
        ):
            return 1, _uncertain("permanent_delete_postcondition_unverified", plan_digest)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - provider details stay private
        return 1, _uncertain("outcome_unknown", plan_digest)
    return 0, {
        "status": "success",
        "deleted_permanently": True,
        "plan_digest": plan_digest,
        "source_count": 1,
        "write_started": True,
    }


def _source_size(source) -> int | None:
    return getattr(source, "size_bytes", None)


def _plan_digest(file_id: str, parent_id: str, name: str) -> str:
    payload = {"file_id": file_id, "parent_id": parent_id, "name": name}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()


def _scope_contains(path: str | os.PathLike[str], parent_id: str) -> bool:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    values = value.get("parent_ids") if isinstance(value, Mapping) else value
    return not isinstance(values, (str, bytes)) and isinstance(values, Collection) and parent_id in {
        str(item) for item in values
    }


def _p115client_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("p115client")
    except Exception:  # noqa: BLE001 - package details stay private
        return None


def _uncertain(code: str, plan_digest: str) -> dict[str, object]:
    return {
        "status": "uncertain",
        "error_code": code,
        "plan_digest": plan_digest,
        "source_count": 1,
        "write_started": True,
    }


def _blocked(code: str, **extra: object) -> dict[str, object]:
    return {"status": "blocked", "error_code": code, "write_started": False, **extra}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one gated permanent-delete probe")
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


if __name__ == "__main__":
    raise SystemExit(main())

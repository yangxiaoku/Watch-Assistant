"""Bounded live application acceptance for the STRM fixture workflow.

The runner configures a temporary application database, performs a complete
library scan through the formal API, renames one explicitly scoped fixture
file, verifies the API incremental update, and restores the original name.
It never accepts a root outside the supplied managed scope and does not
perform permanent deletion.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import tempfile
import time
from collections.abc import Mapping, Sequence
from importlib.metadata import version
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from p115client import P115Client
from pwdlib import PasswordHash

from watch_assistant.adapters.p115_c03_live_transport import (
    EXPECTED_P115CLIENT_VERSION,
    P115C03LiveTransport,
)
from watch_assistant.adapters.p115_organization_transport import (
    OrganizationObjectIntent,
    create_live_p115_organization_transport,
)
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.security import SecurityManager
from watch_assistant.services.p115_credentials import CookieProvider

try:
    from scripts.p115_c03_live_runner import (
        _c03_organization_contract,
        _p115client_timeout_executor,
    )
except ModuleNotFoundError:
    from p115_c03_live_runner import (  # type: ignore[no-redef]
        _c03_organization_contract,
        _p115client_timeout_executor,
    )

LIVE_ENV = "WATCH_ASSISTANT_P115_STRM_LIVE"
DEFAULT_PLAYBACK_PREFIX = "http://127.0.0.1:8115/api/v1/strm/play"
SCAN_POLL_INTERVAL_SECONDS = 0.1
SCAN_POLL_TIMEOUT_SECONDS = 30 * 60


class LiveAcceptanceError(RuntimeError):
    """A redacted acceptance failure."""


def run_acceptance(
    *,
    root_id: str,
    file_id: str,
    cookie_path: Path,
    rename_authorization: Path,
    restore_authorization: Path,
    managed_scope_path: Path,
    environment: Mapping[str, str],
) -> dict[str, object]:
    if environment.get(LIVE_ENV) != "1":
        return _blocked("live_gate_closed")
    if not _stable_id(root_id) or not _stable_id(file_id):
        return _blocked("invalid_scope")
    if not _scope_contains(managed_scope_path, root_id):
        return _blocked("scope_unverified")
    if version("p115client") != EXPECTED_P115CLIENT_VERSION:
        return _blocked("client_version_unverified")
    return asyncio.run(
        _run(
            root_id=root_id,
            file_id=file_id,
            cookie_path=cookie_path,
            rename_authorization=rename_authorization,
            restore_authorization=restore_authorization,
        )
    )


async def _run(
    *,
    root_id: str,
    file_id: str,
    cookie_path: Path,
    rename_authorization: Path,
    restore_authorization: Path,
) -> dict[str, object]:
    temporary = tempfile.TemporaryDirectory(prefix="wa-strm-live-")
    database = None
    app_client: httpx.AsyncClient | None = None
    remote_client = None
    lifespan_context = None
    lifespan_entered = False
    renamed = False
    original_name: str | None = None
    target_name: str | None = None
    try:
        base = Path(temporary.name)
        database = create_database(f"sqlite+aiosqlite:///{base / 'acceptance.db'}")
        await initialize_database(database.engine)
        password = secrets.token_urlsafe(24)
        password_hash = PasswordHash.recommended()
        app = create_app(
            database=database,
            crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
            tmdb_client=_FakeClient(),
            pansou_client=_FakeClient(),
            security_manager=SecurityManager(
                web_password_hash=password_hash.hash(password),
                script_token_hash=password_hash.hash(secrets.token_urlsafe(24)),
            ),
            frontend_dir=base / "missing-frontend",
            organization_plan_enabled=True,
            strm_full_enabled=True,
            strm_incremental_enabled=True,
            strm_cleanup_enabled=True,
            strm_output_root=base / "strm-output",
            strm_playback_url_prefix=DEFAULT_PLAYBACK_PREFIX,
        )
        app.state.organization_target_root_id = root_id
        app.state.organization_cookie_provider = CookieProvider(cookie_path)
        # ASGITransport does not start FastAPI lifespan workers by itself.
        lifespan_context = app.router.lifespan_context(app)
        await lifespan_context.__aenter__()
        lifespan_entered = True
        app_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://app.test"
        )
        headers = await _login(app_client, password)
        configured = await app_client.put(
            "/api/v1/libraries/strm-live/configuration",
            headers=headers,
            json={"name": "STRM Live Fixture", "root_directory_id": root_id, "revision": 0},
        )
        _expect(configured, "configuration_failed")
        verified = await app_client.post(
            "/api/v1/libraries/strm-live/verify-scope", headers=headers
        )
        _expect(verified, "scope_verification_failed")
        initial = await _scan(app_client, headers, "strm-live-initial")
        first_generation = await _post_generation(
            app_client,
            headers,
            "/api/v1/libraries/strm-live/strm-generation",
            initial["run_id"],
            "initial_generation_failed",
        )

        cookie = CookieProvider(cookie_path).load()
        if not cookie:
            raise LiveAcceptanceError("credential_unavailable")
        remote_client = P115Client(cookie, console_qrcode=False)
        original_name = await _resolve_fixture_name(remote_client, root_id, file_id)
        target_name = _probe_target_name(original_name)
        rename_result = await _rename_remote(
            remote_client,
            root_id,
            file_id,
            rename_authorization,
            original_name,
            target_name,
        )
        renamed = True
        changed = await _scan(app_client, headers, "strm-live-renamed")
        if changed["changed_count"] != 1:
            raise LiveAcceptanceError("changed_scan_failed")
        incremental = await _post_generation(
            app_client,
            headers,
            "/api/v1/libraries/strm-live/strm-incremental",
            changed["run_id"],
            "incremental_failed",
        )
        manifest = await app_client.get(
            "/api/v1/libraries/strm-live/strm-manifest", headers=headers
        )
        _expect(manifest, "manifest_failed")
        changed_item = manifest.json()["items"][0]
        changed_path = base / "strm-output" / changed_item["local_relative_path"]
        if not changed_path.is_file():
            raise LiveAcceptanceError("incremental_output_missing")

        restore_result = await _rename_remote(
            remote_client,
            root_id,
            file_id,
            restore_authorization,
            target_name,
            original_name,
        )
        renamed = False
        restored = await _scan(app_client, headers, "strm-live-restored")
        restored_incremental = await _post_generation(
            app_client,
            headers,
            "/api/v1/libraries/strm-live/strm-incremental",
            restored["run_id"],
            "restored_incremental_failed",
        )
        cleanup = await _post_cleanup(app_client, headers, restored["run_id"])
        return {
            "status": "success",
            "library_id": "strm-live",
            "root_directory_id": root_id,
            "verify_scope": {
                "verified": verified.json()["verified"],
                "enabled": verified.json()["enabled"],
            },
            "initial_scan": _scan_public(initial),
            "initial_generation": first_generation,
            "rename": rename_result,
            "changed_scan": _scan_public(changed),
            "incremental_generation": incremental,
            "changed_manifest": {
                "cloud_file_id": changed_item["cloud_file_id"],
                "manifest_id": changed_item["manifest_id"],
                "local_relative_path": changed_item["local_relative_path"],
                "strm_exists": True,
            },
            "restore": restore_result,
            "restored_scan": _scan_public(restored),
            "restored_incremental_generation": restored_incremental,
            "cleanup": cleanup,
            "production_root_touched": False,
            "permanent_delete_used": False,
            "output_root_is_temporary": True,
        }
    except LiveAcceptanceError as error:
        return _blocked(str(error))
    finally:
        if (
            renamed
            and remote_client is not None
            and original_name is not None
            and target_name is not None
        ):
            try:
                await _rename_remote(
                    remote_client,
                    root_id,
                    file_id,
                    restore_authorization,
                    target_name,
                    original_name,
                )
            except Exception:  # noqa: BLE001 - preserve the original report
                raise LiveAcceptanceError("restore_failed") from None
        if app_client is not None:
            await app_client.aclose()
        if remote_client is not None and hasattr(remote_client, "close"):
            remote_client.close()
        if lifespan_entered and lifespan_context is not None:
            await lifespan_context.__aexit__(None, None, None)
        if database is not None:
            await database.engine.dispose()
        temporary.cleanup()


async def _login(client: httpx.AsyncClient, password: str) -> dict[str, str]:
    response = await client.post("/api/v1/auth/login", json={"password": password})
    _expect(response, "login_failed")
    return {"X-CSRF-Token": response.json()["csrf_token"]}


async def _scan(
    client: httpx.AsyncClient, headers: Mapping[str, str], idempotency_key: str
) -> dict[str, object]:
    response = await client.post(
        "/api/v1/libraries/strm-live/scan",
        headers=headers,
        json={"idempotency_key": idempotency_key},
    )
    _expect(response, "scan_failed")
    body = response.json()
    run_id = body.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise LiveAcceptanceError("scan_run_missing")
    deadline = time.monotonic() + SCAN_POLL_TIMEOUT_SECONDS
    while body.get("complete") is not True:
        if body.get("state") in {"failed", "cancelled"} or time.monotonic() >= deadline:
            raise LiveAcceptanceError("scan_incomplete")
        await asyncio.sleep(SCAN_POLL_INTERVAL_SECONDS)
        status = await client.get(
            f"/api/v1/libraries/strm-live/scans/{run_id}", headers=headers
        )
        _expect(status, "scan_status_failed")
        body = status.json()
    return body


async def _post_generation(
    client: httpx.AsyncClient,
    headers: Mapping[str, str],
    path: str,
    run_id: object,
    error_code: str,
) -> dict[str, object]:
    response = await client.post(
        path, headers=headers, json={"source_scan_run_id": str(run_id)}
    )
    _expect(response, error_code)
    return response.json()


async def _post_cleanup(
    client: httpx.AsyncClient,
    headers: Mapping[str, str],
    run_id: object,
) -> dict[str, object]:
    plan_response = await client.post(
        "/api/v1/libraries/strm-live/strm-cleanup-plan",
        headers=headers,
        json={"source_scan_run_id": str(run_id)},
    )
    _expect(plan_response, "cleanup_plan_failed")
    plan = plan_response.json()
    if not isinstance(plan, dict):
        raise LiveAcceptanceError("cleanup_plan_invalid")
    plan_id = plan.get("plan_id")
    revision = plan.get("revision")
    digest = plan.get("plan_hash")
    if (
        not isinstance(plan_id, str)
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or not isinstance(digest, str)
        or len(digest) != 64
    ):
        raise LiveAcceptanceError("cleanup_plan_invalid")
    response = await client.post(
        f"/api/v1/strm-cleanup-plans/{plan_id}/apply",
        headers=headers,
        json={
            "expected_revision": revision,
            "digest": digest,
            "confirm": True,
            "idempotency_key": "strm-live-cleanup-confirmed",
        },
    )
    _expect(response, "cleanup_failed")
    return response.json()


async def _rename_remote(
    client,
    root_id: str,
    file_id: str,
    authorization: Path,
    source_name: str,
    target_name: str,
) -> dict[str, object]:
    if not _authorization_matches(authorization, root_id) or not _consume_authorization(
        authorization
    ):
        raise LiveAcceptanceError("authorization_invalid")
    intent = OrganizationObjectIntent(
        file_id, root_id, source_name, root_id, target_name
    )
    transport = create_live_p115_organization_transport(
        client=client,
        call_executor=_p115client_timeout_executor,
        intents=(intent,),
        managed_directory_ids=(root_id,),
        scope_confirmed=True,
        live_enabled=True,
        write_enabled=True,
        plan_confirmed=True,
        organization_contract=_c03_organization_contract(),
    )
    before = await transport.read_object(file_id)
    target = await transport.read_target(root_id, target_name)
    if before is None or before.name != source_name or target is not None:
        raise LiveAcceptanceError("rename_precondition_changed")
    result = await transport.rename(file_id, target_name)
    after = await transport.read_object(file_id)
    if result.status.value != "success" or after is None or after.name != target_name:
        raise LiveAcceptanceError("rename_postcondition_unconfirmed")
    return {"status": result.status.value, "receipt_count": len(transport.receipts)}


async def _resolve_fixture_name(client, root_id: str, file_id: str) -> str:
    """Read the exact fixture entry before consuming a write authorization."""

    listing_transport = P115C03LiveTransport(
        client, call_executor=_p115client_timeout_executor
    )
    try:
        listing = await listing_transport.list_children(root_id, timeout_seconds=30)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - keep provider details out of the report
        raise LiveAcceptanceError("fixture_inventory_read_failed") from None
    matches = [entry for entry in listing.entries if entry.file_id == file_id]
    if (
        listing.complete is not True
        or len(matches) != 1
        or matches[0].parent_id != root_id
        or matches[0].is_directory
        or not matches[0].name
    ):
        raise LiveAcceptanceError("fixture_precondition_changed")
    return matches[0].name


def _probe_target_name(source_name: str) -> str:
    suffix = Path(source_name).suffix
    target_name = f"wa-strm-probe-renamed{suffix}"
    if target_name == source_name:
        raise LiveAcceptanceError("fixture_name_not_supported")
    return target_name


def _expect(response: httpx.Response, code: str) -> None:
    if response.status_code >= 400:
        raise LiveAcceptanceError(code)


def _scope_contains(path: Path, root_id: str) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    values = value.get("parent_ids") if isinstance(value, dict) else None
    return isinstance(values, list) and root_id in {str(item) for item in values}


def _authorization_matches(path: Path, root_id: str) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return (
        isinstance(value, dict)
        and value.get("version") == 1
        and str(value.get("parent_id")) == root_id
        and isinstance(value.get("expires_at"), (int, float))
        and not isinstance(value.get("expires_at"), bool)
        and float(value["expires_at"]) > time.time()
        and isinstance(value.get("nonce"), str)
        and bool(value["nonce"])
    )


def _consume_authorization(path: Path) -> bool:
    try:
        consumed = path.with_name(path.name + ".consumed")
        path.replace(consumed)
    except OSError:
        return False
    return True


def _stable_id(value: object) -> bool:
    return isinstance(value, str) and value.isdigit() and not value.startswith("0")


def _scan_public(body: Mapping[str, object]) -> dict[str, object]:
    return {
        key: body.get(key)
        for key in (
            "run_id",
            "state",
            "complete",
            "snapshot_revision",
            "pages_read",
            "items_seen",
            "added_count",
            "changed_count",
            "removed_count",
            "error_code",
        )
    }


def _blocked(error_code: str) -> dict[str, object]:
    return {"status": "blocked", "error_code": error_code, "write_started": False}


class _FakeClient:
    async def aclose(self):
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run bounded live STRM application acceptance")
    parser.add_argument("--root-id", required=True)
    parser.add_argument("--file-id", required=True)
    parser.add_argument("--cookie-path", required=True, type=Path)
    parser.add_argument("--rename-authorization", required=True, type=Path)
    parser.add_argument("--restore-authorization", required=True, type=Path)
    parser.add_argument("--managed-scope-path", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    environment = dict(__import__("os").environ)
    if args.live:
        environment[LIVE_ENV] = "1"
    else:
        environment.pop(LIVE_ENV, None)
    report = run_acceptance(
        root_id=args.root_id,
        file_id=args.file_id,
        cookie_path=args.cookie_path,
        rename_authorization=args.rename_authorization,
        restore_authorization=args.restore_authorization,
        managed_scope_path=args.managed_scope_path,
        environment=environment,
    )
    encoded = json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

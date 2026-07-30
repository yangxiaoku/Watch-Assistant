"""Bounded live acceptance for the managed STRM playback boundary.

The runner creates a temporary application database and output root, indexes
one explicitly scoped fixture file, and exercises the application playback
route with HEAD, GET, and a single byte range.  It reports only status codes
and allow-listed response headers; dynamic links and provider payloads never
cross the report boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import tempfile
from collections.abc import Mapping, Sequence
from importlib.metadata import version
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.adapters.p115_c03_live_transport import EXPECTED_P115CLIENT_VERSION
from watch_assistant.adapters.p115_playback_gateway import P115LivePlaybackGateway
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.security import SecurityManager
from watch_assistant.services.p115_credentials import CookieProvider

LIVE_ENV = "WATCH_ASSISTANT_P115_STRM_PLAYBACK_LIVE"
PLAYBACK_PREFIX = "http://127.0.0.1:8115/api/v1/strm/play"


def run_acceptance(
    *,
    root_id: str,
    file_id: str,
    cookie_path: Path,
    managed_scope_path: Path,
    environment: Mapping[str, str],
) -> dict[str, object]:
    if environment.get(LIVE_ENV) != "1":
        return _blocked("live_gate_closed")
    if not _stable_id(root_id) or not _stable_id(file_id):
        return _blocked("invalid_scope")
    if not _scope_contains(managed_scope_path, root_id):
        return _blocked("scope_unverified")
    try:
        if version("p115client") != EXPECTED_P115CLIENT_VERSION:
            return _blocked("client_version_unverified")
    except Exception:  # noqa: BLE001 - package metadata is a gate only
        return _blocked("client_version_unverified")
    return asyncio.run(
        _run(root_id=root_id, file_id=file_id, cookie_path=cookie_path)
    )


async def _run(*, root_id: str, file_id: str, cookie_path: Path) -> dict[str, object]:
    temporary = tempfile.TemporaryDirectory(prefix="wa-strm-playback-live-")
    database = None
    client: httpx.AsyncClient | None = None
    try:
        base = Path(temporary.name)
        database = create_database(f"sqlite+aiosqlite:///{base / 'acceptance.db'}")
        await initialize_database(database.engine)
        password = secrets.token_urlsafe(24)
        password_hash = PasswordHash.recommended()
        playback = P115LivePlaybackGateway(
            database.session_factory,
            CookieProvider(cookie_path),
            max_concurrency=1,
        )
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
            strm_playback_enabled=True,
            strm_playback_contract_verified=True,
            strm_output_root=base / "strm-output",
            strm_playback_url_prefix=PLAYBACK_PREFIX,
            strm_playback_allowed_networks=("127.0.0.0/8",),
            strm_playback_gateway=playback,
        )
        app.state.organization_target_root_id = root_id
        app.state.organization_cookie_provider = CookieProvider(cookie_path)
        app_client = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=app_client, base_url="http://app.test")
        headers = await _login(client, password)
        configured = await client.put(
            "/api/v1/libraries/strm-playback-live/configuration",
            headers=headers,
            json={
                "name": "STRM Playback Fixture",
                "root_directory_id": root_id,
                "revision": 0,
            },
        )
        _expect(configured, "configuration_failed")
        verified = await client.post(
            "/api/v1/libraries/strm-playback-live/verify-scope", headers=headers
        )
        _expect(verified, "scope_verification_failed")
        scan = await _scan(client, headers, "strm-playback-live-scan")
        generated = await client.post(
            "/api/v1/libraries/strm-playback-live/strm-generation",
            headers=headers,
            json={"source_scan_run_id": scan["run_id"]},
        )
        _expect(generated, "generation_failed")
        manifest = await client.get(
            "/api/v1/libraries/strm-playback-live/strm-manifest", headers=headers
        )
        _expect(manifest, "manifest_failed")
        items = manifest.json().get("items")
        if not isinstance(items, list) or len(items) != 1:
            return _blocked("fixture_manifest_count_invalid")
        item = items[0]
        manifest_id = item.get("manifest_id")
        if not isinstance(manifest_id, str):
            return _blocked("fixture_manifest_invalid")

        head = await client.head(
            f"/api/v1/strm/play/{manifest_id}", headers=headers
        )
        get = await client.get(
            f"/api/v1/strm/play/{manifest_id}",
            headers={**headers, "Range": "bytes=0-0"},
        )
        out_of_scope = await client.get(
            "/api/v1/strm/play/strm_foreign_manifest_id_0001", headers=headers
        )
        _expect_status(head, 200, "playback_head_failed")
        _expect_status(get, 206, "playback_range_failed")
        _expect_status(out_of_scope, 404, "playback_scope_failed")
        return {
            "status": "success",
            "root_directory_id": root_id,
            "file_id": file_id,
            "verify_scope": {
                "verified": verified.json().get("verified"),
                "enabled": verified.json().get("enabled"),
            },
            "scan": _scan_public(scan),
            "generation": {
                "status_code": generated.status_code,
                "failed": generated.json().get("failed"),
            },
            "playback": {
                "head": _response_public(head),
                "get_range": _response_public(get),
                "out_of_scope_status": out_of_scope.status_code,
            },
            "output_root_is_temporary": True,
            "permanent_delete_used": False,
        }
    except AcceptanceError as error:
        return _blocked(error.code)
    finally:
        if client is not None:
            await client.aclose()
        if database is not None:
            await database.engine.dispose()
        temporary.cleanup()


async def _login(client: httpx.AsyncClient, password: str) -> dict[str, str]:
    response = await client.post("/api/v1/auth/login", json={"password": password})
    _expect(response, "login_failed")
    token = response.json().get("csrf_token")
    if not isinstance(token, str) or not token:
        raise AcceptanceError("csrf_missing")
    return {"X-CSRF-Token": token}


async def _scan(
    client: httpx.AsyncClient, headers: Mapping[str, str], idempotency_key: str
) -> dict[str, object]:
    response = await client.post(
        "/api/v1/libraries/strm-playback-live/scan",
        headers=headers,
        json={"idempotency_key": idempotency_key},
    )
    _expect(response, "scan_failed")
    body = response.json()
    if body.get("complete") is not True:
        raise AcceptanceError("scan_incomplete")
    return body


def _response_public(response: httpx.Response) -> dict[str, object]:
    allowed = {
        key: response.headers[key]
        for key in (
            "accept-ranges",
            "content-length",
            "content-range",
            "content-type",
        )
        if key in response.headers
    }
    return {"status_code": response.status_code, "headers": allowed}


def _expect(response: httpx.Response, code: str) -> None:
    if response.status_code >= 400:
        raise AcceptanceError(code)


def _expect_status(response: httpx.Response, expected: int, code: str) -> None:
    if response.status_code != expected:
        raise AcceptanceError(code)


def _scope_contains(path: Path, root_id: str) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    values = value.get("parent_ids") if isinstance(value, dict) else None
    return isinstance(values, list) and root_id in {str(item) for item in values}


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


def _blocked(code: str) -> dict[str, object]:
    return {"status": "blocked", "error_code": code, "write_started": False}


class AcceptanceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _FakeClient:
    async def aclose(self):
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run bounded live STRM playback acceptance")
    parser.add_argument("--root-id", required=True)
    parser.add_argument("--file-id", required=True)
    parser.add_argument("--cookie-path", required=True, type=Path)
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

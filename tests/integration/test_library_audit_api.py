import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.models import AgentToken, AuditRecord
from watch_assistant.security import SecurityManager

WEB_PASSWORD = "library-audit-password"


class _FakeClient:
    async def aclose(self):
        return None


async def _client(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    await initialize_database(database.engine)
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-one",
                name="PRIVATE LIBRARY",
                root_directory_id="root-one",
                scope_verified=True,
                enabled=True,
                revision=4,
            )
        )
        await session.flush()
        session.add(
            LibraryScanRun(
                id="scan-one",
                library_id="library-one",
                root_directory_id="root-one",
                idempotency_key="scan-key",
                scan_mode="tree",
                state="completed",
                complete=True,
                snapshot_revision=7,
                expected_total=2,
                pages_read=2,
                items_seen=2,
                added_count=2,
            )
        )
        await session.flush()
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-one",
                object_type="file",
                object_id="file-one",
                parent_id="root-one",
                name="private-title.mkv",
                path="/private/secret/title.mkv",
                is_directory=False,
                size_bytes=123,
                modified_at=now,
            )
        )
        session.add(
            LibraryScanEntry(
                scan_run_id="scan-one",
                object_type="directory",
                object_id="directory-one",
                parent_id="root-one",
                name="private-dir",
                path="/private/secret",
                is_directory=True,
            )
        )
        session.add(
            LibraryScanCheckpoint(
                scan_run_id="scan-one",
                page=2,
                items_seen=2,
                cursor_json=json.dumps(
                    {
                        "version": 2,
                        "directory_totals": {"root-one": 2, "directory-one": 0},
                        "expected_total": 2,
                        "pending": [],
                        "visited": ["root-one", "directory-one"],
                    }
                ),
            )
        )
        session.add(
            AuditRecord(
                id="audit-one",
                timestamp=now,
                event_code="agent.token.created",
                event_version=1,
                title_zh="Agent Token 已创建",
                message_zh="Token 已创建。",
                suggestion_zh="请妥善保管。",
                status="completed",
                request_id="req-one",
                correlation_id="corr-one",
                actor_type="session",
                actor_id="session-one",
                resource_type="agent_token",
                resource_id="agent-one",
                context_json='{"scopes":["library:read"]}',
            )
        )
        await session.commit()
    password_hash = PasswordHash.recommended()
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=_FakeClient(),
        pansou_client=_FakeClient(),
        security_manager=SecurityManager(
            web_password_hash=password_hash.hash(WEB_PASSWORD),
            script_token_hash=password_hash.hash("unused-script-token"),
        ),
        frontend_dir=tmp_path / "missing",
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return client, database


@pytest.mark.integration
async def test_library_media_and_audit_are_authenticated_bounded_and_redacted(tmp_path):
    client, database = await _client(tmp_path)
    assert (await client.get("/api/v1/libraries")).status_code == 401
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    assert login.status_code == 200

    libraries = await client.get("/api/v1/libraries", params={"limit": 1})
    assert libraries.status_code == 200
    assert libraries.json()["items"][0]["library_id"] == "library-one"
    assert "PRIVATE LIBRARY" in libraries.text
    assert "root-one" in libraries.text

    media = await client.get("/api/v1/media", params={"library": "library-one"})
    assert media.status_code == 200
    assert media.json()["items"][0]["media_id"] == "file:file-one"
    assert "/private/secret" not in media.text
    assert "pickcode" not in media.text

    detail = await client.get("/api/v1/media/file:file-one")
    assert detail.status_code == 200
    assert detail.json()["name"] == "private-title.mkv"

    audit = await client.get("/api/v1/audit", params={"event_code": "agent.token.created"})
    assert audit.status_code == 200
    assert audit.json()["items"][0]["audit_id"] == "audit-one"
    assert "scopes" not in audit.text

    await client.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_inventory_reports_freshness_and_only_exact_identity_blocks(tmp_path):
    client, database = await _client(tmp_path)
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    assert login.status_code == 200

    inventory = await client.get("/api/v1/libraries/library-one/inventory")
    assert inventory.status_code == 200
    body = inventory.json()
    assert body["file_count"] == 1
    assert body["freshness"]["complete"] is True
    assert body["freshness"]["status"] == "fresh"
    assert body["duplicate_group_count"] == 0

    events = await client.get("/api/v1/libraries/library-one/inventory/events")
    assert events.status_code == 200
    assert events.json()["items"] == []

    exact = await client.get(
        "/api/v1/libraries/library-one/inventory/check",
        params={"object_id": "file-one"},
    )
    assert exact.status_code == 200
    assert exact.json()["decision"] == "exact_duplicate"
    assert exact.json()["matched_object_count"] == 1

    csrf = login.json()["csrf_token"]
    bound = await client.put(
        "/api/v1/libraries/library-one/inventory/identities/file-one",
        headers={"X-CSRF-Token": csrf},
        json={"tmdb_id": 7, "media_type": "movie", "revision": 0},
    )
    assert bound.status_code == 200
    assert bound.json()["revision"] == 1
    media_duplicate = await client.get(
        "/api/v1/libraries/library-one/inventory/check",
        params={"tmdb_id": 7, "media_type": "movie"},
    )
    assert media_duplicate.status_code == 200
    assert media_duplicate.json()["decision"] == "media_duplicate"
    assert media_duplicate.json()["matched_object_count"] == 1

    stale = await client.put(
        "/api/v1/libraries/library-one/inventory/identities/file-one",
        headers={"X-CSRF-Token": csrf},
        json={"tmdb_id": 8, "media_type": "movie", "revision": 0},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "library_identity_conflict"

    await client.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_inventory_rejects_requeued_scan_even_when_created_earlier(tmp_path):
    client, database = await _client(tmp_path)
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    assert login.status_code == 200
    csrf = login.json()["csrf_token"]

    async with database.session_factory() as session:
        current = await session.get(LibraryScanRun, "scan-one")
        assert current is not None
        assert current.created_at is not None
        assert current.updated_at is not None
        session.add(
            LibraryScanRun(
                id="scan-requeued",
                library_id="library-one",
                root_directory_id="root-one",
                idempotency_key="scan-requeued-key",
                scan_mode="tree",
                state="queued",
                complete=False,
                snapshot_revision=None,
                created_at=current.created_at - timedelta(minutes=1),
                updated_at=current.updated_at + timedelta(minutes=1),
            )
        )
        await session.commit()

    inventory = await client.get("/api/v1/libraries/library-one/inventory")
    assert inventory.status_code == 200
    assert inventory.json()["scan_run_id"] is None
    assert inventory.json()["freshness"]["complete"] is False
    assert inventory.json()["freshness"]["status"] == "incomplete"

    check = await client.get(
        "/api/v1/libraries/library-one/inventory/check",
        params={"object_id": "file-one"},
    )
    assert check.status_code == 200
    assert check.json()["decision"] == "index_incomplete"
    assert check.json()["matched_object_count"] == 0

    bound = await client.put(
        "/api/v1/libraries/library-one/inventory/identities/file-one",
        headers={"X-CSRF-Token": csrf},
        json={"tmdb_id": 7, "media_type": "movie", "revision": 0},
    )
    assert bound.status_code == 409
    assert bound.json()["detail"] == "library_inventory_incomplete"

    await client.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_media_reads_hide_old_or_unverified_snapshots(tmp_path):
    client, database = await _client(tmp_path)
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    assert login.status_code == 200

    async def assert_hidden() -> None:
        libraries = await client.get("/api/v1/libraries")
        assert libraries.status_code == 200
        assert libraries.json()["items"][0]["latest_scan"] is None

        library = await client.get("/api/v1/libraries/library-one")
        assert library.status_code == 200
        assert library.json()["latest_scan"] is None

        library_media = await client.get("/api/v1/libraries/library-one/media")
        assert library_media.status_code == 200
        assert library_media.json()["items"] == []

        media = await client.get("/api/v1/media", params={"library": "library-one"})
        assert media.status_code == 200
        assert media.json()["items"] == []

        detail = await client.get("/api/v1/media/file:file-one")
        assert detail.status_code == 404
        assert detail.json()["detail"] == "media_not_found"

    async with database.session_factory() as session:
        current = await session.get(LibraryScanRun, "scan-one")
        assert current is not None
        assert current.created_at is not None
        assert current.updated_at is not None
        unsettled = LibraryScanRun(
            id="scan-unsettled",
            library_id="library-one",
            root_directory_id="root-one",
            idempotency_key="scan-unsettled-key",
            scan_mode="tree",
            state="queued",
            complete=False,
            snapshot_revision=None,
            created_at=current.created_at - timedelta(minutes=1),
            updated_at=current.updated_at + timedelta(minutes=1),
        )
        session.add(unsettled)
        await session.commit()

    for state, complete in (
        ("queued", False),
        ("running", False),
        ("completed", False),
    ):
        async with database.session_factory() as session:
            unsettled = await session.get(LibraryScanRun, "scan-unsettled")
            assert unsettled is not None
            unsettled.state = state
            unsettled.complete = complete
            await session.commit()
        await assert_hidden()

    async with database.session_factory() as session:
        unverified = await session.get(LibraryScanRun, "scan-unsettled")
        assert unverified is not None
        unverified.state = "completed"
        unverified.complete = True
        unverified.snapshot_revision = 8
        unverified.expected_total = 0
        unverified.pages_read = 0
        unverified.items_seen = 0
        await session.commit()
    await assert_hidden()

    await client.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_inventory_rejects_failed_incomplete_and_wrong_scope_snapshots(tmp_path):
    client, database = await _client(tmp_path)
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    assert login.status_code == 200
    csrf = login.json()["csrf_token"]

    async def assert_blocked() -> None:
        inventory = await client.get("/api/v1/libraries/library-one/inventory")
        assert inventory.status_code == 200
        assert inventory.json()["freshness"]["complete"] is False

        check = await client.get(
            "/api/v1/libraries/library-one/inventory/check",
            params={"object_id": "file-one"},
        )
        assert check.status_code == 200
        assert check.json()["decision"] == "index_incomplete"
        assert check.json()["matched_object_count"] == 0

        bound = await client.put(
            "/api/v1/libraries/library-one/inventory/identities/file-one",
            headers={"X-CSRF-Token": csrf},
            json={"tmdb_id": 7, "media_type": "movie", "revision": 0},
        )
        assert bound.status_code == 409
        assert bound.json()["detail"] == "library_inventory_incomplete"

    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, "scan-one")
        assert run is not None
        run.state = "failed"
        await session.commit()
    await assert_blocked()

    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, "scan-one")
        assert run is not None
        run.state = "completed"
        run.complete = False
        await session.commit()
    await assert_blocked()

    async with database.session_factory() as session:
        run = await session.get(LibraryScanRun, "scan-one")
        assert run is not None
        run.complete = True
        run.root_directory_id = "other-root"
        await session.commit()
    await assert_blocked()

    await client.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_inventory_fails_closed_for_unsettled_and_duplicate_revisions(tmp_path):
    client, database = await _client(tmp_path)
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    assert login.status_code == 200

    async with database.session_factory() as session:
        session.add(
            LibraryScanRun(
                id="scan-pending",
                library_id="library-one",
                root_directory_id="root-one",
                idempotency_key="scan-pending-key",
                scan_mode="tree",
                state="running",
                complete=False,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()

    inventory = await client.get("/api/v1/libraries/library-one/inventory")
    assert inventory.status_code == 200
    assert inventory.json()["freshness"]["complete"] is False

    # 重复 revision 在写入层被唯一索引拒绝(迁移 072),歧义快照不可达:
    # 与 fixture 的 scan-key(revision=7)重复的 UPDATE 必须失败。
    from sqlalchemy.exc import IntegrityError

    async with database.session_factory() as session:
        pending = await session.get(LibraryScanRun, "scan-pending")
        assert pending is not None
        pending.state = "completed"
        pending.complete = True
        pending.snapshot_revision = 7
        pending.expected_total = 0
        pending.pages_read = 0
        pending.items_seen = 0
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    duplicate = await client.get("/api/v1/libraries/library-one/inventory")
    assert duplicate.status_code == 200
    # 重复 revision 未写入;scan-pending 仍为 running → 快照仍未就绪
    assert duplicate.json()["freshness"]["complete"] is False

    await client.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_media_endpoints_hide_completed_snapshots_after_scope_is_disabled(tmp_path):
    client, database = await _client(tmp_path)
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    assert login.status_code == 200

    async with database.session_factory() as session:
        library = await session.get(MediaLibrary, "library-one")
        assert library is not None
        library.enabled = False
        await session.commit()

    libraries = await client.get("/api/v1/libraries")
    assert libraries.status_code == 200
    assert libraries.json()["items"][0]["latest_scan"] is None

    media = await client.get("/api/v1/media", params={"library": "library-one"})
    assert media.status_code == 200
    assert media.json()["items"] == []

    detail = await client.get("/api/v1/media/file:file-one")
    assert detail.status_code == 404
    assert detail.json()["detail"] == "media_not_found"

    await client.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_library_scope_not_found_does_not_leak_other_library(tmp_path):
    client, database = await _client(tmp_path)
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    assert login.status_code == 200
    response = await client.get("/api/v1/libraries/does-not-exist")
    assert response.status_code == 404
    assert "PRIVATE LIBRARY" not in response.text
    await client.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_agent_library_scope_is_enforced_without_resource_leak(tmp_path):
    client, database = await _client(tmp_path)
    raw_token = "wa_at_library_scope_test"
    async with database.session_factory() as session:
        session.add(
            AgentToken(
                id="agent-scope",
                name="scope-test",
                token_digest=hashlib.sha256(raw_token.encode()).hexdigest(),
                token_prefix=raw_token[:16],
                scopes_json=json.dumps(["library:read"]),
                library_ids_json=json.dumps(["library-other"]),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    response = await client.get(
        "/api/v1/libraries/library-one",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert response.status_code == 404
    assert "PRIVATE LIBRARY" not in response.text
    listing = await client.get(
        "/api/v1/libraries",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert listing.status_code == 200
    assert listing.json()["items"] == []
    await client.aclose()
    await database.engine.dispose()

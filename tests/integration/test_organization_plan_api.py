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
    LibraryScanRun,
    MediaLibrary,
    OrganizationPlan,
)
from watch_assistant.security import SecurityManager

WEB_PASSWORD = "organization-review-password"


class _FakeClient:
    async def aclose(self):
        return None


async def _client(tmp_path: Path, *, enabled: bool = False):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'organization.db'}")
    await initialize_database(database.engine)
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-review",
                name="PRIVATE_LIBRARY_NAME",
                root_directory_id="root-private",
                scope_verified=True,
                enabled=True,
                revision=2,
            )
        )
        await session.flush()
        session.add(
            LibraryScanRun(
                id="scan-review",
                library_id="library-review",
                root_directory_id="root-private",
                idempotency_key="review-key",
                state="completed",
                complete=True,
                snapshot_revision=3,
            )
        )
        await session.flush()
        session.add(
            OrganizationPlan(
                id="plan-review",
                library_id="library-review",
                source_scan_run_id="scan-review",
                source_snapshot_revision=3,
                source_snapshot_json='[{"object_id":"remote-private","object_type":"file","parent_id":"parent-private","path":"/private/title.mkv","remote_version":"1"}]',
                target_root="Movies",
                actions_json='[{"object_id":"remote-private","target":"Movies/private-title"}]',
                basis_json='[{"reason":"pickcode-private"}]',
                preconditions_json='{"items":[],"library":{"library_id":"library-review"}}',
                rule_version="rule-v1",
                parser_version="parser-v1",
                matcher_version="matcher-v1",
                status="needs_review",
                revision=1,
                expires_at=now + timedelta(hours=1),
                plan_hash="a" * 64,
            )
        )
        await session.commit()
    password_hash = PasswordHash.recommended()
    security = SecurityManager(
        web_password_hash=password_hash.hash(WEB_PASSWORD),
        script_token_hash=password_hash.hash("unused-script-token"),
    )
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=_FakeClient(),
        pansou_client=_FakeClient(),
        security_manager=security,
        frontend_dir=tmp_path / "missing",
        organization_plan_enabled=enabled,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return client, database


@pytest.mark.integration
async def test_plan_review_api_is_authenticated_and_redacted(tmp_path):
    client, database = await _client(tmp_path, enabled=True)
    unauthenticated = await client.get("/api/v1/organization-plans")
    assert unauthenticated.status_code == 401

    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    assert login.status_code == 200
    csrf = login.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}

    listing = await client.get(
        "/api/v1/organization-plans", params={"status": "needs_review"}
    )
    assert listing.status_code == 200
    body = listing.json()
    assert body["items"][0]["plan_id"] == "plan-review"
    assert "PRIVATE_LIBRARY_NAME" not in listing.text
    assert "remote-private" not in listing.text
    assert "pickcode-private" not in listing.text
    assert "/private/title.mkv" not in listing.text
    assert "source_snapshot" not in listing.text

    missing_csrf = await client.post(
        "/api/v1/organization-plans/plan-review/confirm",
        json={"expected_revision": 1},
    )
    assert missing_csrf.status_code == 403

    confirmed = await client.post(
        "/api/v1/organization-plans/plan-review/confirm",
        json={"expected_revision": 1},
        headers=headers,
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "planned"
    assert confirmed.json()["revision"] == 2

    stale = await client.post(
        "/api/v1/organization-plans/plan-review/ignore",
        json={"expected_revision": 1},
        headers=headers,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_revision"
    assert "remote-private" not in stale.text

    aliased = await client.post(
        "/api/v1/organization-plans/plan-review/alias",
        json={"expected_revision": 2, "alias": "本地收藏"},
        headers=headers,
    )
    assert aliased.status_code == 200
    assert aliased.json()["alias"] == "本地收藏"
    assert aliased.json()["revision"] == 3

    invalid_alias = await client.post(
        "/api/v1/organization-plans/plan-review/alias",
        json={"expected_revision": 3, "alias": "https://remote.invalid"},
        headers=headers,
    )
    assert invalid_alias.status_code == 422
    await client.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_plan_review_cursor_is_bounded_and_confirm_is_local_only(tmp_path):
    client, database = await _client(tmp_path, enabled=True)
    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    headers = {"X-CSRF-Token": login.json()["csrf_token"]}
    too_large = await client.get("/api/v1/organization-plans", params={"limit": 101})
    assert too_large.status_code == 422
    preview = await client.get("/api/v1/organization-plans/plan-review")
    assert preview.status_code == 200
    assert set(preview.json()) == {
        "plan_id",
        "plan_hash",
        "status",
        "revision",
        "expires_at",
        "source_count",
        "action_count",
        "precondition_count",
        "requires_web_approval",
        "high_risk_action_threshold",
        "alias",
    }
    ignored = await client.post(
        "/api/v1/organization-plans/plan-review/ignore",
        json={"expected_revision": 1},
        headers=headers,
    )
    assert ignored.status_code == 200
    assert ignored.json()["status"] == "ignored"
    await client.aclose()
    await database.engine.dispose()


@pytest.mark.integration
async def test_plan_review_is_disabled_by_default_for_reads_and_mutations(tmp_path):
    client, database = await _client(tmp_path)
    health = await client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["organization_plan_enabled"] is False

    login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
    assert login.status_code == 200
    headers = {"X-CSRF-Token": login.json()["csrf_token"]}

    listing = await client.get("/api/v1/organization-plans")
    assert listing.status_code == 503
    assert listing.json()["detail"] == "organization_plan_disabled"

    confirm = await client.post(
        "/api/v1/organization-plans/plan-review/confirm",
        json={"expected_revision": 1},
        headers=headers,
    )
    assert confirm.status_code == 503
    assert confirm.json()["detail"] == "organization_plan_disabled"
    await client.aclose()
    await database.engine.dispose()

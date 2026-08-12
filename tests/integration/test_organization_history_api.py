import hashlib
import json
import secrets
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
    OrganizationHistoryEntry,
    OrganizationPlan,
)
from watch_assistant.models import (
    AgentToken,
    OrganizationOperation,
    OrganizationOperationStatus,
)
from watch_assistant.security import SecurityManager


class _FakeClient:
    async def aclose(self):
        return None


async def _client(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'history.db'}")
    await initialize_database(database.engine)
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        for suffix, library_id in (("allowed", "library-allowed"), ("hidden", "library-hidden")):
            scan_id = f"scan-{suffix}"
            plan_id = f"plan-{suffix}"
            operation_id = f"operation-{suffix}"
            session.add(
                MediaLibrary(
                    id=library_id,
                    name=f"Library {suffix}",
                    root_directory_id=f"root-{suffix}",
                    scope_verified=True,
                    enabled=True,
                    revision=1,
                )
            )
            await session.flush()
            session.add(
                LibraryScanRun(
                    id=scan_id,
                    library_id=library_id,
                    root_directory_id=f"root-{suffix}",
                    idempotency_key=f"scan-key-{suffix}",
                    state="completed",
                    complete=True,
                    snapshot_revision=1,
                )
            )
            await session.flush()
            session.add(
                OrganizationPlan(
                    id=plan_id,
                    library_id=library_id,
                    source_scan_run_id=scan_id,
                    source_snapshot_revision=1,
                    source_snapshot_json="[]",
                    target_root="Movies",
                    actions_json="[]",
                    basis_json="[]",
                    preconditions_json="{}",
                    rule_version="rule-v1",
                    parser_version="parser-v1",
                    matcher_version="matcher-v1",
                    status="planned",
                    revision=1,
                    expires_at=now + timedelta(hours=1),
                    plan_hash=("a" if suffix == "allowed" else "b") * 64,
                )
            )
            await session.flush()
            session.add(
                OrganizationOperation(
                    id=operation_id,
                    plan_id=plan_id,
                    plan_revision=1,
                    idempotency_key=f"operation-key-{suffix}",
                    status=OrganizationOperationStatus.ORGANIZED,
                    revision=1,
                    finished_at=now,
                )
            )
            await session.flush()
            session.add(
                OrganizationHistoryEntry(
                    id=f"history-{suffix}",
                    operation_id=operation_id,
                    plan_id=plan_id,
                    source_object_id=f"source-{suffix}",
                    source_directory_id=f"source-dir-{suffix}",
                    target_directory_id=f"target-dir-{suffix}",
                    title=f"Title {suffix}",
                    media_type="movie",
                    source_name=f"source-{suffix}.mkv",
                    target_path=f"Movies/{suffix}.mkv",
                    status="organized",
                    completed_at=now,
                )
            )
        await session.commit()

    agent_token = secrets.token_urlsafe(24)
    web_password = secrets.token_urlsafe(24)
    legacy_token = secrets.token_urlsafe(24)
    async with database.session_factory() as session:
        session.add(
            AgentToken(
                id="agent-history-scope",
                name="history-scope",
                token_digest=hashlib.sha256(agent_token.encode()).hexdigest(),
                token_prefix=agent_token[:16],
                # 组织历史含目标路径等敏感详情,读权限已收敛到 organize:plan。
                scopes_json=json.dumps(["organize:plan"]),
                library_ids_json=json.dumps(["library-allowed"]),
                expires_at=now + timedelta(hours=1),
                created_at=now,
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
            web_password_hash=password_hash.hash(web_password),
            script_token_hash=password_hash.hash(legacy_token),
        ),
        frontend_dir=tmp_path / "missing",
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    )
    return client, database, agent_token, web_password, legacy_token


@pytest.mark.integration
async def test_organization_history_is_scoped_by_agent_library_and_preserves_other_auth(
    tmp_path: Path,
):
    client, database, agent_token, web_password, legacy_token = await _client(tmp_path)
    try:
        agent_headers = {"Authorization": f"Bearer {agent_token}"}
        scoped = await client.get("/api/v1/organization-history", headers=agent_headers)
        assert scoped.status_code == 200
        assert {item["id"] for item in scoped.json()["items"]} == {"history-allowed"}

        allowed_detail = await client.get(
            "/api/v1/organization-history/history-allowed", headers=agent_headers
        )
        assert allowed_detail.status_code == 200
        hidden_detail = await client.get(
            "/api/v1/organization-history/history-hidden", headers=agent_headers
        )
        assert hidden_detail.status_code == 404
        assert hidden_detail.json()["detail"]["code"] == "history_not_found"

        login = await client.post("/api/v1/auth/login", json={"password": web_password})
        web_headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        web_listing = await client.get("/api/v1/organization-history", headers=web_headers)
        assert {item["id"] for item in web_listing.json()["items"]} == {
            "history-allowed",
            "history-hidden",
        }

        legacy_listing = await client.get(
            "/api/v1/organization-history",
            headers={"Authorization": f"Bearer {legacy_token}"},
        )
        assert {item["id"] for item in legacy_listing.json()["items"]} == {
            "history-allowed",
            "history-hidden",
        }
    finally:
        await client.aclose()
        await database.engine.dispose()

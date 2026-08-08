import asyncio
import json
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash
from sqlalchemy import select

from watch_assistant.adapters.p115_library import DirectoryPage, LibraryEntry, ScanState
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    OrganizationPlan,
)
from watch_assistant.models import OrganizationOperation
from watch_assistant.schemas import MediaType
from watch_assistant.security import SecurityManager
from watch_assistant.services.media_matcher import MediaKind, TmdbCandidate


class _ReadinessFalseTaskAdapter:
    async def ensure_available(self) -> bool:
        return False

    async def aclose(self) -> None:
        return None


class _TmdbClient:
    async def search_candidates(self, _query):
        return [
            TmdbCandidate(
                tmdb_id=42,
                media_type=MediaType.MOVIE,
                title="The Office",
                kind=MediaKind.MOVIE,
                release_year=2005,
                origin_countries=("US",),
            )
        ]

    async def aclose(self) -> None:
        return None


class _NoCandidatesThenManualTmdbClient:
    def __init__(self) -> None:
        self.search_count = 0

    async def search_candidates(self, _query):
        self.search_count += 1
        if self.search_count == 1:
            return []
        return [
            TmdbCandidate(
                tmdb_id=42,
                media_type=MediaType.MOVIE,
                title="The Office",
                kind=MediaKind.MOVIE,
                release_year=2005,
                origin_countries=("US",),
            )
        ]

    async def aclose(self) -> None:
        return None


class _FakeDirectoryGateway:
    def __init__(self, *_args, **_kwargs) -> None:
        self.calls: list[tuple[str, int, int]] = []

    async def list_directory(
        self, directory_id: str, *, page: int = 1, page_size: int = 100
    ) -> DirectoryPage:
        self.calls.append((directory_id, page, page_size))
        pages = {
            ("9000", 1): (
                self._directory("8000", "9000", "library"),
            ),
            ("8000", 1): (
                self._directory("8001", "8000", "movie"),
            ),
            ("8001", 1): (
                self._directory("8002", "8001", "western"),
            ),
            ("8002", 1): (
                self._directory(
                    "8003", "8002", "The Office (2005) {tmdb-42}"
                ),
            ),
            ("8003", 1): (),
            ("1000", 1): (
                LibraryEntry(
                    directory_id=None,
                    file_id="7000",
                    parent_id="1000",
                    name="The.Office.2005.1080p.mkv",
                    is_directory=False,
                    size_bytes=10_000_000,
                    modified_at=None,
                    pickcode=None,
                    path="incoming/The.Office.2005.1080p.mkv",
                ),
            ),
        }
        items = pages[(directory_id, page)]
        return DirectoryPage(
            items=items,
            page=page,
            page_count=1,
            total=len(items),
            scan_complete=True,
            state=ScanState.COMPLETE,
            has_more=False,
            next_page=None,
            terminal=True,
        )

    async def get_file_detail(self, file_id: str) -> LibraryEntry:
        if file_id != "7000":
            raise AssertionError(f"unexpected file detail: {file_id}")
        return LibraryEntry(
            directory_id=None,
            file_id=file_id,
            parent_id="1000",
            name="The.Office.2005.1080p.mkv",
            is_directory=False,
            size_bytes=10_000_000,
            modified_at=None,
            pickcode="fixture-pickcode",
            path=None,
        )

    @staticmethod
    def _directory(directory_id: str, parent_id: str, name: str) -> LibraryEntry:
        return LibraryEntry(
            directory_id=directory_id,
            file_id=None,
            parent_id=parent_id,
            name=name,
            is_directory=True,
            size_bytes=None,
            modified_at=None,
            pickcode=None,
            path=None,
        )


def _app(
    database,
    *,
    task_adapter,
    security,
    tmdb_client=None,
    organization_write_enabled=False,
    organization_write_contract_verified=None,
):
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=tmdb_client or _TmdbClient(),
        pansou_client=_TmdbClient(),
        security_manager=security,
        frontend_dir=Path("missing-frontend"),
        task_adapter=task_adapter,
        organization_plan_enabled=True,
        organization_execution_enabled=True,
        organization_write_enabled=organization_write_enabled,
        organization_write_contract_verified=organization_write_contract_verified,
    )
    app.state.organization_target_root_id = "9000"
    app.state.p115_browsed_directory_ids = {"1000", "9000"}
    return app


async def _login(client: httpx.AsyncClient, password: str) -> dict[str, str]:
    response = await client.post("/api/v1/auth/login", json={"password": password})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"]}


async def _add_target_tree_evidence(database, plan_id: str) -> None:
    """Complete the local target-tree fixture used by execution-scope checks."""

    async with database.session_factory() as session:
        plan = await session.get(OrganizationPlan, plan_id)
        assert plan is not None
        run = await session.get(LibraryScanRun, plan.source_scan_run_id)
        assert run is not None
        checkpoint = await session.get(LibraryScanCheckpoint, run.id)
        assert checkpoint is not None
        target_rows = (
            ("9000", run.root_directory_id, "target-root", "target-root"),
            ("8000", "9000", "library", "library"),
            ("8001", "8000", "movie", "library/movie"),
            (
                "8002",
                "8001",
                "western",
                "library/movie/western",
            ),
            (
                "8003",
                "8002",
                "The Office (2005) {tmdb-42}",
                "library/movie/western/The Office (2005) {tmdb-42}",
            ),
        )
        existing_ids = set(
            (
                await session.scalars(
                    select(LibraryScanEntry.object_id).where(
                        LibraryScanEntry.scan_run_id == run.id,
                        LibraryScanEntry.object_type == "directory",
                    )
                )
            ).all()
        )
        for object_id, parent_id, name, path in target_rows:
            if object_id in existing_ids:
                continue
            session.add(
                LibraryScanEntry(
                    scan_run_id=run.id,
                    object_type="directory",
                    object_id=object_id,
                    parent_id=parent_id,
                    name=name,
                    path=path,
                    is_directory=True,
                )
            )
        await session.flush()
        entries = list(
            (
                await session.scalars(
                    select(LibraryScanEntry).where(
                        LibraryScanEntry.scan_run_id == run.id
                    )
                )
            ).all()
        )
        visited = [run.root_directory_id]
        directory_totals = {run.root_directory_id: 0}
        for entry in entries:
            assert isinstance(entry.parent_id, str)
            directory_totals[entry.parent_id] = (
                directory_totals.get(entry.parent_id, 0) + 1
            )
            if entry.is_directory:
                visited.append(entry.object_id)
                directory_totals.setdefault(entry.object_id, 0)
        assert set(directory_totals) == set(visited)
        run.scan_mode = "tree"
        run.expected_total = len(entries)
        run.items_seen = len(entries)
        checkpoint.page = run.pages_read
        checkpoint.items_seen = len(entries)
        checkpoint.cursor_json = json.dumps(
            {
                "version": 2,
                "directory_totals": directory_totals,
                "expected_total": len(entries),
                "pending": [],
                "visited": visited,
            }
        )
        await session.commit()


@pytest.mark.integration
async def test_legacy_contract_flag_does_not_open_organization_write_runtime(
    tmp_path: Path,
):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'contract-boundary.db'}")
    password_hash = PasswordHash.recommended()
    app = _app(
        database,
        task_adapter=_ReadinessFalseTaskAdapter(),
        security=SecurityManager(
            web_password_hash=password_hash.hash("contract-boundary-password"),
            script_token_hash=password_hash.hash("unused-script-token"),
        ),
        organization_write_enabled=True,
        organization_write_contract_verified=True,
    )

    assert app.state.organization_contract.verified is False
    assert app.state.organization_write_contract_verified is False
    await database.engine.dispose()


@pytest.mark.integration
async def test_manual_organization_flow_fails_closed_unready_p115_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "watch_assistant.app.P115ReadOnlyDirectoryGateway", _FakeDirectoryGateway
    )
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'organization-flow.db'}")
    await initialize_database(database.engine)
    password = "organization-flow-password"
    password_hash = PasswordHash.recommended()
    password_hash_value = password_hash.hash(password)

    app = _app(
        database,
        task_adapter=_ReadinessFalseTaskAdapter(),
        security=SecurityManager(
            web_password_hash=password_hash_value,
            script_token_hash=password_hash.hash("unused-script-token"),
        ),
    )
    async with app.router.lifespan_context(app):
        assert app.state.p115_ready is False
        assert hasattr(app.state, "organization_scheduler")
        assert hasattr(app.state, "organization_automation_service")
        assert not hasattr(app.state, "organization_worker")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://app.test"
        ) as client:
            headers = await _login(client, password)
            current = await client.get(
                "/api/v1/settings/organization", headers=headers
            )
            assert current.status_code == 200
            configured = await client.patch(
                "/api/v1/settings/organization",
                headers=headers,
                json={
                    "revision": current.json()["revision"],
                    "schedule_enabled": False,
                    "source_directory_ids": ["1000"],
                    "target_directory_id": "9000",
                    "rename_enabled": True,
                },
            )
            assert configured.status_code == 200

            queued = await client.post(
                "/api/v1/settings/organization/run-now", json={}, headers=headers
            )
            assert queued.status_code == 200
            run_id = queued.json()["run_id"]
            result = None
            for _ in range(100):
                result_response = await client.get(
                    "/api/v1/settings/organization/result", headers=headers
                )
                result = result_response.json()
                if result["run_id"] == run_id and result["finished_at"] is not None:
                    break
                await asyncio.sleep(0.01)

            assert result is not None
            assert result["run_id"] == run_id
            assert result["scanned_count"] == 1
            assert result["plan_count"] == 1
            assert result["queued_count"] == 0
            assert result["blocked_count"] == 0

            plans = await client.get(
                "/api/v1/organization-plans",
                params={"status": "needs_review", "limit": 20},
                headers=headers,
            )
            assert plans.status_code == 200
            assert len(plans.json()["items"]) == 1
            plan = plans.json()["items"][0]
            assert plan["status"] == "needs_review"
            await _add_target_tree_evidence(database, plan["plan_id"])

            confirmed = await client.post(
                f"/api/v1/organization-plans/{plan['plan_id']}/confirm-and-operation",
                headers=headers,
                json={
                    "expected_revision": plan["revision"],
                    "idempotency_key": "organization-flow-confirm",
                    "confirm": True,
                },
            )
            assert confirmed.status_code == 503
            assert confirmed.json()["detail"] == {
                "code": "organization_execution_unavailable",
                "message": "整理执行能力暂不可用，本次操作未排队",
            }

    restarted = _app(
        database,
        task_adapter=_ReadinessFalseTaskAdapter(),
        security=SecurityManager(
            web_password_hash=password_hash_value,
            script_token_hash=password_hash.hash("unused-script-token-2"),
        ),
    )
    async with restarted.router.lifespan_context(restarted):
        assert restarted.state.p115_ready is False
        assert hasattr(restarted.state, "organization_scheduler")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://app.test"
        ) as client:
            headers = await _login(client, password)
            persisted = await client.get(
                f"/api/v1/organization-plans/{plan['plan_id']}/operation",
                headers=headers,
            )
            assert persisted.status_code == 404
            assert persisted.json()["detail"]["code"] == "operation_not_found"

    await database.engine.dispose()


@pytest.mark.integration
async def test_no_candidates_can_be_searched_selected_and_queued_as_one_confirmed_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "watch_assistant.app.P115ReadOnlyDirectoryGateway", _FakeDirectoryGateway
    )
    tmdb = _NoCandidatesThenManualTmdbClient()
    password = "organization-manual-candidate-password"
    password_hash = PasswordHash.recommended()
    app = _app(
        create_database(f"sqlite+aiosqlite:///{tmp_path / 'manual-candidate.db'}"),
        task_adapter=_ReadinessFalseTaskAdapter(),
        security=SecurityManager(
            web_password_hash=password_hash.hash(password),
            script_token_hash=password_hash.hash("unused-manual-candidate-token"),
        ),
        tmdb_client=tmdb,
    )
    await initialize_database(app.state.database.engine)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        headers = await _login(client, password)
        current = await client.get(
            "/api/v1/settings/organization", headers=headers
        )
        assert current.status_code == 200
        configured = await client.patch(
            "/api/v1/settings/organization",
            headers=headers,
            json={
                "revision": current.json()["revision"],
                "schedule_enabled": False,
                "source_directory_ids": ["1000"],
                "target_directory_id": "9000",
                "rename_enabled": True,
            },
        )
        assert configured.status_code == 200
        queued = await client.post(
            "/api/v1/settings/organization/run-now", json={}, headers=headers
        )
        assert queued.status_code == 200
        run_id = queued.json()["run_id"]
        for _ in range(100):
            result = (
                await client.get(
                    "/api/v1/settings/organization/result", headers=headers
                )
            ).json()
            if result["run_id"] == run_id and result["finished_at"] is not None:
                break
            await asyncio.sleep(0.01)
        assert result["plan_count"] == 1, result

        listing = await client.get(
            "/api/v1/organization-plans",
            params={"status": "needs_review"},
            headers=headers,
        )
        assert listing.status_code == 200
        plan = listing.json()["items"][0]
        assert plan["can_execute"] is False
        assert plan["executable_action_count"] == 0
        assert plan["review_action_count"] == 1
        await _add_target_tree_evidence(app.state.database, plan["plan_id"])

        searched = await client.post(
            f"/api/v1/organization-plans/{plan['plan_id']}/candidate-search",
            headers=headers,
            json={
                "expected_revision": plan["revision"],
                "source_index": 0,
                "query": "The Office 2005",
            },
        )
        assert searched.status_code == 200
        searched_plan = searched.json()
        assert searched_plan["revision"] == plan["revision"] + 1
        assert searched_plan["can_execute"] is False
        assert searched_plan["candidates"][0]["tmdb_id"] == 42

        selected = await client.post(
            f"/api/v1/organization-plans/{plan['plan_id']}/candidate",
            headers=headers,
            json={
                "expected_revision": searched_plan["revision"],
                "source_object_id": searched_plan["candidates"][0][
                    "source_object_id"
                ],
                "tmdb_id": 42,
            },
        )
        assert selected.status_code == 200, selected.text
        selected_plan = selected.json()
        assert selected_plan["plan_id"] == plan["plan_id"]
        assert selected_plan["status"] == "planned"
        assert selected_plan["action_count"] == 1
        assert selected_plan["executable_action_count"] == 1
        assert selected_plan["review_action_count"] == 0
        assert selected_plan["can_execute"] is True

        confirmed = await client.post(
            f"/api/v1/organization-plans/{plan['plan_id']}/confirm-and-operation",
            headers=headers,
            json={
                "expected_revision": selected_plan["revision"],
                "idempotency_key": "manual-candidate-confirm",
                "confirm": True,
            },
        )
        assert confirmed.status_code == 503, confirmed.text
        assert confirmed.json()["detail"] == {
            "code": "organization_execution_unavailable",
            "message": "整理执行能力暂不可用，本次操作未排队",
        }
        assert not hasattr(app.state, "organization_worker")
    await app.state.database.engine.dispose()


class _ReadyTaskAdapter:
    async def ensure_available(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@pytest.mark.integration
async def test_scheduled_automation_queues_high_confidence_plan_when_write_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A scheduled pass with auto-execution enabled confirms and queues the
    high-confidence partition; nothing requires a manual confirm step."""
    monkeypatch.setattr(
        "watch_assistant.app.P115ReadOnlyDirectoryGateway", _FakeDirectoryGateway
    )
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'automation-scheduled.db'}")
    await initialize_database(database.engine)
    password = "automation-scheduled-password"
    password_hash = PasswordHash.recommended()
    password_hash_value = password_hash.hash(password)

    from watch_assistant.adapters.p115_library_write_contract import (
        OrganizationContractEvidence,
        OrganizationWriteCapability,
        P115OrganizationContract,
    )

    capabilities = frozenset(
        {
            OrganizationWriteCapability.READ_SCOPE,
            OrganizationWriteCapability.MOVE,
            OrganizationWriteCapability.RENAME,
            OrganizationWriteCapability.RECYCLE,
            OrganizationWriteCapability.POSTCONDITION,
        }
    )
    contract = P115OrganizationContract(
        verified=True,
        capabilities=capabilities,
        timeout_enforced=True,
        evidence=OrganizationContractEvidence(
            evidence_id="automation-scheduled-fixture",
            capabilities=capabilities,
            timeout_enforced=True,
        ),
    )

    app = _app(
        database,
        task_adapter=_ReadyTaskAdapter(),
        security=SecurityManager(
            web_password_hash=password_hash_value,
            script_token_hash=password_hash.hash("unused-script-token"),
        ),
        organization_write_enabled=True,
        organization_write_contract_verified=True,
    )
    app.state.organization_contract = contract
    app.state.organization_write_contract_verified = True
    async with app.router.lifespan_context(app):
        assert app.state.p115_ready is True
        assert hasattr(app.state, "organization_worker")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://app.test"
        ) as client:
            headers = await _login(client, password)
            current = await client.get(
                "/api/v1/settings/organization", headers=headers
            )
            assert current.status_code == 200
            configured = await client.patch(
                "/api/v1/settings/organization",
                headers=headers,
                json={
                    "revision": current.json()["revision"],
                    "schedule_enabled": False,
                    "auto_execute_enabled": True,
                    "source_directory_ids": ["1000"],
                    "target_directory_id": "9000",
                    "rename_enabled": True,
                },
            )
            assert configured.status_code == 200
            assert configured.json()["auto_execute_enabled"] is True

            # Run the scheduled path directly (run-now is manual and would
            # never queue, by design).
            automation = app.state.organization_automation_service
            assert await automation.run_once() is True
            assert automation.last_result is not None
            assert automation.last_result.queued_count == 1, (
                automation.last_result.blocked_details
            )
            assert automation.last_result.plan_count == 1

            result = (
                await client.get(
                    "/api/v1/settings/organization/result", headers=headers
                )
            ).json()
            assert result["queued_count"] == 1, result

            plans = await client.get(
                "/api/v1/organization-plans",
                params={"status": "needs_review", "limit": 20},
                headers=headers,
            )
            assert plans.status_code == 200
            assert len(plans.json()["items"]) == 0

            async with database.session_factory() as session:
                operation = await session.scalar(
                    select(OrganizationOperation)
                )
                assert operation is not None
                assert operation.status == "planned"
                assert operation.idempotency_key.startswith("auto-")
    await app.state.database.engine.dispose()

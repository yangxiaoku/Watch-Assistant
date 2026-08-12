from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.adapters.p115_library import DirectoryPage, LibraryEntry, ScanState
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.security import SecurityManager

WEB_PASSWORD = "library-config-password"


class _FakeClient:
    async def aclose(self):
        return None


class _FakeCookieProvider:
    def load(self):
        return "unused"


class _FakeGateway:
    def __init__(self, *_args, scan_complete: bool | None = True, **_kwargs):
        self.scan_complete = scan_complete

    async def list_directory(self, _directory_id, *, page, page_size):
        assert page == 1
        assert page_size == 1
        return DirectoryPage(
            items=(),
            page=1,
            page_count=1,
            total=0,
            scan_complete=self.scan_complete,
            state=(
                ScanState.PARTIAL
                if self.scan_complete is False
                else ScanState.COMPLETE
            ),
            has_more=False,
            terminal=True,
        )


class _MultiPageGateway(_FakeGateway):
    def __init__(self, *_args, **_kwargs):
        super().__init__(*_args, **_kwargs)
        self.calls = []

    async def list_directory(self, directory_id, *, page, page_size):
        assert page_size == 1
        self.calls.append((directory_id, page, page_size))
        if page == 1:
            return DirectoryPage(
                items=(
                    LibraryEntry(
                        directory_id=None,
                        file_id="1001",
                        parent_id=directory_id,
                        name="scope-file-1.mkv",
                        is_directory=False,
                        size_bytes=0,
                        modified_at=None,
                        pickcode=None,
                    ),
                ),
                page=1,
                page_count=2,
                total=2,
                scan_complete=None,
                state=ScanState.COMPLETE,
                has_more=True,
                next_page=2,
                terminal=False,
            )
        return DirectoryPage(
            items=(
                LibraryEntry(
                    directory_id=None,
                    file_id="1002",
                    parent_id=directory_id,
                    name="scope-file-2.mkv",
                    is_directory=False,
                    size_bytes=0,
                    modified_at=None,
                    pickcode=None,
                ),
            ),
            page=2,
            page_count=2,
            total=2,
            scan_complete=True,
            state=ScanState.COMPLETE,
            has_more=False,
            next_page=None,
            terminal=True,
        )


async def _client(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'library-config.db'}")
    await initialize_database(database.engine)
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
    app.state.organization_target_root_id = "2988794667098701570"
    app.state.organization_cookie_provider = _FakeCookieProvider()
    return app, database


@pytest.mark.integration
async def test_library_configuration_is_scoped_optimistic_and_read_verified(tmp_path, monkeypatch):
    app, database = await _client(tmp_path)
    monkeypatch.setattr(
        "watch_assistant.api.library.P115ReadOnlyDirectoryGateway", _FakeGateway
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        assert (
            await client.put(
                "/api/v1/libraries/production/configuration",
                json={
                    "name": "Production",
                    "root_directory_id": "2988794667098701570",
                    "revision": 0,
                },
            )
        ).status_code == 401
        login = await client.post(
            "/api/v1/auth/login", json={"password": WEB_PASSWORD}
        )
        csrf = login.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}

        invalid_root = await client.put(
            "/api/v1/libraries/production/configuration",
            headers=headers,
            json={
                "name": "Production",
                "root_directory_id": "2988794667098701570-not-a-cid",
                "revision": 0,
            },
        )
        assert invalid_root.status_code == 422

        mismatch = await client.put(
            "/api/v1/libraries/production/configuration",
            headers=headers,
            json={
                "name": "Production",
                "root_directory_id": "3482085898508567892",
                "revision": 0,
            },
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["detail"] == "library_scope_mismatch"

        configured = await client.put(
            "/api/v1/libraries/production/configuration",
            headers=headers,
            json={
                "name": "Production",
                "root_directory_id": "2988794667098701570",
                "revision": 0,
            },
        )
        assert configured.status_code == 200
        assert configured.json()["scope_verified"] is False
        assert configured.json()["enabled"] is False
        assert configured.json()["revision"] == 1

        conflict = await client.put(
            "/api/v1/libraries/production/configuration",
            headers=headers,
            json={
                "name": "Production renamed",
                "root_directory_id": "2988794667098701570",
                "revision": 0,
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "library_configuration_conflict"

        verified = await client.post(
            "/api/v1/libraries/production/verify-scope", headers=headers
        )
        assert verified.status_code == 200
        assert verified.json()["verified"] is True
        assert verified.json()["enabled"] is True

    await database.engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize("scan_complete", [None, False])
async def test_library_scope_verification_requires_explicit_complete_scan(
    tmp_path, monkeypatch, scan_complete
):
    app, database = await _client(tmp_path)
    monkeypatch.setattr(
        "watch_assistant.api.library.P115ReadOnlyDirectoryGateway",
        lambda *args, **kwargs: _FakeGateway(
            *args, scan_complete=scan_complete, **kwargs
        ),
    )
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="production",
                name="Production",
                root_directory_id="2988794667098701570",
                scope_verified=False,
                enabled=False,
            )
        )
        await session.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        login = await client.post(
            "/api/v1/auth/login", json={"password": WEB_PASSWORD}
        )
        response = await client.post(
            "/api/v1/libraries/production/verify-scope",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "library_scope_verification_failed"

    async with database.session_factory() as session:
        library = await session.get(MediaLibrary, "production")
        assert library is not None
        assert library.scope_verified is False
        assert library.enabled is False
        assert library.revision == 0
    await database.engine.dispose()


@pytest.mark.integration
async def test_library_scope_verification_reads_all_pages_before_enabling(
    tmp_path, monkeypatch
):
    app, database = await _client(tmp_path)
    gateway = _MultiPageGateway()
    monkeypatch.setattr(
        "watch_assistant.api.library.P115ReadOnlyDirectoryGateway",
        lambda *args, **kwargs: gateway,
    )
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="production",
                name="Production",
                root_directory_id="2988794667098701570",
                scope_verified=False,
                enabled=False,
            )
        )
        await session.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        login = await client.post("/api/v1/auth/login", json={"password": WEB_PASSWORD})
        response = await client.post(
            "/api/v1/libraries/production/verify-scope",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
        )

    assert response.status_code == 200
    assert [call[1] for call in gateway.calls] == [1, 2]
    async with database.session_factory() as session:
        library = await session.get(MediaLibrary, "production")
        assert library is not None
        assert library.scope_verified is True
        assert library.enabled is True
    await database.engine.dispose()


@pytest.mark.integration
async def test_library_scan_operation_api_is_idempotent_bounded_and_cancellable(tmp_path):
    app, database = await _client(tmp_path)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-scan",
                name="Scan library",
                root_directory_id="7000",
                scope_verified=True,
                enabled=True,
            )
        )
        await session.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        login = await client.post(
            "/api/v1/auth/login", json={"password": WEB_PASSWORD}
        )
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        first = await client.post(
            "/api/v1/libraries/library-scan/scan",
            headers=headers,
            json={"idempotency_key": "api-scan-key", "max_directories": 3},
        )
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["state"] == "queued"
        assert first_body["state_message_zh"] == "等待扫描"
        assert first_body["error_code"] is None
        async with database.session_factory() as session:
            scan_run = await session.get(LibraryScanRun, first_body["run_id"])
        assert scan_run is not None
        assert scan_run.scan_mode == "tree"

        repeated = await client.post(
            "/api/v1/libraries/library-scan/scan",
            headers=headers,
            json={"idempotency_key": "api-scan-key", "max_directories": 9},
        )
        assert repeated.status_code == 200
        assert repeated.json()["run_id"] == first_body["run_id"]

        async with database.session_factory() as session:
            session.add_all(
                [
                    LibraryScanEntry(
                        scan_run_id=first_body["run_id"],
                        object_type="file",
                        object_id="1001",
                        parent_id="7000",
                        name="title-a.mkv",
                        path="title-a.mkv",
                        is_directory=False,
                        size_bytes=100,
                    ),
                    LibraryScanEntry(
                        scan_run_id=first_body["run_id"],
                        object_type="file",
                        object_id="1002",
                        parent_id="7000",
                        name="title-b.mkv",
                        path="title-b.mkv",
                        is_directory=False,
                        size_bytes=200,
                    ),
                ]
            )
            await session.commit()

        page = await client.get(
            f"/api/v1/libraries/library-scan/scans/{first_body['run_id']}/entries",
            params={"limit": 1},
        )
        assert page.status_code == 200
        assert len(page.json()["items"]) == 1
        assert page.json()["next_cursor"] == 1
        assert page.json()["items"][0]["object_id"] == "1001"

        cancelled = await client.post(
            f"/api/v1/libraries/library-scan/scans/{first_body['run_id']}/cancel",
            headers=headers,
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "cancelled"
        assert cancelled.json()["state_message_zh"] == "已取消"
        assert cancelled.json()["error_code"] == "cancelled"

        listing = await client.get(
            "/api/v1/libraries/library-scan/scans"
        )
        assert listing.status_code == 200
        assert listing.json()["items"][0]["run_id"] == first_body["run_id"]
        assert listing.json()["items"][0]["state"] == "cancelled"

    await database.engine.dispose()


@pytest.mark.integration
async def test_delete_route_requires_verified_write_and_delete_contracts(tmp_path):
    app, database = await _client(tmp_path)
    app.state.organization_write_enabled = True
    app.state.permanent_delete_enabled = True
    app.state.organization_write_contract_verified = False
    app.state.permanent_delete_contract_verified = False
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        login = await client.post(
            "/api/v1/auth/login", json={"password": WEB_PASSWORD}
        )
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        response = await client.post(
            "/api/v1/libraries/production/objects/100/delete",
            headers=headers,
            json={"expected_name": "fixture.wav", "confirm": True},
        )
    assert response.status_code == 503
    assert response.json()["detail"] == "organization_write_unverified"
    await database.engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("state_overrides", "expected_detail"),
    [
        (
            {
                "organization_write_enabled": False,
                "organization_write_contract_verified": True,
                "permanent_delete_enabled": True,
                "permanent_delete_contract_verified": True,
            },
            "organization_write_disabled",
        ),
        (
            {
                "organization_write_enabled": True,
                "organization_write_contract_verified": False,
                "permanent_delete_enabled": True,
                "permanent_delete_contract_verified": True,
            },
            "organization_write_unverified",
        ),
        (
            {
                "organization_write_enabled": True,
                "organization_write_contract_verified": True,
                "permanent_delete_enabled": False,
                "permanent_delete_contract_verified": True,
            },
            "permanent_delete_disabled",
        ),
        (
            {
                "organization_write_enabled": True,
                "organization_write_contract_verified": True,
                "permanent_delete_enabled": True,
                "permanent_delete_contract_verified": False,
            },
            "permanent_delete_unverified",
        ),
    ],
)
async def test_delete_route_fails_closed_on_each_gate(
    tmp_path, state_overrides, expected_detail
):
    """永久删除端点 4 道开关任一未满足都必须 503 + 对应 detail。"""
    app, database = await _client(tmp_path)
    for key, value in state_overrides.items():
        setattr(app.state, key, value)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        login = await client.post(
            "/api/v1/auth/login", json={"password": WEB_PASSWORD}
        )
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        response = await client.post(
            "/api/v1/libraries/production/objects/100/delete",
            headers=headers,
            json={"expected_name": "fixture.wav", "confirm": True},
        )
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == expected_detail
    await database.engine.dispose()

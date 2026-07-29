from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.security import SecurityManager


@pytest.mark.integration
async def test_subtitle_analysis_is_authenticated_and_does_not_read_content(tmp_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'subtitles.db'}")
    await initialize_database(database.engine)
    password_hash = PasswordHash.recommended()
    app = create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=type("Tmdb", (), {"aclose": lambda self: _noop()})(),
        pansou_client=type("PanSou", (), {"aclose": lambda self: _noop()})(),
        security_manager=SecurityManager(
            web_password_hash=password_hash.hash("subtitle-password"),
            script_token_hash=password_hash.hash("subtitle-token"),
            cookie_secure=False,
        ),
        frontend_dir=tmp_path / "missing",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        denied = await client.post(
            "/api/v1/subtitles/analyze",
            json={"video_name": "Show.mkv", "subtitle_names": ["Show.chs.srt"]},
        )
        assert denied.status_code == 401
        login = await client.post(
            "/api/v1/auth/login", json={"password": "subtitle-password"}
        )
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        response = await client.post(
            "/api/v1/subtitles/analyze",
            json={
                "video_name": "Show.S01E01.mkv",
                "subtitle_names": ["Show.S01E01.chs.srt"],
            },
            headers=headers,
        )
    assert login.status_code == 200
    assert response.status_code == 200
    assert response.json()["items"][0]["language"] == "zh-Hans"
    assert "Show.S01E01.mkv" not in response.json()["items"][0]["recommended_name"]
    await database.engine.dispose()


async def _noop():
    return None

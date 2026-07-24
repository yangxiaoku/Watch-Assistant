"""Serve the built frontend against an in-process Watch Assistant test backend."""

import argparse
import asyncio
import os
from pathlib import Path

import uvicorn
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.adapters.qbittorrent import (
    InspectionStatus,
    QbittorrentInspectionResult,
)
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.schemas import MediaType, MovieMetadata
from watch_assistant.security import SecurityManager

ROOT = Path(__file__).resolve().parents[1]
TEST_PASSWORD = "watch-assistant-live-test"
INFOHASH = "1234567890abcdef1234567890abcdef12345678"
MAGNET = f"magnet:?xt=urn:btih:{INFOHASH}&dn=Live+Test+Film+2026+1080p"


class TestTmdb:
    async def get_media(self, tmdb_id: int, media_type: MediaType) -> MovieMetadata:
        return MovieMetadata(
            tmdb_id=tmdb_id,
            media_type=media_type,
            title="Live Test Film",
            original_title="Live Test Film",
            release_year=2026,
            overview="Local frontend integration fixture.",
            poster_path=None,
            vote_average=8.0,
        )


class TestPanSou:
    async def search(self, _query: str) -> dict:
        return {
            "total": 1,
            "merged_by_type": {
                "magnet": [
                    {
                        "url": MAGNET,
                        "note": "Live Test Film 2026 1080p",
                        "source": "live-test",
                        "datetime": "2026-07-25T00:00:00Z",
                    }
                ]
            },
        }


class TestQbittorrent:
    async def ensure_available(self) -> None:
        return None

    async def inspect(self, _magnets: list[str]) -> list[QbittorrentInspectionResult]:
        return [
            QbittorrentInspectionResult(
                infohash=INFOHASH,
                status=InspectionStatus.VERIFIED,
                total_size_bytes=1073741824,
                file_count=2,
                video_file_count=1,
                video_size_bytes=1073741824,
                subtitle_count=1,
                sample_count=0,
                largest_video_name="live-test.mkv",
                content_summary="1 video(s), 1 subtitle(s), 0 suspicious file(s)",
            )
        ]

    async def aclose(self) -> None:
        return None


async def build_app():
    database = create_database("sqlite+aiosqlite:///:memory:")
    await initialize_database(database.engine)
    password_hash = PasswordHash.recommended()
    return create_app(
        database=database,
        crypto=SecretCrypto(Fernet.generate_key().decode("ascii")),
        tmdb_client=TestTmdb(),
        pansou_client=TestPanSou(),
        security_manager=SecurityManager(
            web_password_hash=password_hash.hash(TEST_PASSWORD),
            script_token_hash=password_hash.hash("live-test-script-token"),
        ),
        qbittorrent_client=TestQbittorrent(),
        push_supported=False,
        frontend_dir=ROOT / "frontend" / "dist",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4176)
    args = parser.parse_args()
    os.environ.setdefault("PYTHONPATH", str(ROOT / "src"))
    app = asyncio.run(build_app())
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()

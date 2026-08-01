"""Serve the built frontend against an in-process Watch Assistant test backend."""

import argparse
import asyncio
import os
import tempfile
from pathlib import Path

import uvicorn
from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from watch_assistant.adapters.p115_library import DirectoryPage, LibraryEntry, ScanState
from watch_assistant.adapters.qbittorrent import (
    InspectionStatus,
    QbittorrentInspectionResult,
)
from watch_assistant.api import library as library_api
from watch_assistant.app import create_app
from watch_assistant.crypto import SecretCrypto
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import MediaLibrary
from watch_assistant.schemas import MediaType, MovieMetadata
from watch_assistant.security import SecurityManager

ROOT = Path(__file__).resolve().parents[1]
TEST_PASSWORD = "watch-assistant-live-test"
INFOHASH = "1234567890abcdef1234567890abcdef12345678"
MAGNET = f"magnet:?xt=urn:btih:{INFOHASH}&dn=Live+Test+Film+2026+1080p"


class TestTmdb:
    async def get_feed(
        self, feed: str, media_type: MediaType = MediaType.MOVIE
    ) -> list[MovieMetadata]:
        media_label = "电视剧" if media_type == MediaType.TV else "电影"
        feed_labels = {
            "popular": "热门推荐",
            "now_playing": "正在热映",
            "upcoming": "即将上映",
            "top_rated": "高分佳作",
            "on_the_air": "热播剧集",
        }
        label = feed_labels.get(feed, feed)
        base_id = 1000 if media_type == MediaType.MOVIE else 2000
        return [
            MovieMetadata(
                tmdb_id=base_id + index,
                media_type=media_type,
                title=f"本地验收 {label} {index}",
                original_title=f"Live Test {media_label} {feed} {index}",
                release_year=2026,
                overview="本地前端验收数据，不访问外部服务。",
                poster_path=None,
                backdrop_path=None,
                vote_average=8.0 - index / 10,
            )
            for index in range(1, 4)
        ]

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


class TestLibraryGateway:
    """Deterministic read-only tree used by the real STRM live test."""

    def __init__(self, *_args, **_kwargs):
        pass

    async def list_directory(self, directory_id: str, *, page: int, page_size: int):
        assert page == 1
        assert page_size == 1
        if directory_id == "1000":
            items = (
                LibraryEntry(
                    directory_id="2000",
                    file_id=None,
                    parent_id="1000",
                    name="Shows",
                    is_directory=True,
                    size_bytes=None,
                    modified_at=None,
                    pickcode=None,
                    path="Shows",
                ),
            )
        elif directory_id == "2000":
            items = (
                LibraryEntry(
                    directory_id=None,
                    file_id="3000",
                    parent_id="2000",
                    name="Episode.mkv",
                    is_directory=False,
                    size_bytes=10,
                    modified_at=None,
                    pickcode=None,
                ),
            )
        else:
            items = ()
        return DirectoryPage(
            items=items,
            page=1,
            page_count=1,
            total=len(items),
            scan_complete=True,
            state=ScanState.COMPLETE,
            has_more=False,
            terminal=True,
        )


async def build_app(database_path: Path):
    database = create_database(f"sqlite+aiosqlite:///{database_path}")
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="live-library",
                name="本地 STRM 验收媒体库",
                root_directory_id="1000",
                scope_verified=True,
                enabled=True,
                revision=1,
            )
        )
        await session.commit()
    # The live server must exercise the same API gateway construction as the
    # product, while keeping the fixture entirely local and read-only.
    library_api.P115ReadOnlyDirectoryGateway = TestLibraryGateway
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
        organization_plan_enabled=True,
        organization_execution_enabled=False,
        strm_full_enabled=True,
        strm_incremental_enabled=True,
        strm_cleanup_enabled=True,
        strm_output_root=database_path.parent / "strm-output",
        frontend_dir=ROOT / "frontend" / "dist",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4176)
    args = parser.parse_args()
    os.environ.setdefault("PYTHONPATH", str(ROOT / "src"))
    with tempfile.TemporaryDirectory(prefix="watch-assistant-live-") as temp_dir:
        app = asyncio.run(build_app(Path(temp_dir) / "live.db"))
        uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()

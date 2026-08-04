import pytest

from watch_assistant.adapters.p115_playback_contract import (
    PlaybackGate,
    PlaybackStatus,
    make_playback_request,
)
from watch_assistant.adapters.p115_playback_gateway import P115LivePlaybackGateway
from watch_assistant.adapters.p115_playback_transport import PlaybackLink
from watch_assistant.db import create_database, initialize_database
from watch_assistant.library_models import (
    MediaLibrary,
    StrmManifestEntry,
    StrmManifestStatus,
)

MANIFEST_ID = "strm_7qXh5Mptv9K2dR4a"
CLOUD_FILE_ID = "100"
PROTECTED_PICKCODE = "protected-pickcode-value"
ENABLED_GATE = PlaybackGate(enabled=True, contract_verified=True)


class _CredentialSource:
    def load(self):
        return "SYNTHETIC_CREDENTIAL"


class _Transport:
    def __init__(self):
        self.calls = []

    async def download_url(self, pickcode, *, timeout_seconds):
        self.calls.append((pickcode, timeout_seconds))
        return PlaybackLink("https://cdn.example.invalid/temporary")


async def _database(tmp_path, *, pickcode=PROTECTED_PICKCODE):
    database = create_database(
        f"sqlite+aiosqlite:///{tmp_path / 'playback-gateway.db'}"
    )
    await initialize_database(database.engine)
    async with database.session_factory() as session:
        session.add(
            MediaLibrary(
                id="library-one",
                name="offline playback test library",
                root_directory_id="root-one",
                scope_verified=True,
                enabled=True,
                revision=1,
            )
        )
        session.add(
            StrmManifestEntry(
                manifest_id=MANIFEST_ID,
                library_id="library-one",
                cloud_file_id=CLOUD_FILE_ID,
                cloud_directory_id="root-one",
                pickcode=pickcode,
                cloud_relative_path="Episode.mkv",
                local_relative_path="Episode.strm",
                size_bytes=10,
                source_version=1,
                status=StrmManifestStatus.VERIFIED,
                is_current=True,
            )
        )
        await session.commit()
    return database


@pytest.mark.asyncio
async def test_live_gateway_passes_manifest_pickcode_not_cloud_file_id(tmp_path):
    database = await _database(tmp_path)
    transport = _Transport()
    try:
        gateway = P115LivePlaybackGateway(
            database.session_factory,
            _CredentialSource(),
            lambda _credential: transport,
        )
        outcome = await gateway.resolve(
            make_playback_request(MANIFEST_ID, "GET"),
            gate=ENABLED_GATE,
            allowed_library_ids={"library-one"},
        )

        assert outcome.status is PlaybackStatus.READY
        assert transport.calls
        assert transport.calls[0][0] == PROTECTED_PICKCODE
        assert transport.calls[0][0] != CLOUD_FILE_ID
        assert PROTECTED_PICKCODE not in repr(outcome)
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("pickcode", (None, ""))
async def test_live_gateway_rejects_missing_or_empty_pickcode_before_transport(
    tmp_path, pickcode
):
    database = await _database(tmp_path, pickcode=pickcode)
    transport = _Transport()
    try:
        gateway = P115LivePlaybackGateway(
            database.session_factory,
            _CredentialSource(),
            lambda _credential: transport,
        )
        outcome = await gateway.resolve(
            make_playback_request(MANIFEST_ID, "GET"), gate=ENABLED_GATE
        )

        assert (outcome.status, outcome.error_code) == (
            PlaybackStatus.NOT_FOUND,
            "manifest_out_of_scope",
        )
        assert transport.calls == []
    finally:
        await database.engine.dispose()

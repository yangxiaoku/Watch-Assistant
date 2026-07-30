import asyncio

import pytest

from watch_assistant.schemas import OrganizationSettingsResponse
from watch_assistant.services.organization_scheduler import OrganizationScheduler


class SettingsStub:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    async def get_organization(self):
        return OrganizationSettingsResponse(
            schedule_enabled=self.enabled,
            scan_interval_minutes=5,
            source_directory_ids=[],
            target_directory_id=None,
            video_extensions=["mkv"],
            metadata_extensions=["srt"],
            rename_enabled=True,
            media_probe_enabled=True,
            ai_identification_enabled=False,
            small_file_threshold_mb=0,
            cleanup_empty_directories=False,
            strm_linkage_enabled=False,
            operation_delay_seconds=1.5,
            include_children_category=False,
            include_concert_category=False,
            region_grouping_enabled=True,
            year_grouping_enabled=False,
            prefer_remux=True,
            prefer_resolution=True,
            prefer_dolby=False,
            conflict_mode=2,
            multi_version_enabled=False,
            revision=0,
        )


@pytest.mark.asyncio
async def test_manual_run_wakes_scheduler_when_schedule_is_disabled():
    settings = SettingsStub(False)
    calls = 0

    async def run_once():
        nonlocal calls
        calls += 1
        return True

    scheduler = OrganizationScheduler(settings, run_once)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run_forever(stop))
    await scheduler.request_run_now()
    await asyncio.wait_for(_wait_for(lambda: calls == 1), timeout=1)
    stop.set()
    scheduler.stop_pending()
    await asyncio.wait_for(task, timeout=1)


async def _wait_for(predicate):
    while not predicate():
        await asyncio.sleep(0.01)

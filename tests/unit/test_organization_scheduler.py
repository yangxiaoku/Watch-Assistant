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
            auto_execute_enabled=False,
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
            auto_cleanup_junk_files=False,
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


@pytest.mark.asyncio
async def test_manual_run_passes_a_stable_run_id_to_the_callback():
    settings = SettingsStub(False)
    received: list[str] = []

    async def run_once():
        return True

    async def manual_run(run_id: str):
        received.append(run_id)
        return True

    scheduler = OrganizationScheduler(settings, run_once, manual_run)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run_forever(stop))
    requested = await scheduler.request_run_now()
    await asyncio.wait_for(_wait_for(lambda: received == [requested]), timeout=1)
    assert requested.startswith("org_")
    stop.set()
    scheduler.stop_pending()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_enabled_schedule_waits_for_the_first_interval():
    settings = SettingsStub(True)
    calls = 0

    async def run_once():
        nonlocal calls
        calls += 1
        return True

    scheduler = OrganizationScheduler(settings, run_once)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run_forever(stop))
    await asyncio.sleep(0.05)
    assert calls == 0
    stop.set()
    scheduler.stop_pending()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_manual_run_is_not_lost_while_settings_are_loading():
    settings = BlockingSettingsStub(False)
    calls = 0

    async def run_once():
        nonlocal calls
        calls += 1
        return True

    scheduler = OrganizationScheduler(settings, run_once)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run_forever(stop))
    await asyncio.wait_for(settings.started.wait(), timeout=1)
    await scheduler.request_run_now()
    settings.release.set()
    await asyncio.wait_for(_wait_for(lambda: calls == 1), timeout=1)
    stop.set()
    scheduler.stop_pending()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_scheduler_only_invokes_the_planning_callback():
    settings = SettingsStub(False)
    planning_calls: list[str] = []

    async def plan_once():
        planning_calls.append("plan")
        return True

    scheduler = OrganizationScheduler(settings, plan_once)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run_forever(stop))
    await scheduler.request_run_now()
    await asyncio.wait_for(_wait_for(lambda: planning_calls == ["plan"]), timeout=1)
    stop.set()
    scheduler.stop_pending()
    await asyncio.wait_for(task, timeout=1)


class BlockingSettingsStub(SettingsStub):
    def __init__(self, enabled: bool):
        super().__init__(enabled)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_organization(self):
        self.started.set()
        await self.release.wait()
        return await super().get_organization()


async def _wait_for(predicate):
    while not predicate():
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_manual_run_queue_is_capped():
    """L14:手动整理请求队列必须有界,防止长跑实例无界堆积。"""
    settings = SettingsStub(False)
    scheduler = OrganizationScheduler(settings, run_once=None, manual_run=None)
    for _ in range(60):
        await scheduler.request_run_now()
    assert len(scheduler._manual_runs) <= scheduler._MANUAL_RUNS_MAX
    assert scheduler._manual_runs.maxlen is None  # 手动设上限,deque 本身无 maxlen


@pytest.mark.asyncio
async def test_run_forever_survives_run_once_exception():
    """L14:run_once 抛异常时调度循环不得死亡(异常隔离 + 退避)。"""
    calls = {"count": 0}

    async def explode_once():
        calls["count"] += 1
        raise RuntimeError("transient")

    scheduler = OrganizationScheduler(SettingsStub(False), explode_once)
    stop = asyncio.Event()
    # schedule 禁用时 idle 等 _wake,必须先 request_run_now 触发 run_once。
    await scheduler.request_run_now()

    async def _stop_later():
        await asyncio.sleep(0.05)
        stop.set()

    task = asyncio.create_task(scheduler.run_forever(stop))
    await asyncio.gather(_stop_later(), task)
    assert task.exception() is None, "异常不得逃逸出 run_forever"
    assert calls["count"] >= 1

"""单元测试：订阅整季完整性评估服务 (Task C1)。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from watch_assistant.services.media_parser import parse_media_filename
from watch_assistant.services.subscription_completeness import (
    evaluate_subscription_completeness,
    inventory_covers_all_episodes,
    resources_cover_all_episodes,
)


def make_season(episode_count: int, season_number: int = 1) -> SimpleNamespace:
    """构造 season_detail 形状的最小对象（与 SeasonDetailResponse 同字段）。"""
    return SimpleNamespace(
        season_number=season_number,
        episode_count=episode_count,
        episodes=[],
    )


def make_identity(*, name: str, object_id: str = "obj-1", season=None,
                  episode_start=None, episode_end=None) -> SimpleNamespace:
    """构造 InventoryIdentity 形状的最小对象。

    ``parsed`` 采用真实 parse_media_filename 结果，还原生产语义
    (InventoryIdentity.parsed 本就是从文件名解析而来)。显式传入的
    season/episode_start/episode_end 覆盖在 file 字段，与 library_inventory.
    build_identity 的优先级 (file 优先于 parsed) 一致。
    """
    parsed = parse_media_filename(name)
    season = parsed.season if season is None else season
    episode_start = parsed.episode_start if episode_start is None else episode_start
    episode_end = parsed.episode_end if episode_end is None else episode_end
    return SimpleNamespace(
        file=SimpleNamespace(
            object_id=object_id,
            name=name,
            season=season,
            episode_start=episode_start,
            episode_end=episode_end,
        ),
        parsed=parsed,
    )


# ---------------------------------------------------------------------------
# resources_cover_all_episodes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "names, episode_count, expected",
    [
        (["Show S01 1080p COMPLETE"], 10, True),  # 计划必测项：整季包
        (["Show S01E01"], 10, False),  # 单集不足
        (["Show S01E01-E05", "Show S01E06-E10"], 10, True),  # 分集覆盖全部
        (["Other S02"], 10, False),  # 其他季名称不纳入
        (["Show S01E01-E05", "Show S01E06-E10"], 0, False),  # 无 episode_count
    ],
)
def test_resources_cover_all_episodes(names, episode_count, expected):
    season = make_season(episode_count)
    assert resources_cover_all_episodes(names, season) is expected


def test_resources_cover_all_episodes_different_season_pack_not_included():
    # 整季包位于其他季 (S02)，不应纳入本季 (S01) 判定。
    names = ["Show S02 1080p COMPLETE"]
    season = make_season(episode_count=10, season_number=1)
    assert resources_cover_all_episodes(names, season) is False


def test_resources_special_not_cover_full_season():
    # 特辑 (special) 不应被判为整季包覆盖全季。
    names = ["Show S01 SP01"]
    season = make_season(episode_count=10)
    assert resources_cover_all_episodes(names, season) is False


def test_resources_range_end_below_start_clamps_to_single_episode():
    # end<start 的范围：_range_numbers 用 max 夹取，退化为单集主张 start。
    from watch_assistant.services.subscription_completeness import _range_numbers

    # "Show S01E10-E05" 解析为 ep_start=10、ep_end=None (parser 已丢弃
    # 不合法范围)，_range_numbers(10, None) → (10,)。
    assert _range_numbers(10, 5) == (10,)
    # 单集主张无法覆盖全部已播出集。
    season = make_season(episode_count=10)
    assert resources_cover_all_episodes(["Show S01E10-E05"], season) is False


# ---------------------------------------------------------------------------
# inventory_covers_all_episodes
# ---------------------------------------------------------------------------

def test_inventory_full_season_pack_covers():
    season = make_season(episode_count=10)
    identities = [make_identity(name="Show S01 1080p COMPLETE")]
    assert inventory_covers_all_episodes(identities, season) is True


def test_inventory_single_episode_not_cover():
    season = make_season(episode_count=10)
    identities = [make_identity(name="Show S01E01")]
    assert inventory_covers_all_episodes(identities, season) is False


def test_inventory_episode_range_covers_all():
    season = make_season(episode_count=10)
    identities = [
        make_identity(name="Show S01E01-E05", object_id="a"),
        make_identity(name="Show S01E06-E10", object_id="b"),
    ]
    assert inventory_covers_all_episodes(identities, season) is True


def test_inventory_wrong_season_skipped():
    season = make_season(episode_count=10, season_number=1)
    identities = [make_identity(name="Other S02 series 1080p COMPLETE")]
    assert inventory_covers_all_episodes(identities, season) is False


def test_inventory_special_not_cover_full_season():
    # 特辑 identity 不应被判为整季包覆盖全季。
    season = make_season(episode_count=10)
    identities = [make_identity(name="Show S01 SP01", object_id="sp1")]
    assert inventory_covers_all_episodes(identities, season) is False


# ---------------------------------------------------------------------------
# evaluate_subscription_completeness
# ---------------------------------------------------------------------------

def _make_search_result(*names: str) -> SimpleNamespace:
    return SimpleNamespace(results=[SimpleNamespace(name=name) for name in names])


@pytest.mark.asyncio
async def test_evaluate_complete_when_resources_full_and_inventory_empty():
    season = make_season(episode_count=10)
    search_result = _make_search_result("Show S01 1080p COMPLETE")
    assert await evaluate_subscription_completeness(
        session_factory=None,
        search_result=search_result,
        season_detail=season,
        inventory_files=[],
    ) is True


@pytest.mark.asyncio
async def test_evaluate_complete_when_inventory_full_and_resources_incomplete():
    season = make_season(episode_count=10)
    search_result = _make_search_result("Show S01E01")  # 单集，不齐
    identities = [
        make_identity(name="Show S01E01-E05", object_id="a"),
        make_identity(name="Show S01E06-E10", object_id="b"),
    ]
    assert await evaluate_subscription_completeness(
        session_factory=None,
        search_result=search_result,
        season_detail=season,
        inventory_files=identities,
    ) is True


@pytest.mark.asyncio
async def test_evaluate_false_when_nothing_covers():
    season = make_season(episode_count=10)
    search_result = _make_search_result("Show S01E01")
    identities = [make_identity(name="Show S01E02", object_id="a")]
    assert await evaluate_subscription_completeness(
        session_factory=None,
        search_result=search_result,
        season_detail=season,
        inventory_files=identities,
    ) is False


@pytest.mark.asyncio
async def test_evaluate_false_when_no_season_detail():
    search_result = _make_search_result("Show S01 1080p COMPLETE")
    assert await evaluate_subscription_completeness(
        session_factory=None,
        search_result=search_result,
        season_detail=None,
        inventory_files=[],
    ) is False

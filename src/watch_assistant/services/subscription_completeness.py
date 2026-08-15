"""整季完整性评估服务 (订阅智能暂停 · Task C1)。

本模块评估一条订阅在其季度资料下的 "已播出集是否被全部覆盖"，用于决定
订阅是否应被自动暂停 (PAUSED)。采用保守性原则：

- 宁可漏判，不肯误杀：TMDB 季集信息 (season_detail) 不可用或 episode_count
  不大于 0 时，一律返回 False (不做自动暂停, fail-safe)。
- 任一来源 (搜索资源名 / 本地库存) 覆盖全部已播出集即返回 True，任一满足
  即判定完整并触发自动暂停。
- 自动暂停是可逆操作 (状态 PAUSED，可随时人工恢复)，因此整季包资源
  (纯季名、无集号) 视为覆盖全集集包的依据是安全的。

设计口径：以 TMDB 当前季集数 (episode_count) 作为 baseline 上限，并假定
1..episode_count 全部为已播出 (aired=True)。理由：TMDB 返回的季集清单通常
代表已知存在的剧集；将 aired 全置 True 已足够保守地服务于"是否完整已播出"
的暂停判定——只要某集被声明存在，本地/搜索未覆盖即不算完整。
实现复用 episode_completeness 矩阵的判定：missing_episodes 为空且
conclusion_available 为真即为齐全。
"""

from __future__ import annotations

from watch_assistant.services.episode_completeness import (
    EpisodeBaseline,
    EpisodeFileReference,
    build_episode_matrix,
)
from watch_assistant.services.media_parser import parse_media_filename


def resources_cover_all_episodes(resource_names: list[str], season_detail) -> bool:
    """判断搜索资源名集合是否覆盖 season_detail 下全部已播出集。

    仅当 parsed.season == season_detail.season_number 时纳入该资源：

    - episode_start 非空：覆盖 episode_start..(episode_end or episode_start)。
    - episode_start 为空 (纯整季名，如 "Show S01 1080p COMPLETE")：视为
      整季包，覆盖 1..episode_count。
    """
    references = tuple(
        _resource_reference(index, name, season_detail)
        for index, name in enumerate(resource_names)
    )
    return _references_cover_all(references, season_detail)


def inventory_covers_all_episodes(
    inventory_files: list, season_detail
) -> bool:
    """判断本地库存 (InventoryIdentity 列表) 是否覆盖全部已播出集。

    对每个 InventoryIdentity：

    - episode_start 取 file.episode_start or parsed.episode_start；
      episode_end 取 file.episode_end or parsed.episode_end。
    - episode_start 非空：覆盖 episode_start..(episode_end or episode_start)。
    - episode_start 与 episode_end 皆空且
      parsed.season == season_detail.season_number：视为整季包，覆盖
      1..episode_count。
    - parsed.season 与 season_detail.season_number 不符且无集号信息：跳过。
    """
    references = tuple(
        _inventory_reference(identity, season_detail) for identity in inventory_files
    )
    return _references_cover_all(references, season_detail)


async def evaluate_subscription_completeness(
    *, session_factory, search_result, season_detail, inventory_files
) -> bool:
    """编排完整性评估：任一来源覆盖全部已播出集即返回 True。

    判定顺序：

    1. 先以搜索资源名判断；若已覆盖全部，直接返回 True。
    2. 否则以本地库存判断；若覆盖全部，返回 True。
    3. 两者皆不满足，返回 False。

    ``session_factory`` 为编排保留参数 (供未来/调用方扩展 DB 上下文)，
    本函数当前不做任何 DB 查询——库存加载由 Task C2 的调用方完成。
    ``season_detail`` / ``search_result`` 为 None 或不可用时返回 False
    (fail-safe，不做自动暂停)。
    """
    if season_detail is None or search_result is None:
        return False
    result_names = [
        item.name for item in getattr(search_result, "results", ()) if item is not None
    ]
    if resources_cover_all_episodes(result_names, season_detail):
        return True
    return inventory_covers_all_episodes(inventory_files or [], season_detail)


def _build_baseline(season_detail) -> tuple[EpisodeBaseline, ...]:
    episode_count = getattr(season_detail, "episode_count", None)
    if not isinstance(episode_count, int) or episode_count < 1:
        return ()
    # 设计口径：以 TMDB 当前季集数作 baseline；aired 全 True（见模块 docstring）。
    return tuple(EpisodeBaseline(n, aired=True) for n in range(1, episode_count + 1))


def _references_cover_all(
    references: tuple[EpisodeFileReference, ...], season_detail
) -> bool:
    baseline = _build_baseline(season_detail)
    if not baseline:
        return False
    matrix = build_episode_matrix(baseline, references, inventory_complete=True)
    return bool(matrix.conclusion_available) and not matrix.missing_episodes


def _resource_reference(
    index: int, name: str, season_detail
) -> EpisodeFileReference:
    """把单个资源名转成 EpisodeFileReference。

    file_id 用稳定字符串 f"resource:{index}"，保证非空且不重复。
    special=bool(parsed.special_hints)：特辑 (如 "Show S01 SP01" /
    "Show S01 SPECIAL") 解析为 season=1, episode_start=None，若不标记
    special 会被误判为整季包覆盖全季；由 build_episode_matrix 将其排除
    出覆盖范围，符合保守原则。
    """
    parsed = parse_media_filename(name)
    if parsed.season != getattr(season_detail, "season_number", None):
        return EpisodeFileReference(f"resource:{index}", (), recognized=False)
    return EpisodeFileReference(
        f"resource:{index}",
        _episode_numbers(parsed, season_detail),
        special=bool(parsed.special_hints),
    )


def _inventory_reference(identity, season_detail) -> EpisodeFileReference:
    """把单个 InventoryIdentity 转成 EpisodeFileReference。

    episode_start / episode_end 优先取 file 字段，其次取 parsed 字段；
    file_id 用 file.object_id。special=bool(parsed.special_hints)：
    库存特辑与搜索资源同理 (file 字段无 special 字段，取 parsed 的
    special_hints)，避免被误判为整季包。
    """
    file = identity.file
    parsed = identity.parsed
    episode_start = file.episode_start if file.episode_start is not None else parsed.episode_start
    episode_end = file.episode_end if file.episode_end is not None else parsed.episode_end
    season_number = getattr(season_detail, "season_number", None)
    if episode_start is None and parsed.season != season_number:
        # 无集号信息且季不符：跳过。
        return EpisodeFileReference(
            file.object_id, (), special=bool(parsed.special_hints), recognized=False
        )
    numbers = _range_numbers(episode_start, episode_end)
    if numbers is None and parsed.season == season_number:
        # 整季包 (两者皆空且季相符)：1..episode_count。
        episode_count = getattr(season_detail, "episode_count", None)
        if isinstance(episode_count, int) and episode_count >= 1:
            numbers = tuple(range(1, episode_count + 1))
    return EpisodeFileReference(
        file.object_id,
        numbers or (),
        special=bool(parsed.special_hints),
        recognized=bool(numbers),
    )


def _episode_numbers(parsed, season_detail) -> tuple[int, ...]:
    """资源名的集号范围；episode_start 为空 (纯整季名) 视为整季包。"""
    if parsed.episode_start is not None:
        # episode_start 非空时 _range_numbers 恒返回非空 tuple。
        return _range_numbers(parsed.episode_start, parsed.episode_end)
    episode_count = getattr(season_detail, "episode_count", None)
    if isinstance(episode_count, int) and episode_count >= 1:
        return tuple(range(1, episode_count + 1))
    return ()


def _range_numbers(start: int | None, end: int | None) -> tuple[int, ...] | None:
    """返回 start..(end or start) 的闭合区间集号；start 为空返回 None。"""
    if start is None:
        return None
    stop = end if end is not None else start
    stop = max(stop, start)
    return tuple(range(start, stop + 1))

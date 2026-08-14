"""只读库存重复检测:从扫描快照条目计算「完全重复」与「同片多版本」两类报告。

零写操作:纯函数只读输入条目,不访问文件系统、不调用 115 写 API、不触碰
数据库。判定规则:
- 完全重复(exact_duplicate):同一 (name, size_bytes) 出现多次。115 只读
  网关不提供 content_hash,故用「同名 + 同大小」作为同一内容的保守近似;
  保留 object_id 字典序最小者,其余计入可回收字节。
- 同片多版本(multi_version):media_parser 解析文件名得到媒体身份
  (剧集 title+season+episode 或电影 title+year),同一身份出现多个不同
  分辨率(resolution)时记为洗版候选;本期不直接算可回收字节(它是「替换」
  而非「删除」)。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from watch_assistant.services.media_parser import (
    MediaParseResult,
    parse_media_filename,
)


@dataclass(frozen=True, slots=True)
class InventoryAuditEntry:
    """一条扫描快照中的文件条目(只读输入)。"""

    object_id: str
    name: str
    path: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class InventoryAuditItem:
    """报告中的单个文件项,含解析出的分辨率标签。"""

    object_id: str
    name: str
    path: str | None
    size_bytes: int | None
    resolution: str | None = None


@dataclass(frozen=True, slots=True)
class InventoryAuditGroup:
    group_id: str
    kind: Literal["exact_duplicate", "multi_version"]
    items: tuple[InventoryAuditItem, ...]
    reclaimable_bytes: int
    keep_object_id: str | None = None


@dataclass(frozen=True, slots=True)
class InventoryAuditReport:
    groups: tuple[InventoryAuditGroup, ...]
    duplicate_count: int
    multi_version_count: int
    reclaimable_bytes: int


def _hash(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _media_identity(parsed: MediaParseResult) -> tuple[object, ...] | None:
    """返回稳定的媒体身份键,无法可靠判定的返回 None。

    剧集: (kind, title, season, episode_start, episode_end)
    电影: (kind, title, year)
    """
    title = (parsed.title or "").strip().casefold()
    if not title:
        return None
    if parsed.season is not None and parsed.episode_start is not None:
        return ("tv", title, parsed.season, parsed.episode_start, parsed.episode_end)
    if parsed.media_type_hint == "tv" or parsed.season is not None:
        # 有季但无集起始,仍按剧集身份(季粒度)聚合,避免误判电影。
        return ("tv", title, parsed.season, None, None)
    return ("movie", title, parsed.year)


def build_audit_report(entries: Sequence[InventoryAuditEntry]) -> InventoryAuditReport:
    """从扫描快照条目计算库存重复检测报告(纯函数)。"""
    # 1) 完全重复:按 (name, size_bytes) 分组(仅 size 已知的文件)。
    by_signature: dict[tuple[str, int], list[InventoryAuditEntry]] = {}
    for entry in entries:
        if entry.size_bytes is not None:
            by_signature.setdefault((entry.name, entry.size_bytes), []).append(entry)

    duplicate_groups: list[InventoryAuditGroup] = []
    for (name, size), group in sorted(by_signature.items()):
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda item: item.object_id)
        keep = ordered[0]
        reclaimable = sum(item.size_bytes or 0 for item in ordered[1:])
        items = tuple(
            InventoryAuditItem(
                object_id=item.object_id,
                name=item.name,
                path=item.path,
                size_bytes=item.size_bytes,
                resolution=None,
            )
            for item in ordered
        )
        duplicate_groups.append(
            InventoryAuditGroup(
                group_id=f"duplicate:{_hash(name, size)}",
                kind="exact_duplicate",
                items=items,
                reclaimable_bytes=reclaimable,
                keep_object_id=keep.object_id,
            )
        )

    # 2) 同片多版本:按媒体身份分组,组内出现多个不同分辨率。
    by_identity: dict[
        tuple[object, ...], list[tuple[InventoryAuditEntry, MediaParseResult]]
    ] = {}
    for entry in entries:
        parsed = parse_media_filename(entry.name)
        identity = _media_identity(parsed)
        if identity is None:
            continue
        by_identity.setdefault(identity, []).append((entry, parsed))

    multi_groups: list[InventoryAuditGroup] = []
    for identity, group in sorted(by_identity.items(), key=lambda kv: str(kv[0])):
        if len(group) < 2:
            continue
        resolutions = {parsed.resolution for _, parsed in group}
        if len(resolutions) < 2:
            # 同身份但分辨率相同(或都解析不出)不算多版本。
            continue
        # 分辨率高的排前(字符串排序不够精确,但足够作为建议保留的稳定顺序)。
        ordered = sorted(
            group,
            key=lambda pair: (pair[1].resolution or "", pair[0].size_bytes or 0),
            reverse=True,
        )
        keep = ordered[0][0]
        items = tuple(
            InventoryAuditItem(
                object_id=entry.object_id,
                name=entry.name,
                path=entry.path,
                size_bytes=entry.size_bytes,
                resolution=parsed.resolution,
            )
            for entry, parsed in ordered
        )
        multi_groups.append(
            InventoryAuditGroup(
                group_id=f"multi:{_hash(*identity)}",
                kind="multi_version",
                items=items,
                reclaimable_bytes=0,
                keep_object_id=keep.object_id,
            )
        )

    return InventoryAuditReport(
        groups=tuple(duplicate_groups + multi_groups),
        duplicate_count=len(duplicate_groups),
        multi_version_count=len(multi_groups),
        reclaimable_bytes=sum(group.reclaimable_bytes for group in duplicate_groups),
    )


__all__ = [
    "InventoryAuditEntry",
    "InventoryAuditGroup",
    "InventoryAuditItem",
    "InventoryAuditReport",
    "build_audit_report",
]

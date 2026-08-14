"""库存重复检测判定纯函数单元测试。"""

from watch_assistant.services.inventory_audit import (
    InventoryAuditEntry,
    build_audit_report,
)


def _entry(object_id, name, size_bytes=None, path=None):
    return InventoryAuditEntry(object_id=object_id, name=name, path=path, size_bytes=size_bytes)


def test_exact_duplicate_by_name_and_size():
    entries = [
        _entry("a1", "某剧.S01E01.1080p.mkv", 1000),
        _entry("a2", "某剧.S01E01.1080p.mkv", 1000),
        _entry("a3", "另一部.mkv", 500),
    ]
    report = build_audit_report(entries)
    assert report.duplicate_count == 1
    assert report.multi_version_count == 0
    assert report.reclaimable_bytes == 1000
    group = report.groups[0]
    assert group.kind == "exact_duplicate"
    assert len(group.items) == 2
    assert group.keep_object_id == "a1"


def test_exact_duplicate_ignores_different_size():
    entries = [
        _entry("a1", "某剧.S01E01.mkv", 1000),
        _entry("a2", "某剧.S01E01.mkv", 2000),
    ]
    report = build_audit_report(entries)
    assert report.duplicate_count == 0


def test_multi_version_same_episode_different_resolution():
    entries = [
        _entry("b1", "某剧.S01E01.1080p.mkv", 3000),
        _entry("b2", "某剧.S01E01.2160p.mkv", 6000),
    ]
    report = build_audit_report(entries)
    assert report.duplicate_count == 0
    assert report.multi_version_count == 1
    group = report.groups[0]
    assert group.kind == "multi_version"
    assert group.reclaimable_bytes == 0
    # 分辨率高的建议保留(2160p 的 b2)
    assert group.keep_object_id == "b2"


def test_single_entry_no_groups():
    report = build_audit_report([_entry("a1", "某片.mkv", 1000)])
    assert report.duplicate_count == 0
    assert report.multi_version_count == 0
    assert report.groups == ()


def test_empty_entries():
    report = build_audit_report([])
    assert report.duplicate_count == 0
    assert report.multi_version_count == 0
    assert report.reclaimable_bytes == 0


def test_movie_multi_version_by_title_and_year():
    entries = [
        _entry("c1", "星际穿越.2014.1080p.mkv", 5000),
        _entry("c2", "星际穿越.2014.4K.mkv", 9000),
    ]
    report = build_audit_report(entries)
    assert report.multi_version_count == 1

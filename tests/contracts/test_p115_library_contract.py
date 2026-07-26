import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from watch_assistant.adapters.p115_library import (
    DirectoryPage,
    FakeP115LibraryGateway,
    LibraryContractError,
    P115LibraryGateway,
    ScanState,
    parse_directory_detail,
    parse_directory_page,
    parse_file_detail,
    run_read_only_probe,
    scan_directory,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "p115_library"


def _fixture(name: str) -> DirectoryPage:
    with (FIXTURES / name).open(encoding="utf-8") as fixture:
        return parse_directory_page(json.load(fixture))


def _unique_items(count: int):
    template = _fixture("page_1.json").items[0]
    return tuple(
        replace(template, file_id=str(1000 + index), name=f"synthetic-{index}")
        for index in range(count)
    )


@pytest.mark.asyncio
async def test_fake_gateway_scans_multiple_pages_without_exposing_call_values():
    first = _fixture("page_1.json")
    second = _fixture("page_2.json")
    gateway = FakeP115LibraryGateway({1: first, 2: second})

    assert first.scan_complete is None
    result = await scan_directory(gateway, "sensitive-directory-id", page_size=2)

    assert result.scan_complete is True
    assert result.state is ScanState.COMPLETE
    assert result.pages_read == 2
    assert [entry.file_id for entry in result.items] == ["101", None, "102"]
    assert gateway.calls == [
        type(gateway.calls[0])("list_directory", 1, 2),
        type(gateway.calls[0])("list_directory", 2, 2),
    ]
    assert "sensitive-directory-id" not in repr(gateway)
    assert "SYNTHETIC_PICK" not in repr(gateway.calls)


@pytest.mark.asyncio
async def test_repeated_empty_and_changed_pages_are_partial():
    first = _fixture("page_1.json")
    second = _fixture("page_2.json")

    repeated = FakeP115LibraryGateway({1: first, 2: replace(first, page=1)})
    repeated_result = await scan_directory(repeated, "directory")
    assert repeated_result.scan_complete is False
    assert repeated_result.error_code == "repeated_page"

    empty = FakeP115LibraryGateway({1: first, 2: replace(second, items=(), page=2)})
    empty_result = await scan_directory(empty, "directory")
    assert empty_result.scan_complete is False
    assert empty_result.error_code == "empty_page"

    changed = FakeP115LibraryGateway({1: first, 2: replace(second, page_count=3)})
    changed_result = await scan_directory(changed, "directory")
    assert changed_result.scan_complete is False
    assert changed_result.error_code == "page_count_changed"

    cancelled = FakeP115LibraryGateway(
        {1: replace(first, state=ScanState.CANCELLED, scan_complete=False)}
    )
    cancelled_result = await scan_directory(cancelled, "directory")
    assert cancelled_result.state is ScanState.CANCELLED
    assert cancelled_result.scan_complete is False


@pytest.mark.asyncio
async def test_missing_page_count_never_completes_after_first_page():
    page = replace(
        _fixture("page_1.json"),
        page_count=None,
        total=250,
        items=_unique_items(100),
    )
    result = await scan_directory(FakeP115LibraryGateway({1: page}), "directory")
    assert result.scan_complete is False
    assert result.error_code == "pagination_unverified"


@pytest.mark.asyncio
async def test_missing_all_termination_information_is_unverified():
    page = replace(_fixture("page_1.json"), page_count=None, total=None, items=())
    result = await scan_directory(FakeP115LibraryGateway({1: page}), "directory")
    assert result.scan_complete is False
    assert result.error_code == "pagination_unverified"


@pytest.mark.asyncio
async def test_total_change_and_terminal_total_mismatch_are_partial():
    first = replace(_fixture("page_1.json"), page_count=2, total=3)
    second = replace(_fixture("page_2.json"), total=4)
    changed = await scan_directory(
        FakeP115LibraryGateway({1: first, 2: second}), "directory"
    )
    assert changed.scan_complete is False
    assert changed.error_code == "total_changed"

    mismatch = replace(_fixture("page_1.json"), page_count=1, total=4)
    result = await scan_directory(FakeP115LibraryGateway({1: mismatch}), "directory")
    assert result.scan_complete is False
    assert result.error_code == "total_mismatch"


@pytest.mark.asyncio
async def test_explicit_terminal_is_a_valid_completion_boundary():
    with_total = replace(
        _fixture("page_1.json"), page_count=None, total=2, terminal=True
    )
    result = await scan_directory(FakeP115LibraryGateway({1: with_total}), "directory")
    assert result.scan_complete is True

    first = replace(
        _fixture("page_1.json"),
        page_count=None,
        total=3,
        has_more=True,
        next_page=2,
    )
    second = replace(
        _fixture("page_2.json"),
        page_count=None,
        total=3,
        has_more=False,
    )
    result = await scan_directory(
        FakeP115LibraryGateway({1: first, 2: second}), "directory"
    )
    assert result.scan_complete is True
    assert result.state is ScanState.COMPLETE

    without_total = replace(
        _fixture("page_1.json"), page_count=None, total=None, terminal=True
    )
    result = await scan_directory(
        FakeP115LibraryGateway({1: without_total}), "directory"
    )
    assert result.scan_complete is True


@pytest.mark.asyncio
async def test_gateway_exception_returns_partial_without_exception_details():
    gateway = FakeP115LibraryGateway(
        {1: _fixture("page_1.json"), 2: _fixture("page_2.json")}, fail_page=2
    )

    result = await scan_directory(gateway, "directory")

    assert result.state is ScanState.PARTIAL
    assert result.scan_complete is False
    assert result.error_code == "gateway_error"
    assert "request failed" not in repr(result)


@pytest.mark.asyncio
async def test_cancellation_propagates():
    class CancelledGateway:
        async def list_directory(
            self, directory_id: str, *, page: int = 1, page_size: int = 100
        ):
            del directory_id, page, page_size
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await scan_directory(CancelledGateway(), "directory")


def test_invalid_fields_are_rejected_without_raw_values():
    invalid_records = [
        {"fid": True, "file_name": "synthetic-name", "is_dir": False},
        {"fid": "1", "file_name": "synthetic-name", "is_dir": False, "size": True},
        {
            "fid": "1",
            "file_name": "synthetic-name",
            "is_dir": False,
            "user_utime": "bad",
        },
        {"fid": "1", "file_name": "synthetic-name", "is_dir": False, "pick_code": 12},
    ]
    for record in invalid_records:
        with pytest.raises(LibraryContractError) as error:
            parse_directory_page(
                {"data": {"list": [record], "page": 1, "page_count": 1}}
            )
        assert "synthetic-name" not in str(error.value)


def test_dto_repr_redacts_names_paths_and_pickcodes():
    page = _fixture("page_1.json")
    rendered = repr(page) + repr(page.items[0])
    assert "Synthetic Feature" not in rendered
    assert "SYNTHETIC_PICK" not in rendered
    assert "item_count=2" in rendered


@pytest.mark.asyncio
async def test_file_and_directory_detail_use_the_same_sanitized_dto():
    file_detail = parse_file_detail(
        {
            "fid": "201",
            "pid": "7000",
            "file_name": "Synthetic Detail.mkv",
            "is_dir": False,
            "size": 12,
            "pick_code": "SYNTHETIC_DETAIL_PICK",
        }
    )
    directory_detail = parse_directory_detail(
        {"cid": "7010", "pid": "7000", "file_name": "Synthetic Folder", "is_dir": True}
    )
    gateway = FakeP115LibraryGateway(
        {1: _fixture("page_1.json")},
        file_details={"201": file_detail},
        directory_details={"7010": directory_detail},
    )

    assert await gateway.get_file_detail("201") == file_detail
    assert await gateway.get_directory_detail("7010") == directory_detail
    assert gateway.calls == [
        type(gateway.calls[0])("get_file_detail"),
        type(gateway.calls[0])("get_directory_detail"),
    ]


@pytest.mark.asyncio
async def test_probe_is_explicitly_offline_only_and_protocol_has_no_writes():
    gateway = FakeP115LibraryGateway(
        {1: _fixture("page_1.json"), 2: _fixture("page_2.json")}
    )
    report = await run_read_only_probe(gateway, "directory")

    assert report.mode == "offline_only"
    assert report.scan.scan_complete is True
    assert set(P115LibraryGateway.__dict__) >= {
        "list_directory",
        "get_file_detail",
        "get_directory_detail",
    }
    assert not any(name.startswith("write") for name in P115LibraryGateway.__dict__)

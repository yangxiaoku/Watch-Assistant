from __future__ import annotations

from collections.abc import Mapping

import pytest

from watch_assistant.adapters.p115_library import (
    DirectoryPage,
    LibraryEntry,
    ScanState,
)
from watch_assistant.adapters.p115_library_gateway import P115ReadOnlyGatewayError
from watch_assistant.services.organization_target import (
    OrganizationTargetError,
    read_target_catalog,
)


def _entry(
    *,
    name: str,
    directory_id: str | None = None,
    file_id: str | None = None,
    parent_id: str = "100",
) -> LibraryEntry:
    return LibraryEntry(
        directory_id=directory_id,
        file_id=file_id,
        parent_id=parent_id,
        name=name,
        is_directory=directory_id is not None,
        size_bytes=None,
        modified_at=None,
        pickcode=None,
    )


class _DirectoryGateway:
    def __init__(self, pages: Mapping[tuple[str, int], DirectoryPage]):
        self.pages = dict(pages)
        self.calls: list[tuple[str, int, int]] = []

    async def list_directory(
        self, directory_id: str, *, page: int = 1, page_size: int = 100
    ) -> DirectoryPage:
        self.calls.append((directory_id, page, page_size))
        return self.pages[(directory_id, page)]


class _RetryingDirectoryGateway(_DirectoryGateway):
    def __init__(self, pages):
        super().__init__(pages)
        self.failures = 1

    async def list_directory(
        self, directory_id: str, *, page: int = 1, page_size: int = 100
    ) -> DirectoryPage:
        self.calls.append((directory_id, page, page_size))
        if self.failures:
            self.failures -= 1
            raise P115ReadOnlyGatewayError("fs_files_failed")
        return self.pages[(directory_id, page)]


def _page(
    *items: LibraryEntry,
    page: int = 1,
    total: int | None = None,
    next_page: int | None = None,
    terminal: bool = True,
    state: ScanState = ScanState.COMPLETE,
) -> DirectoryPage:
    return DirectoryPage(
        items=tuple(items),
        page=page,
        page_count=None,
        total=total if total is not None else len(items),
        scan_complete=(
            True
            if state is ScanState.COMPLETE and terminal
            else None
            if state is ScanState.COMPLETE
            else False
        ),
        state=state,
        has_more=next_page is not None,
        next_page=next_page,
        terminal=terminal,
    )


@pytest.mark.asyncio
async def test_reads_complete_recursive_catalog_and_ignores_files():
    gateway = _DirectoryGateway(
        {
            ("100", 1): _page(
                _entry(name="Movies", directory_id="200"),
                total=2,
                next_page=2,
                terminal=False,
            ),
            ("100", 2): _page(_entry(name="ignored.mkv", file_id="300")),
            ("200", 1): _page(_entry(name="4K", directory_id="400", parent_id="200")),
            ("400", 1): _page(),
        }
    )

    catalog = await read_target_catalog(gateway, "100")

    assert catalog.root_directory_id == "100"
    assert catalog.directories == (("", "100"), ("Movies", "200"), ("Movies/4K", "400"))
    assert catalog.directory_id_for("Movies/4K") == "400"
    assert catalog.directory_id_for("../outside") is None
    assert gateway.calls == [
        ("100", 1, 1),
        ("100", 2, 1),
        ("200", 1, 1),
        ("400", 1, 1),
    ]


@pytest.mark.asyncio
async def test_retries_transient_target_directory_read_once():
    gateway = _RetryingDirectoryGateway({("100", 1): _page()})

    catalog = await read_target_catalog(gateway, "100")

    assert catalog.root_directory_id == "100"
    assert gateway.calls == [("100", 1, 1), ("100", 1, 1)]


@pytest.mark.asyncio
async def test_exposes_target_read_failure_without_remote_details():
    class _AlwaysFailedGateway(_DirectoryGateway):
        async def list_directory(self, directory_id: str, *, page: int = 1, page_size: int = 100):
            self.calls.append((directory_id, page, page_size))
            raise P115ReadOnlyGatewayError("fs_files_failed")

    gateway = _AlwaysFailedGateway({})
    with pytest.raises(OrganizationTargetError) as error:
        await read_target_catalog(gateway, "100")

    assert error.value.code == "target_directory_read_failed"
    assert error.value.cause_code == "fs_files_failed"
    assert gateway.calls == [("100", 1, 1), ("100", 1, 1)]


@pytest.mark.asyncio
async def test_rejects_incomplete_directory_listing():
    gateway = _DirectoryGateway(
        {
            ("100", 1): _page(
                _entry(name="Movies", directory_id="200"),
                terminal=False,
                state=ScanState.PARTIAL,
            )
        }
    )

    with pytest.raises(OrganizationTargetError) as error:
        await read_target_catalog(gateway, "100")

    assert error.value.code == "target_directory_incomplete"


@pytest.mark.asyncio
async def test_rejects_path_and_identity_conflicts():
    same_path = _DirectoryGateway(
        {
            ("100", 1): _page(
                _entry(name="Movies", directory_id="200"),
                _entry(name="Movies", directory_id="201"),
            ),
        }
    )
    with pytest.raises(OrganizationTargetError) as path_error:
        await read_target_catalog(same_path, "100")
    assert path_error.value.code == "target_directory_path_conflict"

    same_identity = _DirectoryGateway(
        {
            ("100", 1): _page(
                _entry(name="Movies", directory_id="200"),
                _entry(name="Series", directory_id="200"),
            ),
        }
    )
    with pytest.raises(OrganizationTargetError) as identity_error:
        await read_target_catalog(same_identity, "100")
    assert identity_error.value.code == "target_directory_identity_conflict"


@pytest.mark.asyncio
async def test_rejects_noncontiguous_pagination():
    gateway = _DirectoryGateway(
        {
            ("100", 1): _page(
                _entry(name="Movies", directory_id="200"),
                total=2,
                next_page=3,
                terminal=False,
            )
        }
    )

    with pytest.raises(OrganizationTargetError) as error:
        await read_target_catalog(gateway, "100")

    assert error.value.code == "target_directory_pagination_unverified"


@pytest.mark.asyncio
async def test_rejects_invalid_file_limit():
    gateway = _DirectoryGateway({("100", 1): _page()})

    with pytest.raises(OrganizationTargetError) as error:
        await read_target_catalog(gateway, "100", max_files=0)

    assert error.value.code == "target_file_limit_invalid"
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_rejects_target_file_limit_before_collecting_more_files():
    gateway = _DirectoryGateway(
        {
            ("100", 1): _page(
                _entry(name="one.mkv", file_id="301"),
                _entry(name="two.mkv", file_id="302"),
            )
        }
    )

    with pytest.raises(OrganizationTargetError) as error:
        await read_target_catalog(gateway, "100", max_files=1)

    assert error.value.code == "target_file_limit_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "error_code"),
    (
        (_entry(name="missing-id"), "target_entry_identity_unverified"),
        (
            _entry(name="wrong-parent", file_id="301", parent_id="999"),
            "target_entry_scope_unverified",
        ),
        (
            LibraryEntry(
                directory_id=None,
                file_id=None,
                parent_id="100",
                name="missing-directory-id",
                is_directory=True,
                size_bytes=None,
                modified_at=None,
                pickcode=None,
            ),
            "target_entry_identity_unverified",
        ),
    ),
)
async def test_rejects_target_entries_without_identity_or_scope_evidence(
    entry, error_code
):
    gateway = _DirectoryGateway({("100", 1): _page(entry)})

    with pytest.raises(OrganizationTargetError) as error:
        await read_target_catalog(gateway, "100")

    assert error.value.code == error_code


@pytest.mark.asyncio
async def test_rejects_duplicate_file_identity_across_target_paths():
    gateway = _DirectoryGateway(
        {
            ("100", 1): _page(
                _entry(name="Movies", directory_id="200"),
                _entry(name="Series", directory_id="201"),
            ),
            ("200", 1): _page(
                _entry(name="movie.mkv", file_id="300", parent_id="200")
            ),
            ("201", 1): _page(
                _entry(name="same-file.mkv", file_id="300", parent_id="201")
            ),
        }
    )

    with pytest.raises(OrganizationTargetError) as error:
        await read_target_catalog(gateway, "100")

    assert error.value.code == "target_file_identity_conflict"

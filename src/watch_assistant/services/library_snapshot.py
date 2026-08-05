"""Fail-closed selection of one current, complete library snapshot."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from watch_assistant.library_models import (
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
)
from watch_assistant.services.library_index import (
    LibraryIndexError,
    ScanRunState,
    validate_complete_scan_evidence,
)
from watch_assistant.services.strm_scope import source_snapshot_is_current


async def verified_latest_scan(
    session: AsyncSession, library: MediaLibrary | None
) -> LibraryScanRun | None:
    """Return the only current complete tree snapshot for ``library``.

    A newer unsettled scan, duplicate current revision, scope mismatch, or
    incomplete checkpoint makes the result unavailable.  Callers must treat
    ``None`` as unknown rather than falling back to an older snapshot.
    """

    if library is None or not library.enabled or not library.scope_verified:
        return None
    run = await session.scalar(
        select(LibraryScanRun)
        .where(
            LibraryScanRun.library_id == library.id,
            LibraryScanRun.root_directory_id == library.root_directory_id,
            LibraryScanRun.state == ScanRunState.COMPLETED.value,
            LibraryScanRun.complete.is_(True),
            LibraryScanRun.snapshot_revision.is_not(None),
        )
        .order_by(
            LibraryScanRun.snapshot_revision.desc(),
            LibraryScanRun.updated_at.desc(),
            LibraryScanRun.id.desc(),
        )
        .limit(1)
    )
    if run is None:
        return None
    if not await scan_has_verified_evidence(session, run, library):
        return None
    return run


async def scan_has_verified_evidence(
    session: AsyncSession,
    run: LibraryScanRun | None,
    library: MediaLibrary | None,
    *,
    require_current: bool = True,
) -> bool:
    """Validate scope, freshness, revision uniqueness, and tree evidence."""

    if (
        run is None
        or library is None
        or not library.enabled
        or not library.scope_verified
        or run.library_id != library.id
        or run.root_directory_id != library.root_directory_id
        or run.state != ScanRunState.COMPLETED.value
        or not run.complete
        or run.snapshot_revision is None
    ):
        return False
    if require_current and not await source_snapshot_is_current(
        session,
        library_id=library.id,
        source_scan_run_id=run.id,
        source_snapshot_revision=run.snapshot_revision,
    ):
        return False
    checkpoint = await session.get(LibraryScanCheckpoint, run.id)
    entries = list(
        (
            await session.scalars(
                select(LibraryScanEntry).where(
                    LibraryScanEntry.scan_run_id == run.id
                )
            )
        ).all()
    )
    try:
        validate_complete_scan_evidence(
            run,
            checkpoint,
            entries,
            root_directory_id=library.root_directory_id,
            require_tree=True,
        )
    except LibraryIndexError:
        return False
    return True


__all__ = ["scan_has_verified_evidence", "verified_latest_scan"]

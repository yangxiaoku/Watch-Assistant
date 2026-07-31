"""Non-destructive STRM cleanup plans built from complete scan evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    LibraryScanDiff,
    LibraryScanRun,
    MediaLibrary,
    StrmCleanupPlan,
    StrmManifestEntry,
)
from watch_assistant.services.strm_manifest import StrmManifestError, _remove_managed


class StrmCleanupPlanError(ValueError):
    """Stable local error without paths or remote values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class StrmCleanupPlanView:
    plan_id: str
    library_id: str
    source_scan_run_id: str
    source_snapshot_revision: int
    plan_hash: str
    status: str
    revision: int
    expires_at: datetime
    candidate_count: int
    executable_count: int
    blocked_count: int

    def to_public_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "library_id": self.library_id,
            "source_scan_run_id": self.source_scan_run_id,
            "source_snapshot_revision": self.source_snapshot_revision,
            "plan_hash": self.plan_hash,
            "status": self.status,
            "revision": self.revision,
            "expires_at": self.expires_at.isoformat(),
            "candidate_count": self.candidate_count,
            "executable_count": self.executable_count,
            "blocked_count": self.blocked_count,
        }


@dataclass(frozen=True, slots=True)
class StrmCleanupApplyView:
    plan: StrmCleanupPlanView
    retired: int


class StrmCleanupPlanService:
    """Persist reviewable candidates without retiring manifests or files."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_plan(
        self,
        *,
        library_id: str,
        source_scan_run_id: str,
        output_root: Path | str,
        playback_url_prefix: str,
        now: datetime | None = None,
    ) -> StrmCleanupPlanView:
        if not _valid_id(library_id) or not _valid_id(source_scan_run_id):
            raise StrmCleanupPlanError("invalid_request")
        root = _readable_root(output_root)
        prefix = _safe_prefix(playback_url_prefix)
        current_time = _utc(now)
        async with self._session_factory() as session:
            library, run = await self._validated_current_run(
                session, library_id, source_scan_run_id
            )
            diffs = list(
                (
                    await session.scalars(
                        select(LibraryScanDiff)
                        .where(
                            LibraryScanDiff.scan_run_id == run.id,
                            LibraryScanDiff.object_type == "file",
                            LibraryScanDiff.change_kind == "removed",
                        )
                        .order_by(LibraryScanDiff.object_id)
                    )
                ).all()
            )
            object_ids = [item.object_id for item in diffs]
            manifests = {
                item.cloud_file_id: item
                for item in (
                    await session.scalars(
                        select(StrmManifestEntry).where(
                            StrmManifestEntry.library_id == library.id,
                            StrmManifestEntry.cloud_file_id.in_(object_ids),
                            StrmManifestEntry.is_current.is_(True),
                        )
                    )
                ).all()
            } if object_ids else {}
            candidates = []
            for object_id in object_ids:
                manifest = manifests.get(object_id)
                if manifest is None:
                    continue
                state = _managed_state(
                    root,
                    manifest.local_relative_path,
                    f"{prefix}{manifest.manifest_id}\n",
                )
                candidates.append(
                    {
                        "manifest_id": manifest.manifest_id,
                        "cloud_file_id": manifest.cloud_file_id,
                        "local_relative_path": manifest.local_relative_path,
                        "state": state,
                    }
                )
            canonical = {
                "library_id": library.id,
                "source_scan_run_id": run.id,
                "source_snapshot_revision": run.snapshot_revision,
                "candidates": candidates,
            }
            plan_hash = hashlib.sha256(
                json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            existing = await session.scalar(
                select(StrmCleanupPlan).where(StrmCleanupPlan.plan_hash == plan_hash)
            )
            if existing is not None:
                return _view(existing)
            plan = StrmCleanupPlan(
                id="strm_cleanup_" + uuid.uuid4().hex,
                library_id=library.id,
                source_scan_run_id=run.id,
                source_snapshot_revision=run.snapshot_revision,
                candidates_json=json.dumps(candidates, ensure_ascii=True, sort_keys=True),
                status="needs_review",
                revision=1,
                expires_at=current_time + timedelta(hours=24),
                plan_hash=plan_hash,
            )
            session.add(plan)
            await session.commit()
            await session.refresh(plan)
            return _view(plan)

    async def get_plan(self, plan_id: str) -> StrmCleanupPlanView:
        if not _valid_id(plan_id):
            raise StrmCleanupPlanError("plan_not_found")
        async with self._session_factory() as session:
            plan = await session.get(StrmCleanupPlan, plan_id)
            if plan is None:
                raise StrmCleanupPlanError("plan_not_found")
            return _view(plan)

    async def apply_plan(
        self,
        *,
        plan_id: str,
        expected_revision: int,
        digest: str,
        confirm: bool,
        idempotency_key: str,
        output_root: Path | str,
        playback_url_prefix: str,
        now: datetime | None = None,
    ) -> StrmCleanupApplyView:
        if (
            not _valid_id(plan_id)
            or not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
            or not isinstance(digest, str)
            or len(digest) != 64
            or not all(char in "0123456789abcdef" for char in digest.lower())
            or not confirm
            or not _valid_id(idempotency_key)
        ):
            raise StrmCleanupPlanError("invalid_request")
        root = _readable_root(output_root)
        prefix = _safe_prefix(playback_url_prefix)
        current_time = _utc(now)
        async with self._session_factory() as session:
            plan = await session.get(StrmCleanupPlan, plan_id)
            if plan is None:
                raise StrmCleanupPlanError("plan_not_found")
            if not hmac.compare_digest(plan.plan_hash, digest.lower()):
                raise StrmCleanupPlanError("plan_digest_mismatch")
            if plan.status == "applied":
                return StrmCleanupApplyView(_view(plan), 0)
            if plan.revision != expected_revision:
                raise StrmCleanupPlanError("plan_revision_changed")
            if plan.status != "needs_review":
                raise StrmCleanupPlanError("cleanup_plan_not_reviewable")
            if _utc(plan.expires_at) <= current_time:
                raise StrmCleanupPlanError("cleanup_plan_expired")
            library, _run = await self._validated_current_run(
                session, plan.library_id, plan.source_scan_run_id
            )
            candidates = _candidates(plan)
            manifest_ids = [item["manifest_id"] for item in candidates]
            manifests = {
                item.manifest_id: item
                for item in (
                    await session.scalars(
                        select(StrmManifestEntry).where(
                            StrmManifestEntry.library_id == library.id,
                            StrmManifestEntry.manifest_id.in_(manifest_ids),
                            StrmManifestEntry.is_current.is_(True),
                        )
                    )
                ).all()
            } if manifest_ids else {}
            preflight: list[tuple[StrmManifestEntry, str]] = []
            for item in candidates:
                manifest = manifests.get(item["manifest_id"])
                if manifest is None or manifest.cloud_file_id != item["cloud_file_id"]:
                    raise StrmCleanupPlanError("cleanup_plan_changed")
                state = _managed_state(
                    root,
                    manifest.local_relative_path,
                    f"{prefix}{manifest.manifest_id}\n",
                )
                if state not in {"ready", "missing"}:
                    raise StrmCleanupPlanError("cleanup_plan_blocked")
                preflight.append((manifest, state))
            retired = 0
            for manifest, state in preflight:
                if state == "ready":
                    try:
                        _remove_managed(
                            root,
                            manifest.local_relative_path,
                            f"{prefix}{manifest.manifest_id}\n".encode(),
                        )
                    except StrmManifestError:
                        raise StrmCleanupPlanError("cleanup_plan_blocked") from None
                manifest.is_current = False
                manifest.status = "retired"
                retired += 1
            plan.status = "applied"
            plan.revision += 1
            await session.commit()
            await session.refresh(plan)
            return StrmCleanupApplyView(_view(plan), retired)

    async def _validated_current_run(
        self, session: AsyncSession, library_id: str, scan_run_id: str
    ) -> tuple[MediaLibrary, LibraryScanRun]:
        library = await session.get(MediaLibrary, library_id)
        run = await session.get(LibraryScanRun, scan_run_id)
        if (
            library is None
            or not library.enabled
            or not library.scope_verified
            or run is None
            or run.library_id != library_id
            or run.root_directory_id != library.root_directory_id
            or run.state != "completed"
            or not run.complete
            or run.snapshot_revision is None
        ):
            raise StrmCleanupPlanError("source_snapshot_not_ready")
        latest = await session.scalar(
            select(LibraryScanRun.snapshot_revision)
            .where(
                LibraryScanRun.library_id == library_id,
                LibraryScanRun.complete.is_(True),
                LibraryScanRun.state == "completed",
            )
            .order_by(LibraryScanRun.snapshot_revision.desc())
            .limit(1)
        )
        if latest != run.snapshot_revision:
            raise StrmCleanupPlanError("source_snapshot_not_current")
        return library, run


def _view(plan: StrmCleanupPlan) -> StrmCleanupPlanView:
    candidates = _candidates(plan)
    executable = sum(
        isinstance(item, dict) and item.get("state") in {"ready", "missing"}
        for item in candidates
    )
    return StrmCleanupPlanView(
        plan_id=plan.id,
        library_id=plan.library_id,
        source_scan_run_id=plan.source_scan_run_id,
        source_snapshot_revision=plan.source_snapshot_revision,
        plan_hash=plan.plan_hash,
        status=plan.status,
        revision=plan.revision,
        expires_at=plan.expires_at,
        candidate_count=len(candidates),
        executable_count=executable,
        blocked_count=len(candidates) - executable,
    )


def _candidates(plan: StrmCleanupPlan) -> list[dict[str, str]]:
    try:
        candidates = json.loads(plan.candidates_json)
    except (TypeError, json.JSONDecodeError):
        raise StrmCleanupPlanError("plan_invalid") from None
    if not isinstance(candidates, list) or any(
        not isinstance(item, dict)
        or not all(
            isinstance(item.get(key), str)
            for key in (
                "manifest_id",
                "cloud_file_id",
                "local_relative_path",
                "state",
            )
        )
        for item in candidates
    ):
        raise StrmCleanupPlanError("plan_invalid")
    return candidates


def _managed_state(root: Path, relative_path: str, expected: str) -> str:
    if not _valid_relative_path(relative_path):
        return "unsafe"
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        target.parent.resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        return "unsafe"
    if not target.exists():
        return "missing"
    if target.is_symlink() or not target.is_file():
        return "unsafe"
    try:
        actual = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return "unreadable"
    return "ready" if actual == expected else "changed"


def _readable_root(value: Path | str) -> Path:
    root = Path(value)
    try:
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise StrmCleanupPlanError("strm_output_unavailable") from error
    if not resolved.is_dir():
        raise StrmCleanupPlanError("strm_output_unavailable")
    return resolved


def _safe_prefix(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise StrmCleanupPlanError("invalid_playback_url_prefix")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise StrmCleanupPlanError("invalid_playback_url_prefix")
    return value.rstrip("/") + "/"


def _valid_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and value.isascii()
        and "/" not in value
        and "\\" not in value
    )


def _valid_relative_path(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 1024 or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and path.as_posix() == value and all(
        part not in {"", ".", ".."} for part in path.parts
    )


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


__all__ = [
    "StrmCleanupApplyView",
    "StrmCleanupPlanError",
    "StrmCleanupPlanService",
    "StrmCleanupPlanView",
]

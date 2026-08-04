"""Durable ownership evidence for directories created by organization writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    ManagedDirectoryOwnership,
    ManagedDirectoryOwnershipStatus,
)


class ManagedDirectoryOwnershipError(ValueError):
    """Stable local error for an unavailable or conflicting ownership record."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ManagedDirectoryOwnershipView:
    directory_id: str
    library_id: str
    parent_directory_id: str
    name: str
    relative_path: str
    status: str
    revision: int


class ManagedDirectoryOwnershipService:
    """Read and transition only directory ownership proven by a successful mkdir."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def register_created(
        self,
        *,
        directory_id: str,
        library_id: str,
        parent_directory_id: str,
        name: str,
        relative_path: str,
        operation_id: str | None = None,
    ) -> ManagedDirectoryOwnershipView:
        _validate_directory_id(directory_id)
        _validate_library_id(library_id)
        _validate_directory_id(parent_directory_id)
        _validate_name(name)
        _validate_relative_path(relative_path)
        if operation_id is not None:
            _validate_operation_id(operation_id)
        async with self._session_factory() as session:
            existing = await session.get(ManagedDirectoryOwnership, directory_id)
            if existing is not None:
                self._assert_identity(
                    existing,
                    library_id=library_id,
                    parent_directory_id=parent_directory_id,
                    name=name,
                    relative_path=relative_path,
                )
                if existing.status == ManagedDirectoryOwnershipStatus.ACTIVE.value:
                    return _view(existing)
                if existing.status != ManagedDirectoryOwnershipStatus.RECYCLED.value:
                    raise ManagedDirectoryOwnershipError(
                        "directory_ownership_unavailable"
                    )
                existing.status = ManagedDirectoryOwnershipStatus.ACTIVE.value
                existing.recycled_at = None
                existing.revision += 1
                existing.updated_at = _utc_now()
                await session.commit()
                await session.refresh(existing)
                return _view(existing)

            record = ManagedDirectoryOwnership(
                directory_id=directory_id,
                library_id=library_id,
                parent_directory_id=parent_directory_id,
                name=name,
                relative_path=relative_path,
                status=ManagedDirectoryOwnershipStatus.ACTIVE.value,
                created_by_operation_id=operation_id,
                revision=1,
            )
            session.add(record)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.get(
                    ManagedDirectoryOwnership, directory_id
                )
                if existing is None:
                    raise ManagedDirectoryOwnershipError(
                        "directory_ownership_unavailable"
                    ) from None
                self._assert_identity(
                    existing,
                    library_id=library_id,
                    parent_directory_id=parent_directory_id,
                    name=name,
                    relative_path=relative_path,
                )
                return _view(existing)
            await session.refresh(record)
            return _view(record)

    async def list_active(
        self, library_id: str
    ) -> dict[str, ManagedDirectoryOwnershipView]:
        _validate_library_id(library_id)
        async with self._session_factory() as session:
            records = list(
                (
                    await session.scalars(
                        select(ManagedDirectoryOwnership).where(
                            ManagedDirectoryOwnership.library_id == library_id,
                            ManagedDirectoryOwnership.status
                            == ManagedDirectoryOwnershipStatus.ACTIVE.value,
                        )
                    )
                ).all()
            )
        return {record.directory_id: _view(record) for record in records}

    async def has_active_records(self) -> bool:
        async with self._session_factory() as session:
            record = await session.scalar(
                select(ManagedDirectoryOwnership.directory_id)
                .where(
                    ManagedDirectoryOwnership.status
                    == ManagedDirectoryOwnershipStatus.ACTIVE.value
                )
                .limit(1)
            )
        return record is not None

    async def get_active(
        self,
        directory_id: str,
        *,
        library_id: str | None = None,
        parent_directory_id: str | None = None,
        name: str | None = None,
        relative_path: str | None = None,
    ) -> ManagedDirectoryOwnershipView | None:
        _validate_directory_id(directory_id)
        if library_id is not None:
            _validate_library_id(library_id)
        if parent_directory_id is not None:
            _validate_directory_id(parent_directory_id)
        if name is not None:
            _validate_name(name)
        if relative_path is not None:
            _validate_relative_path(relative_path)
        async with self._session_factory() as session:
            record = await session.scalar(
                select(ManagedDirectoryOwnership).where(
                    ManagedDirectoryOwnership.directory_id == directory_id,
                    ManagedDirectoryOwnership.status
                    == ManagedDirectoryOwnershipStatus.ACTIVE.value,
                )
            )
        if record is None:
            return None
        if library_id is not None and record.library_id != library_id:
            return None
        if (
            parent_directory_id is not None
            and record.parent_directory_id != parent_directory_id
        ):
            return None
        if name is not None and record.name != name:
            return None
        if relative_path is not None and record.relative_path != relative_path:
            return None
        return _view(record)

    async def mark_recycled(
        self,
        *,
        directory_id: str,
        library_id: str,
        parent_directory_id: str,
        name: str,
        relative_path: str,
        now: datetime | None = None,
    ) -> ManagedDirectoryOwnershipView:
        return await self._transition(
            directory_id=directory_id,
            library_id=library_id,
            parent_directory_id=parent_directory_id,
            name=name,
            relative_path=relative_path,
            target=ManagedDirectoryOwnershipStatus.RECYCLED.value,
            now=now,
        )

    async def mark_restored(
        self,
        *,
        directory_id: str,
        library_id: str,
        parent_directory_id: str,
        name: str,
        relative_path: str,
        now: datetime | None = None,
    ) -> ManagedDirectoryOwnershipView:
        return await self._transition(
            directory_id=directory_id,
            library_id=library_id,
            parent_directory_id=parent_directory_id,
            name=name,
            relative_path=relative_path,
            target=ManagedDirectoryOwnershipStatus.ACTIVE.value,
            now=now,
        )

    async def _transition(
        self,
        *,
        directory_id: str,
        library_id: str,
        parent_directory_id: str,
        name: str,
        relative_path: str,
        target: str,
        now: datetime | None,
    ) -> ManagedDirectoryOwnershipView:
        _validate_directory_id(directory_id)
        _validate_library_id(library_id)
        _validate_directory_id(parent_directory_id)
        _validate_name(name)
        _validate_relative_path(relative_path)
        if target not in {
            ManagedDirectoryOwnershipStatus.ACTIVE.value,
            ManagedDirectoryOwnershipStatus.RECYCLED.value,
        }:
            raise ManagedDirectoryOwnershipError("directory_ownership_unavailable")
        current_time = _as_utc(now)
        async with self._session_factory() as session:
            record = await session.get(ManagedDirectoryOwnership, directory_id)
            if record is None:
                raise ManagedDirectoryOwnershipError("directory_ownership_missing")
            self._assert_identity(
                record,
                library_id=library_id,
                parent_directory_id=parent_directory_id,
                name=name,
                relative_path=relative_path,
            )
            if record.status == target:
                return _view(record)
            if record.status not in {
                ManagedDirectoryOwnershipStatus.ACTIVE.value,
                ManagedDirectoryOwnershipStatus.RECYCLED.value,
            }:
                raise ManagedDirectoryOwnershipError("directory_ownership_unavailable")
            record.status = target
            record.revision += 1
            record.updated_at = current_time
            record.recycled_at = (
                current_time
                if target == ManagedDirectoryOwnershipStatus.RECYCLED.value
                else None
            )
            await session.commit()
            await session.refresh(record)
            return _view(record)

    @staticmethod
    def _assert_identity(
        record: ManagedDirectoryOwnership,
        *,
        library_id: str,
        parent_directory_id: str,
        name: str,
        relative_path: str,
    ) -> None:
        if (
            record.library_id != library_id
            or record.parent_directory_id != parent_directory_id
            or record.name != name
            or record.relative_path != relative_path
        ):
            raise ManagedDirectoryOwnershipError("directory_ownership_scope_conflict")


def _view(record: ManagedDirectoryOwnership) -> ManagedDirectoryOwnershipView:
    return ManagedDirectoryOwnershipView(
        directory_id=record.directory_id,
        library_id=record.library_id,
        parent_directory_id=record.parent_directory_id,
        name=record.name,
        relative_path=record.relative_path,
        status=record.status,
        revision=record.revision,
    )


def _validate_directory_id(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or value.startswith("0")
        or len(value) > 128
    ):
        raise ManagedDirectoryOwnershipError("invalid_directory_id")


def _validate_library_id(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or not value.isascii()
        or any(character in value for character in "/\\\x00")
    ):
        raise ManagedDirectoryOwnershipError("invalid_library_id")


def _validate_operation_id(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 40
        or not value.isascii()
        or any(character in value for character in "/\\\x00")
    ):
        raise ManagedDirectoryOwnershipError("invalid_operation_id")


def _validate_name(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 255
        or value in {".", ".."}
        or any(character in value for character in "/\\\x00")
    ):
        raise ManagedDirectoryOwnershipError("invalid_directory_name")


def _validate_relative_path(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or "\x00" in value
    ):
        raise ManagedDirectoryOwnershipError("invalid_directory_path")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized:
        raise ManagedDirectoryOwnershipError("invalid_directory_path")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ManagedDirectoryOwnershipError("invalid_directory_path")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return _utc_now()
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


__all__ = [
    "ManagedDirectoryOwnershipError",
    "ManagedDirectoryOwnershipService",
    "ManagedDirectoryOwnershipView",
]

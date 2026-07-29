"""Conservative episode completeness matrix for confirmed local identities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EpisodeMatrixStatus(StrEnum):
    OWNED = "owned"
    MULTIPLE = "multiple"
    MISSING = "missing"
    UNAIRED = "unaired"
    UNKNOWN = "unknown"
    IGNORED = "ignored"
    SPECIAL = "special"


@dataclass(frozen=True, slots=True)
class EpisodeBaseline:
    episode_number: int
    aired: bool
    special: bool = False
    ignored: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.episode_number, bool) or self.episode_number <= 0:
            raise ValueError("episode_number must be positive")


@dataclass(frozen=True, slots=True)
class EpisodeFileReference:
    file_id: str
    episode_numbers: tuple[int, ...]
    special: bool = False
    recognized: bool = True

    def __post_init__(self) -> None:
        if not self.file_id or len(self.file_id) > 128 or "\x00" in self.file_id:
            raise ValueError("invalid file_id")
        if any(isinstance(number, bool) or number <= 0 for number in self.episode_numbers):
            raise ValueError("episode_numbers must be positive")
        if len(set(self.episode_numbers)) != len(self.episode_numbers):
            raise ValueError("duplicate episode number in file reference")


@dataclass(frozen=True, slots=True)
class EpisodeMatrixItem:
    episode_number: int
    status: EpisodeMatrixStatus
    file_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EpisodeCompletenessMatrix:
    items: tuple[EpisodeMatrixItem, ...]
    unrecognized_file_ids: tuple[str, ...]
    inventory_complete: bool
    conclusion_available: bool

    @property
    def missing_episodes(self) -> tuple[int, ...]:
        return tuple(
            item.episode_number
            for item in self.items
            if item.status == EpisodeMatrixStatus.MISSING
        )

    @property
    def duplicate_episodes(self) -> tuple[int, ...]:
        return tuple(
            item.episode_number
            for item in self.items
            if item.status == EpisodeMatrixStatus.MULTIPLE
        )


def build_episode_matrix(
    baseline: tuple[EpisodeBaseline, ...],
    files: tuple[EpisodeFileReference, ...],
    *,
    inventory_complete: bool,
) -> EpisodeCompletenessMatrix:
    """Build a matrix without treating an incomplete inventory as absence."""

    _validate_baseline(baseline)
    by_episode: dict[int, set[str]] = {}
    unrecognized: set[str] = set()
    for reference in files:
        if reference.special or not reference.recognized:
            unrecognized.add(reference.file_id)
            continue
        for episode_number in reference.episode_numbers:
            by_episode.setdefault(episode_number, set()).add(reference.file_id)

    items: list[EpisodeMatrixItem] = []
    for episode in sorted(baseline, key=lambda item: item.episode_number):
        if episode.special:
            status = EpisodeMatrixStatus.SPECIAL
            file_ids = ()
        elif episode.ignored:
            status = EpisodeMatrixStatus.IGNORED
            file_ids = ()
        elif not episode.aired:
            status = EpisodeMatrixStatus.UNAIRED
            file_ids = ()
        else:
            file_ids = tuple(sorted(by_episode.get(episode.episode_number, set())))
            if not file_ids:
                status = (
                    EpisodeMatrixStatus.MISSING
                    if inventory_complete
                    else EpisodeMatrixStatus.UNKNOWN
                )
            elif len(file_ids) == 1:
                status = EpisodeMatrixStatus.OWNED
            else:
                status = EpisodeMatrixStatus.MULTIPLE
        items.append(EpisodeMatrixItem(episode.episode_number, status, file_ids))

    return EpisodeCompletenessMatrix(
        items=tuple(items),
        unrecognized_file_ids=tuple(sorted(unrecognized)),
        inventory_complete=inventory_complete,
        conclusion_available=inventory_complete and bool(baseline),
    )


def _validate_baseline(baseline: tuple[EpisodeBaseline, ...]) -> None:
    numbers = [item.episode_number for item in baseline]
    if len(set(numbers)) != len(numbers):
        raise ValueError("duplicate baseline episode")

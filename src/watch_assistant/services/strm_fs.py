"""Pure filesystem mutation primitives for managed STRM output.

Split from strm_manifest (phase D): write/remove/undo helpers and their
lexical validation, no database access. Error contract: StrmManifestError.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from watch_assistant.services.strm_errors import StrmManifestError
from watch_assistant.services.strm_path import (
    absolute_path as _absolute_path,
)
from watch_assistant.services.strm_path import (
    same_path as _same_path,
)
from watch_assistant.services.strm_scope import normalize_playback_url_prefix


@dataclass(frozen=True, slots=True)
class _FileMutation:
    """The exact file state needed to compensate one local side effect."""

    root: Path
    relative_path: str
    before: bytes | None
    after: bytes | None

def _write(root: Path, relative_path: str, content: bytes) -> bool:
    written, _mutation = _write_with_undo(root, relative_path, content)
    return written

def _write_with_undo(
    root: Path, relative_path: str, content: bytes
) -> tuple[bool, _FileMutation | None]:
    if not _valid_relative_path(relative_path):
        raise StrmManifestError("invalid_managed_path")
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    parent = target.parent
    _assert_no_symlink_components(parent)
    _within(root, parent.resolve(strict=False))
    parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_components(parent)
    resolved_parent = parent.resolve(strict=True)
    _within(root, resolved_parent)
    if target.is_symlink():
        raise StrmManifestError("symlink_target")
    before: bytes | None = None
    if target.is_file():
        try:
            before = target.read_bytes()
        except OSError as error:
            raise StrmManifestError("managed_file_not_readable") from error
        if before == content:
            return False, None
    if target.exists() and not target.is_file():
        raise StrmManifestError("target_not_file")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".watch-assistant-", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return True, _FileMutation(root, relative_path, before, content)

def _remove_with_undo(
    root: Path,
    relative_path: str,
    expected: bytes,
    *,
    tolerate_missing: bool = False,
) -> _FileMutation | None:
    if not _valid_relative_path(relative_path):
        raise StrmManifestError("invalid_managed_path")
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    _assert_no_symlink_components(target.parent)
    try:
        resolved_parent = target.parent.resolve(strict=True)
    except OSError as error:
        if tolerate_missing and not target.exists():
            return None
        raise StrmManifestError("managed_parent_not_safe") from error
    _within(root, resolved_parent)
    if target.is_symlink():
        raise StrmManifestError("managed_file_not_safe")
    if not target.exists():
        return None
    if not target.is_file():
        raise StrmManifestError("managed_file_not_safe")
    try:
        actual = target.read_bytes()
    except OSError as error:
        raise StrmManifestError("managed_file_not_readable") from error
    if actual != expected:
        raise StrmManifestError("managed_file_changed")
    target.unlink()
    return _FileMutation(root, relative_path, actual, None)

def _remove_managed(
    root: Path,
    relative_path: str,
    expected: bytes,
    *,
    tolerate_missing: bool = False,
) -> bool:
    return (
        _remove_with_undo(
            root,
            relative_path,
            expected,
            tolerate_missing=tolerate_missing,
        )
        is not None
    )

def _restore_file_mutation(mutation: _FileMutation) -> None:
    current = _read_target(mutation.root, mutation.relative_path)
    if mutation.after is None:
        if current is None:
            _write(mutation.root, mutation.relative_path, mutation.before or b"")
        elif current != mutation.before:
            raise StrmManifestError("uncertain")
        return
    if current is not None and current != mutation.after:
        raise StrmManifestError("uncertain")
    if mutation.before is None:
        if current is not None:
            _remove_managed(
                mutation.root,
                mutation.relative_path,
                mutation.after,
                tolerate_missing=True,
            )
    else:
        _write(mutation.root, mutation.relative_path, mutation.before)

def _restore_file_mutations(mutations: list[_FileMutation]) -> None:
    first_error: BaseException | None = None
    for mutation in reversed(mutations):
        try:
            _restore_file_mutation(mutation)
        except BaseException as error:  # noqa: BLE001 - finish every compensation
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise StrmManifestError("uncertain") from None

def _read_target(root: Path, relative_path: str) -> bytes | None:
    if not _valid_relative_path(relative_path):
        raise StrmManifestError("invalid_managed_path")
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    _assert_no_symlink_components(target.parent)
    try:
        resolved_parent = target.parent.resolve(strict=True)
    except OSError:
        if not target.exists():
            return None
        raise StrmManifestError("managed_parent_not_safe") from None
    _within(root, resolved_parent)
    if target.is_symlink():
        raise StrmManifestError("managed_file_not_safe")
    if not target.exists():
        return None
    if not target.is_file():
        raise StrmManifestError("managed_file_not_safe")
    try:
        return target.read_bytes()
    except OSError as error:
        raise StrmManifestError("managed_file_not_readable") from error

def _is_managed_content(content: bytes, manifest_id: str) -> bool:
    """Recognize a prior stable playback entry for this manifest only."""

    if not content.endswith(b"\n") or b"\n" in content[:-1] or b"\r" in content:
        return False
    try:
        value = content[:-1].decode("ascii")
    except UnicodeDecodeError:
        return False
    marker = quote(manifest_id, safe="")
    if not value.endswith(marker):
        return False
    try:
        normalize_playback_url_prefix(value[: -len(marker)])
    except ValueError:
        return False
    return True

def _safe_root(
    value: Path | str,
    managed_output_roots: Collection[Path] = (),
) -> Path:
    root = _absolute_path(Path(value))
    _assert_no_symlink_components(root)
    if managed_output_roots and not any(
        _same_path(root, allowed) for allowed in managed_output_roots
    ):
        raise StrmManifestError("output_root_not_allowed")
    root.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_components(root)
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise StrmManifestError("output_root_not_directory")
    return resolved

def _within(root: Path, candidate: Path) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise StrmManifestError("output_path_escapes_root") from error

def _safe_prefix(value: object) -> str:
    try:
        return normalize_playback_url_prefix(value)
    except ValueError:
        raise StrmManifestError("invalid_playback_url_prefix") from None

def _valid_relative_path(value: object) -> bool:
    if (
        not isinstance(value, str)
        or len(value) > 1024
        or "\\" in value
        or "\x00" in value
    ):
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and all(
            part not in {"", ".", ".."} and ":" not in part
            for part in path.parts
        )
    )

def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.anchor else Path.cwd()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current /= part
        if current.is_symlink():
            raise StrmManifestError("symlink_path_component")

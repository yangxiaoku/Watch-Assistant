#!/usr/bin/env python3
"""Validate a systemd release tree and normalize its top-level directory mode."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = SCRIPT_ROOT / "src"
if SRC_ROOT.is_dir():
    sys.path.insert(0, str(SRC_ROOT))

from watch_assistant.release_metadata import normalize_release, read_release_commit

DEFAULT_ALLOWED_RELEASES_ROOT = Path("/opt/watch-assistant/releases")
DEFAULT_REQUIRED_PATHS = (
    "VERSION",
    "config/tgto-contract.json",
    "frontend/dist/index.html",
    "src/watch_assistant",
    "deploy/watch-assistant.service",
    "scripts/deploy_systemd_release.sh",
    "scripts/systemd_release_prepare.py",
    "scripts/systemd_release_update.py",
    "scripts/postdeploy_release_check.py",
    "scripts/release_startup_smoke.py",
)
_TOP_LEVEL_MODE = 0o755


class ReleasePrepareError(ValueError):
    """A stable, non-sensitive release preparation failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _is_filesystem_root(path: Path) -> bool:
    return path.parent == path


def _workspace_root() -> Path | None:
    candidate = SCRIPT_ROOT
    if (candidate / ".git").exists() and (candidate / "src" / "watch_assistant").is_dir():
        return candidate
    return None


def _resolve_allowed_root(path: Path) -> Path:
    try:
        if path.is_symlink():
            raise ReleasePrepareError("allowed_releases_root_symlink")
        resolved = path.resolve(strict=True)
    except ReleasePrepareError:
        raise
    except (OSError, RuntimeError):
        raise ReleasePrepareError("allowed_releases_root_missing") from None
    if not resolved.is_dir():
        raise ReleasePrepareError("allowed_releases_root_not_directory")
    if _is_filesystem_root(resolved):
        raise ReleasePrepareError("allowed_releases_root_invalid")
    home = Path.home().resolve()
    workspace = _workspace_root()
    if resolved == home or (workspace is not None and resolved == workspace):
        raise ReleasePrepareError("allowed_releases_root_invalid")
    return resolved


def _resolve_release_root(path: Path, allowed_root: Path) -> Path:
    try:
        if path.is_symlink():
            raise ReleasePrepareError("release_root_symlink")
        resolved = path.resolve(strict=True)
    except ReleasePrepareError:
        raise
    except (OSError, RuntimeError):
        raise ReleasePrepareError("release_root_missing") from None
    if not resolved.is_dir():
        raise ReleasePrepareError("release_root_not_directory")
    if _is_filesystem_root(resolved):
        raise ReleasePrepareError("release_root_filesystem_root")
    home = Path.home().resolve()
    if resolved == home:
        raise ReleasePrepareError("release_root_home")
    workspace = _workspace_root()
    if workspace is not None and _is_within(resolved, workspace):
        raise ReleasePrepareError("release_root_workspace")
    if resolved == allowed_root:
        raise ReleasePrepareError("release_root_is_allowed_root")
    if not _is_within(resolved, allowed_root):
        raise ReleasePrepareError("release_root_out_of_scope")
    return resolved


def _required_path(path: str, release_root: Path) -> tuple[str, Path]:
    relative = Path(path)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise ReleasePrepareError("required_path_invalid")
    if not relative.parts:
        raise ReleasePrepareError("required_path_invalid")
    try:
        resolved = (release_root / relative).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ReleasePrepareError("required_path_missing") from None
    if not _is_within(resolved, release_root):
        raise ReleasePrepareError("required_path_escape")
    return relative.as_posix(), resolved


def _validate_required_path(relative: str, path: Path) -> None:
    is_directory = relative == "src/watch_assistant"
    try:
        metadata = path.stat()
    except (OSError, RuntimeError):
        raise ReleasePrepareError("required_path_missing") from None
    if is_directory:
        if not stat.S_ISDIR(metadata.st_mode):
            raise ReleasePrepareError("required_path_type")
    elif not stat.S_ISREG(metadata.st_mode):
        raise ReleasePrepareError("required_path_type")

    if os.name == "nt":
        return
    mode = stat.S_IMODE(metadata.st_mode)
    required_bits = stat.S_IROTH
    if is_directory:
        required_bits |= stat.S_IXOTH
    if mode & required_bits != required_bits:
        raise ReleasePrepareError("required_path_not_readable")


def _validate_parent_traversal(release_root: Path) -> None:
    if os.name == "nt":
        return
    try:
        metadata = release_root.parent.stat()
    except (OSError, RuntimeError):
        raise ReleasePrepareError("release_parent_unavailable") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReleasePrepareError("release_parent_not_directory")
    if not stat.S_IMODE(metadata.st_mode) & stat.S_IXOTH:
        raise ReleasePrepareError("release_parent_not_traversable")


def _release_root_mode(release_root: Path) -> int:
    try:
        metadata = release_root.stat()
    except (OSError, RuntimeError):
        raise ReleasePrepareError("release_root_unavailable") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReleasePrepareError("release_root_not_directory")
    return stat.S_IMODE(metadata.st_mode)


def _normalize_root_mode(release_root: Path) -> None:
    if os.name == "nt":
        return
    try:
        if _release_root_mode(release_root) != _TOP_LEVEL_MODE:
            os.chmod(release_root, _TOP_LEVEL_MODE)
    except (OSError, RuntimeError):
        raise ReleasePrepareError("release_root_mode_update_failed") from None
    if _release_root_mode(release_root) != _TOP_LEVEL_MODE:
        raise ReleasePrepareError("release_root_mode_invalid")


def _effective_required_paths(
    required_files: Sequence[str] | None,
) -> tuple[str, ...]:
    candidates = (
        *DEFAULT_REQUIRED_PATHS,
        *(tuple(required_files) if required_files is not None else ()),
    )
    if "VERSION" not in candidates:
        candidates = ("VERSION", *candidates)
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str):
            raise ReleasePrepareError("required_path_invalid")
        key = os.path.normcase(os.path.normpath(candidate))
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return tuple(result)


def prepare_release(
    release_root: Path,
    expected_release: str,
    allowed_releases_root: Path = DEFAULT_ALLOWED_RELEASES_ROOT,
    required_files: Sequence[str] | None = None,
) -> str:
    """Validate and prepare one release; return its normalized release id."""

    expected = normalize_release(expected_release)
    if expected is None:
        raise ReleasePrepareError("expected_release_invalid")

    allowed_root = _resolve_allowed_root(Path(allowed_releases_root))
    release_root = _resolve_release_root(Path(release_root), allowed_root)
    _validate_parent_traversal(release_root)

    validated_paths: dict[str, Path] = {}
    for relative in _effective_required_paths(required_files):
        normalized, resolved = _required_path(relative, release_root)
        _validate_required_path(normalized, resolved)
        validated_paths[normalized] = resolved

    actual = read_release_commit(validated_paths["VERSION"])
    if actual is None:
        raise ReleasePrepareError("version_invalid")
    if actual != expected:
        raise ReleasePrepareError("version_mismatch")

    _normalize_root_mode(release_root)
    return actual


def _message(code: str) -> str:
    return {
        "ok": "发布目录准备成功。",
        "expected_release_invalid": "expected release 无效。",
        "version_invalid": "VERSION 无效。",
        "version_mismatch": "VERSION 与 expected release 不一致。",
        "release_root_missing": "发布目录不存在。",
        "release_root_not_directory": "发布根目录不是目录。",
        "release_root_symlink": "发布根目录不能是符号链接。",
        "release_root_filesystem_root": "禁止使用文件系统根目录。",
        "release_root_home": "禁止使用 home 目录。",
        "release_root_workspace": "禁止使用工作区目录。",
        "release_root_is_allowed_root": "发布根目录不能等于允许根目录。",
        "release_root_out_of_scope": "发布根目录超出允许范围。",
        "allowed_releases_root_missing": "允许发布根目录不存在。",
        "allowed_releases_root_not_directory": "允许发布根目录不是目录。",
        "allowed_releases_root_symlink": "允许发布根目录不能是符号链接。",
        "allowed_releases_root_invalid": "允许发布根目录不安全。",
        "required_path_invalid": "关键路径无效。",
        "required_path_missing": "关键路径缺失或不可解析。",
        "required_path_escape": "关键路径通过符号链接逃逸发布目录。",
        "required_path_type": "关键路径类型不正确。",
        "required_path_not_readable": "关键路径缺少服务用户读取权限。",
        "release_parent_unavailable": "发布目录父目录不可用。",
        "release_parent_not_directory": "发布目录父路径不是目录。",
        "release_parent_not_traversable": "发布目录父目录缺少服务用户遍历权限。",
        "release_root_unavailable": "发布根目录不可用。",
        "release_root_mode_update_failed": "发布根目录权限规范化失败。",
        "release_root_mode_invalid": "发布根目录权限校验失败。",
    }.get(code, "发布目录准备失败。")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--expected-release", required=True)
    parser.add_argument(
        "--allowed-releases-root",
        "--allowed-root",
        type=Path,
        default=DEFAULT_ALLOWED_RELEASES_ROOT,
    )
    parser.add_argument(
        "--required-file",
        "--required-path",
        dest="required_files",
        action="append",
        help="额外的相对关键路径；未传入时使用内置发布清单。",
    )
    args = parser.parse_args(argv)
    try:
        release = prepare_release(
            release_root=args.release_root,
            expected_release=args.expected_release,
            allowed_releases_root=args.allowed_releases_root,
            required_files=args.required_files,
        )
    except ReleasePrepareError as exc:
        print(f"系统发布目录准备失败：{_message(exc.code)}")
        print("SYSTEMD_RELEASE_PREPARE_RESULT=failed")
        print(f"SYSTEMD_RELEASE_PREPARE_CODE={exc.code}")
        return 1
    except (OSError, ValueError):
        print("系统发布目录准备失败：文件系统校验失败。")
        print("SYSTEMD_RELEASE_PREPARE_RESULT=failed")
        print("SYSTEMD_RELEASE_PREPARE_CODE=filesystem_validation_failed")
        return 1

    print(f"系统发布目录准备成功：已校验并规范化为 {_TOP_LEVEL_MODE:04o}。")
    print("SYSTEMD_RELEASE_PREPARE_RESULT=ok")
    print(f"SYSTEMD_RELEASE_PREPARE_RELEASE={release[:7]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

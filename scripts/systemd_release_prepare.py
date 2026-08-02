#!/usr/bin/env python3
"""Prepare a systemd release directory without touching its contents."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path
from typing import NamedTuple

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = SCRIPT_ROOT / "src"
if SRC_ROOT.is_dir():
    sys.path.insert(0, str(SRC_ROOT))

from watch_assistant.release_metadata import normalize_release, read_release_commit

DEFAULT_RELEASES_ROOT = Path("/opt/watch-assistant/releases")
DEFAULT_SERVICE_USER = "watch-assistant"
TOP_LEVEL_MODE = 0o755

# Keep this list aligned with release_startup_smoke.py. These files are the
# smallest useful proof that a release contains the service runtime and its
# deployment metadata.
REQUIRED_RELEASE_FILES = (
    "VERSION",
    "config/tgto-contract.json",
    "frontend/dist/index.html",
    "src/watch_assistant/app.py",
    "src/watch_assistant/release_metadata.py",
    "scripts/release_startup_smoke.py",
    "scripts/systemd_release_update.py",
    "scripts/postdeploy_release_check.py",
    "scripts/deploy_systemd_release.sh",
    "scripts/systemd_release_prepare.py",
)


class ReleasePrepareError(ValueError):
    """A stable, user-safe release preparation failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


class ServiceIdentity(NamedTuple):
    uid: int
    gids: tuple[int, ...]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _fail(code: str, message: str) -> None:
    raise ReleasePrepareError(code, message)


def _validate_release_root(
    release_root: Path, allowed_root: Path
) -> tuple[Path, Path]:
    requested_root = _absolute(release_root)
    requested_allowed_root = _absolute(allowed_root)
    if requested_root == requested_allowed_root or not _is_relative_to(
        requested_root, requested_allowed_root
    ):
        _fail("release_path_out_of_scope", "发布目录不在允许的 releases 范围内")
    if not requested_allowed_root.is_dir():
        _fail("releases_root_missing", "允许的 releases 目录不存在")
    if requested_root.is_symlink():
        _fail("release_root_symlink", "发布目录不能是符号链接")
    if not requested_root.is_dir():
        _fail("release_root_invalid", "发布目录不存在或不是目录")

    try:
        resolved_allowed_root = requested_allowed_root.resolve(strict=True)
        resolved_root = requested_root.resolve(strict=True)
    except OSError:
        _fail("release_root_invalid", "发布目录不存在或不是目录")
    if not _is_relative_to(resolved_root, resolved_allowed_root):
        _fail("release_path_out_of_scope", "发布目录不在允许的 releases 范围内")
    return requested_root, resolved_root


def _validate_symlinks(release_root: Path, resolved_root: Path) -> None:
    def on_error(_error: OSError) -> None:
        _fail("release_tree_unreadable", "无法读取发布目录")

    for directory, directory_names, file_names in os.walk(
        release_root, topdown=True, followlinks=False, onerror=on_error
    ):
        for name in (*directory_names, *file_names):
            entry = Path(directory) / name
            if not entry.is_symlink():
                continue
            try:
                target = Path(os.path.realpath(os.fspath(entry)))
            except OSError:
                _fail("release_symlink_invalid", "发布目录包含无效符号链接")
            if not _is_relative_to(target, resolved_root):
                _fail("release_symlink_escape", "发布目录包含越界符号链接")
            if not target.exists():
                _fail("release_symlink_invalid", "发布目录包含无效符号链接")


def _safe_required_file(
    release_root: Path, resolved_root: Path, relative_name: str
) -> Path:
    requested = release_root / relative_name
    try:
        resolved = Path(os.path.realpath(os.fspath(requested)))
    except OSError:
        _fail("required_file_invalid", "发布目录包含无效关键文件")
    if not _is_relative_to(resolved, resolved_root):
        _fail("release_symlink_escape", "发布目录包含越界符号链接")
    if not resolved.is_file():
        _fail("required_file_missing", "发布目录缺少关键文件")
    return requested


def _resolve_service_identity(
    service_user: str | None, release_root: Path
) -> ServiceIdentity:
    if service_user is None:
        try:
            uid = os.getuid()
            gid = os.getgid()
        except AttributeError:
            root_stat = release_root.stat()
            uid = root_stat.st_uid
            gid = root_stat.st_gid
        return ServiceIdentity(uid=uid, gids=(gid,))

    try:
        import pwd

        account = pwd.getpwnam(service_user)
    except (ImportError, KeyError, OSError):
        _fail("service_user_unavailable", "无法解析指定的服务用户")
    gids = {account.pw_gid}
    getgrouplist = getattr(os, "getgrouplist", None)
    if getgrouplist is not None:
        try:
            gids.update(getgrouplist(service_user, account.pw_gid))
        except OSError:
            pass
    return ServiceIdentity(uid=account.pw_uid, gids=tuple(gids))


def _permission_bits(mode: int, identity: ServiceIdentity, file_stat: os.stat_result) -> int:
    if identity.uid == file_stat.st_uid:
        return (mode >> 6) & 0b111
    if file_stat.st_gid in identity.gids:
        return (mode >> 3) & 0b111
    return mode & 0b111


def _require_access(
    path: Path, identity: ServiceIdentity, required_bits: int
) -> None:
    try:
        file_stat = path.stat()
    except OSError:
        _fail("release_access_denied", "服务用户无法读取关键文件或遍历目录")
    mode = stat.S_IMODE(file_stat.st_mode)
    if _permission_bits(mode, identity, file_stat) & required_bits != required_bits:
        _fail("release_access_denied", "服务用户无法读取关键文件或遍历目录")


def _validate_required_access(
    release_root: Path,
    required_files: tuple[Path, ...],
    identity: ServiceIdentity,
) -> None:
    for parent in reversed(release_root.parents):
        if parent.is_dir():
            _require_access(parent, identity, 0b001)
    _require_access(release_root, identity, 0b101)
    checked_directories: set[Path] = {release_root}
    for required_file in required_files:
        relative_parent = required_file.relative_to(release_root).parent
        current = release_root
        for part in relative_parent.parts:
            current /= part
            if current not in checked_directories:
                _require_access(current, identity, 0b101)
                checked_directories.add(current)
        _require_access(required_file, identity, 0b100)


def prepare_release(
    *,
    release_root: Path,
    expected_release: str,
    service_user: str | None = DEFAULT_SERVICE_USER,
    service_identity: ServiceIdentity | None = None,
) -> str:
    """Normalize and validate one release root, without changing its files."""

    expected = normalize_release(expected_release)
    if expected is None:
        _fail("expected_release_invalid", "期望发布版本无效")

    requested_root, resolved_root = _validate_release_root(
        release_root, DEFAULT_RELEASES_ROOT
    )
    _validate_symlinks(requested_root, resolved_root)
    required_files = tuple(
        _safe_required_file(requested_root, resolved_root, relative_name)
        for relative_name in REQUIRED_RELEASE_FILES
    )

    actual = read_release_commit(required_files[0])
    if actual is None:
        _fail("release_version_invalid", "发布目录缺少有效 VERSION")
    if actual != expected:
        _fail("release_version_mismatch", "VERSION 与期望发布版本不一致")

    identity = service_identity or _resolve_service_identity(service_user, requested_root)
    try:
        requested_root.chmod(TOP_LEVEL_MODE)
    except OSError:
        _fail("release_root_permission_failed", "无法规范化发布目录权限")

    _validate_required_access(requested_root, required_files, identity)
    return actual


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="规范化并验证 systemd 发布目录顶层权限"
    )
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--expected-release", required=True)
    parser.add_argument("--service-user", default=DEFAULT_SERVICE_USER)
    args = parser.parse_args(argv)
    try:
        release = prepare_release(
            release_root=args.release_root,
            expected_release=args.expected_release,
            service_user=args.service_user,
        )
    except ReleasePrepareError as exc:
        print("SYSTEMD_RELEASE_PREPARE_RESULT=failed")
        print(f"SYSTEMD_RELEASE_PREPARE_CODE={exc.code}")
        print(f"systemd 发布预检失败：{exc.message}")
        return 1
    print("SYSTEMD_RELEASE_PREPARE_RESULT=ok")
    print(f"SYSTEMD_RELEASE_PREPARE_RELEASE={release[:7]}")
    print("systemd 发布目录预检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Safely copy the 115 cookie assignment from a TgtoDrive dotenv file."""

from __future__ import annotations

import argparse
import os
import re
import stat
import tempfile
from pathlib import Path

from watch_assistant.services.p115_credentials import (
    MAX_COOKIE_BYTES,
    normalize_cookie_text,
)


class CookieSyncError(RuntimeError):
    pass


_COOKIE_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?ENV_115_COOKIES\s*=\s*(.*?)\s*$")


def sync_cookie(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    owner: str | int | None = None,
    group: str | int | None = None,
) -> None:
    source_path = Path(source)
    destination_path = Path(destination)
    try:
        if source_path.resolve() == destination_path.resolve():
            raise CookieSyncError("source and destination must differ")
        cookie = _read_dotenv_cookie(source_path)
        uid, gid = _resolve_owner_group(owner, group)
        _write_secure(destination_path, cookie, uid, gid)
    except CookieSyncError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError):
        raise CookieSyncError("cookie synchronization failed") from None


def _read_dotenv_cookie(path: Path) -> str:
    try:
        file_stat = path.lstat()
    except OSError:
        raise CookieSyncError("source cookie is unavailable") from None
    if not stat.S_ISREG(file_stat.st_mode):
        raise CookieSyncError("source cookie is unavailable")
    try:
        with path.open("rb") as source_file:
            raw = source_file.read(MAX_COOKIE_BYTES + 1)
    except OSError:
        raise CookieSyncError("source cookie is unavailable") from None
    if len(raw) > MAX_COOKIE_BYTES or b"\x00" in raw:
        raise CookieSyncError("source cookie is unavailable")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CookieSyncError("source cookie is unavailable") from None
    values: list[str] = []
    for line in text.splitlines():
        match = _COOKIE_ASSIGNMENT.fullmatch(line)
        if match is not None:
            values.append(_dotenv_value(match.group(1)))
    if len(values) != 1:
        raise CookieSyncError("source cookie is unavailable")
    cookie = normalize_cookie_text(values[0])
    if cookie is None:
        raise CookieSyncError("source cookie is unavailable")
    return cookie


def _dotenv_value(value: str) -> str:
    if not value:
        return value
    if value[0] in {'"', "'"}:
        quote = value[0]
        closing = value.find(quote, 1)
        if closing < 0:
            raise CookieSyncError("source cookie is unavailable")
        suffix = value[closing + 1 :]
        if suffix and not re.fullmatch(r"\s+#.*", suffix):
            raise CookieSyncError("source cookie is unavailable")
        value = value[1:closing]
    else:
        value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
    if "\n" in value or "\r" in value or "\x00" in value:
        raise CookieSyncError("source cookie is unavailable")
    return value


def _resolve_owner_group(
    owner: str | int | None, group: str | int | None
) -> tuple[int | None, int | None]:
    if owner is None and group is None:
        return None, None
    if os.name == "nt":
        raise CookieSyncError("owner and group are unsupported")
    import grp
    import pwd

    try:
        uid = _resolve_id(owner, pwd.getpwnam) if owner is not None else None
        gid = _resolve_id(group, grp.getgrnam) if group is not None else None
    except (KeyError, TypeError, ValueError, OverflowError):
        raise CookieSyncError("owner or group is invalid") from None
    return uid, gid


def _resolve_id(value: str | int, lookup) -> int:
    if isinstance(value, bool):
        raise TypeError
    if isinstance(value, int):
        if value < 0:
            raise ValueError
        return value
    if not isinstance(value, str) or not value:
        raise ValueError
    if value.isdigit():
        return int(value)
    record = lookup(value)
    return int(record.pw_uid if hasattr(record, "pw_uid") else record.gr_gid)


def _write_secure(path: Path, cookie: str, uid: int | None, gid: int | None) -> None:
    if path.is_symlink():
        raise CookieSyncError("destination is a symlink")
    parent = path.parent
    if not parent.is_dir():
        raise CookieSyncError("destination directory is unavailable")
    desired = cookie.encode("ascii")
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError:
        raise CookieSyncError("destination is unavailable") from None
    if existing is not None:
        if not stat.S_ISREG(existing.st_mode):
            raise CookieSyncError("destination is unavailable")
        try:
            same_content = path.read_bytes() == desired
        except OSError:
            raise CookieSyncError("destination is unavailable") from None
        mode_ok = os.name == "nt" or stat.S_IMODE(existing.st_mode) == 0o600
        owner_ok = uid is None or existing.st_uid == uid
        group_ok = gid is None or existing.st_gid == gid
        if same_content and mode_ok and owner_ok and group_ok:
            return
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".p115-cookie-", dir=parent
    )
    temporary_path = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(file_descriptor, 0o600)
            if uid is not None or gid is not None:
                os.fchown(
                    file_descriptor,
                    uid if uid is not None else -1,
                    gid if gid is not None else -1,
                )
        else:
            os.chmod(temporary_path, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="ascii", newline="") as output:
            output.write(cookie)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        if os.name != "nt":
            try:
                directory_fd = os.open(
                    parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--owner")
    parser.add_argument("--group")
    args = parser.parse_args(argv)
    try:
        sync_cookie(args.source, args.destination, owner=args.owner, group=args.group)
    except CookieSyncError:
        print("cookie synchronization failed")
        return 2
    print("cookie synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

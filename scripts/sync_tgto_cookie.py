"""Safely copy a validated TgtoDrive cookie file into the 115 secret path."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from watch_assistant.services.p115_credentials import CookieProvider


class CookieSyncError(RuntimeError):
    pass


def sync_cookie(
    source: str | os.PathLike[str], destination: str | os.PathLike[str]
) -> None:
    source_path = Path(source)
    destination_path = Path(destination)
    try:
        if source_path.resolve() == destination_path.resolve():
            raise CookieSyncError("source and destination must differ")
        cookie = CookieProvider(source_path).load()
        if cookie is None:
            raise CookieSyncError("source cookie is unavailable")
        _write_secure(destination_path, cookie)
    except CookieSyncError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise CookieSyncError("cookie synchronization failed") from error


def _write_secure(path: Path, cookie: str) -> None:
    if path.is_symlink():
        raise CookieSyncError("destination is a symlink")
    parent = path.parent
    if not parent.is_dir():
        raise CookieSyncError("destination directory is unavailable")
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".p115-cookie-", dir=parent
    )
    temporary_path = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(file_descriptor, 0o600)
        else:
            os.chmod(temporary_path, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="ascii", newline="") as output:
            output.write(cookie)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
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
    args = parser.parse_args(argv)
    try:
        sync_cookie(args.source, args.destination)
    except CookieSyncError:
        print("cookie synchronization failed")
        return 2
    print("cookie synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

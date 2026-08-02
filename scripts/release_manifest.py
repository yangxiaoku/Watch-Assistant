#!/usr/bin/env python3
"""Create and validate release manifests with only the Python standard library."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 2
_FULL_SHA_PATTERN = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)
_SHORT_SHA_PATTERN = re.compile(r"[0-9a-f]{7}", re.IGNORECASE)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_VERSION_COMMIT_PATTERN = re.compile(
    r"^commit=(?P<commit>[0-9a-f]{7}|[0-9a-f]{40})$", re.IGNORECASE
)
_ARTIFACT_PATTERN = re.compile(
    r"^watch-assistant-(?P<short>[0-9a-f]{7})-[0-9]{8}-[0-9]{4}\.tar\.gz$",
    re.IGNORECASE,
)


class ReleaseManifestError(ValueError):
    """A stable, non-sensitive release manifest validation failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _invalid(message: str) -> ReleaseManifestError:
    return ReleaseManifestError("manifest_invalid", message)


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise _invalid(f"release manifest {field} is invalid")
    return value


def _require_commit(value: Any, field: str, *, full: bool) -> str:
    pattern = _FULL_SHA_PATTERN if full else _SHORT_SHA_PATTERN
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise _invalid(f"release manifest {field} is invalid")
    return value.lower()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _invalid("release manifest is invalid") from exc
    if not isinstance(payload, dict):
        raise _invalid("release manifest is invalid")
    return payload


def validate_version_commit_file(
    path: Path, *, expected_commit: str | None = None
) -> str:
    """Read one unambiguous release commit from a VERSION file."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReleaseManifestError("version_invalid", "release VERSION is invalid") from exc

    commit_lines = [line for line in lines if line.strip().startswith("commit=")]
    if len(commit_lines) != 1:
        raise ReleaseManifestError("version_invalid", "release VERSION is invalid")
    match = _VERSION_COMMIT_PATTERN.fullmatch(commit_lines[0])
    if match is None:
        raise ReleaseManifestError("version_invalid", "release VERSION is invalid")
    raw_commit = match.group("commit")
    commit = (
        _require_commit(raw_commit, "version commit", full=True)
        if len(raw_commit) == 40
        else _require_commit(raw_commit, "version commit", full=False)
    )
    if expected_commit is not None:
        expected = _require_commit(expected_commit, "expected commit", full=True)
        if commit != expected:
            raise ReleaseManifestError(
                "version_commit_mismatch", "release VERSION commit mismatch"
            )
    return commit


def _validate_schema(payload: dict[str, Any]) -> None:
    if (
        type(payload.get("schema_version")) is not int
        or payload["schema_version"] != _SCHEMA_VERSION
    ):
        raise _invalid("release manifest schema version is invalid")


def _validate_build_payload(
    payload: dict[str, Any],
    *,
    expected_commit: str | None = None,
    expected_short_commit: str | None = None,
    expected_source_sha256: str | None = None,
    expected_frontend_sha256: str | None = None,
    require_build_metadata: bool,
) -> dict[str, Any]:
    _validate_schema(payload)

    commit = _require_commit(payload.get("commit"), "commit", full=True)
    if expected_commit is not None:
        expected = _require_commit(
            expected_commit, "expected commit", full=True
        )
        if commit != expected:
            raise ReleaseManifestError(
                "commit_mismatch", "release manifest commit mismatch"
            )

    short_commit = payload.get("short_commit")
    if require_build_metadata or short_commit is not None:
        short_commit = _require_commit(short_commit, "short_commit", full=False)
        if short_commit != commit[:7]:
            raise ReleaseManifestError(
                "short_commit_mismatch", "release manifest short commit mismatch"
            )
        if expected_short_commit is not None:
            expected_short = _require_commit(
                expected_short_commit, "expected short commit", full=False
            )
            if short_commit != expected_short:
                raise ReleaseManifestError(
                    "short_commit_mismatch", "release manifest short commit mismatch"
                )
    elif expected_short_commit is not None:
        raise _invalid("release manifest short_commit is missing")

    source_sha256 = _require_sha256(payload.get("source_sha256"), "source_sha256")
    if expected_source_sha256 is not None:
        expected_source = _require_sha256(
            expected_source_sha256, "expected source_sha256"
        )
        if source_sha256 != expected_source:
            raise ReleaseManifestError(
                "source_sha256_mismatch", "source hash does not match expected commit"
            )

    frontend_sha256 = _require_sha256(
        payload.get("frontend_sha256"), "frontend_sha256"
    )
    if expected_frontend_sha256 is not None:
        expected_frontend = _require_sha256(
            expected_frontend_sha256, "expected frontend_sha256"
        )
        if frontend_sha256 != expected_frontend:
            raise ReleaseManifestError(
                "frontend_sha256_mismatch",
                "frontend hash does not match archive content",
            )

    if require_build_metadata:
        build_time = payload.get("build_time")
        if not isinstance(build_time, str) or not build_time:
            raise _invalid("release manifest build_time is invalid")
        branch = payload.get("branch")
        if not isinstance(branch, str) or not branch:
            raise _invalid("release manifest branch is invalid")

    return payload


def validate_build_manifest_file(
    path: Path,
    *,
    expected_commit: str | None = None,
    expected_short_commit: str | None = None,
    expected_source_sha256: str | None = None,
    expected_frontend_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate the manifest written inside a release archive."""

    payload = _load_json(path)
    return _validate_build_payload(
        payload,
        expected_commit=expected_commit,
        expected_short_commit=expected_short_commit,
        expected_source_sha256=expected_source_sha256,
        expected_frontend_sha256=expected_frontend_sha256,
        require_build_metadata=True,
    )


def validate_runtime_manifest_file(path: Path, release: str) -> dict[str, Any]:
    """Validate the subset of provenance required before startup smoke."""

    if not isinstance(release, str) or (
        _FULL_SHA_PATTERN.fullmatch(release) is None
        and _SHORT_SHA_PATTERN.fullmatch(release) is None
    ):
        raise _invalid("release identifier is invalid")
    payload = _load_json(path)
    _validate_schema(payload)
    commit = payload.get("commit")
    expected = release.lower()
    if not isinstance(commit, str) or commit.lower() != expected:
        raise ReleaseManifestError(
            "commit_mismatch", "release manifest commit mismatch"
        )
    _require_sha256(payload.get("source_sha256"), "source_sha256")
    _require_sha256(payload.get("frontend_sha256"), "frontend_sha256")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_name = stream.name
                json.dump(payload, stream, ensure_ascii=True, indent=2)
                stream.write("\n")
            os.replace(temporary_name, path)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
    except OSError as exc:
        raise ReleaseManifestError(
            "manifest_write_failed", "release manifest could not be written"
        ) from exc


def _build_payload(
    *,
    commit: str,
    short_commit: str,
    source_sha256: str,
    frontend_sha256: str,
    build_time: str,
    branch: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "commit": commit.lower(),
        "short_commit": short_commit.lower(),
        "source_sha256": source_sha256,
        "frontend_sha256": frontend_sha256,
        "build_time": build_time,
        "branch": branch,
    }
    _validate_build_payload(
        payload,
        expected_commit=commit,
        expected_short_commit=short_commit,
        expected_source_sha256=source_sha256,
        expected_frontend_sha256=frontend_sha256,
        require_build_metadata=True,
    )
    return payload


def write_build_manifest(
    *,
    output: Path,
    commit: str,
    short_commit: str,
    source_sha256: str,
    frontend_sha256: str,
    build_time: str,
    branch: str,
) -> None:
    _write_json(
        output,
        _build_payload(
            commit=commit,
            short_commit=short_commit,
            source_sha256=source_sha256,
            frontend_sha256=frontend_sha256,
            build_time=build_time,
            branch=branch,
        ),
    )


def _artifact_payload(
    *,
    artifact: str,
    commit: str,
    short_commit: str,
    package_sha256: str,
    package_size_bytes: int,
    source_sha256: str,
    frontend_sha256: str,
    version: str,
) -> dict[str, Any]:
    if not isinstance(artifact, str) or _ARTIFACT_PATTERN.fullmatch(artifact) is None:
        raise _invalid("release manifest artifact is invalid")
    if not isinstance(package_size_bytes, int) or package_size_bytes < 0:
        raise _invalid("release manifest package_size_bytes is invalid")
    if not isinstance(version, str) or not version:
        raise _invalid("release manifest version is invalid")

    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "artifact": artifact,
        "commit": commit.lower(),
        "short_commit": short_commit.lower(),
        "package_sha256": package_sha256,
        "package_size_bytes": package_size_bytes,
        "source_sha256": source_sha256,
        "frontend_sha256": frontend_sha256,
        "version": version,
    }
    _validate_schema(payload)
    _require_commit(payload["commit"], "commit", full=True)
    _require_commit(payload["short_commit"], "short_commit", full=False)
    if payload["short_commit"] != payload["commit"][:7]:
        raise ReleaseManifestError(
            "short_commit_mismatch", "release manifest short commit mismatch"
        )
    artifact_match = _ARTIFACT_PATTERN.fullmatch(payload["artifact"])
    if artifact_match is None or artifact_match.group("short").lower() != payload[
        "short_commit"
    ]:
        raise ReleaseManifestError(
            "short_commit_mismatch", "release manifest artifact commit mismatch"
        )
    _require_sha256(payload["package_sha256"], "package_sha256")
    _require_sha256(payload["source_sha256"], "source_sha256")
    _require_sha256(payload["frontend_sha256"], "frontend_sha256")
    return payload


def write_artifact_manifest(
    *,
    output: Path,
    artifact: str,
    commit: str,
    short_commit: str,
    package_sha256: str,
    package_size_bytes: int,
    source_sha256: str,
    frontend_sha256: str,
    version_file: Path,
) -> None:
    try:
        validate_version_commit_file(version_file, expected_commit=commit)
        version = version_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReleaseManifestError(
            "version_invalid", "release VERSION could not be read"
        ) from exc
    _write_json(
        output,
        _artifact_payload(
            artifact=artifact,
            commit=commit,
            short_commit=short_commit,
            package_sha256=package_sha256,
            package_size_bytes=package_size_bytes,
            source_sha256=source_sha256,
            frontend_sha256=frontend_sha256,
            version=version,
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("write-build", help="write the package manifest")
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--commit", required=True)
    build.add_argument("--short-commit", required=True)
    build.add_argument("--source-sha256", required=True)
    build.add_argument("--frontend-sha256", required=True)
    build.add_argument("--build-time", required=True)
    build.add_argument("--branch", required=True)

    verify = commands.add_parser("verify-build", help="validate the package manifest")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--expected-commit", required=True)
    verify.add_argument("--expected-short-commit", required=True)
    verify.add_argument("--expected-source-sha256", required=True)
    verify.add_argument("--expected-frontend-sha256", required=True)

    version = commands.add_parser("verify-version", help="validate VERSION provenance")
    version.add_argument("--version-file", type=Path, required=True)
    version.add_argument("--expected-commit", required=True)

    artifact = commands.add_parser(
        "write-artifact", help="write verification metadata"
    )
    artifact.add_argument("--output", type=Path, required=True)
    artifact.add_argument("--artifact", required=True)
    artifact.add_argument("--commit", required=True)
    artifact.add_argument("--short-commit", required=True)
    artifact.add_argument("--package-sha256", required=True)
    artifact.add_argument("--package-size-bytes", type=int, required=True)
    artifact.add_argument("--source-sha256", required=True)
    artifact.add_argument("--frontend-sha256", required=True)
    artifact.add_argument("--version-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "write-build":
            write_build_manifest(
                output=args.output,
                commit=args.commit,
                short_commit=args.short_commit,
                source_sha256=args.source_sha256,
                frontend_sha256=args.frontend_sha256,
                build_time=args.build_time,
                branch=args.branch,
            )
        elif args.command == "verify-build":
            validate_build_manifest_file(
                args.manifest,
                expected_commit=args.expected_commit,
                expected_short_commit=args.expected_short_commit,
                expected_source_sha256=args.expected_source_sha256,
                expected_frontend_sha256=args.expected_frontend_sha256,
            )
        elif args.command == "verify-version":
            validate_version_commit_file(
                args.version_file, expected_commit=args.expected_commit
            )
        elif args.command == "write-artifact":
            write_artifact_manifest(
                output=args.output,
                artifact=args.artifact,
                commit=args.commit,
                short_commit=args.short_commit,
                package_sha256=args.package_sha256,
                package_size_bytes=args.package_size_bytes,
                source_sha256=args.source_sha256,
                frontend_sha256=args.frontend_sha256,
                version_file=args.version_file,
            )
        else:  # pragma: no cover - argparse enforces the command choices.
            raise _invalid("release manifest command is invalid")
    except ReleaseManifestError as exc:
        print(f"release manifest refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

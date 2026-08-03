import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release_manifest.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("release_manifest", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_manifest = _load_helper()
COMMIT = "a" * 40
SHORT_COMMIT = COMMIT[:7]
SOURCE_SHA256 = "b" * 64
FRONTEND_SHA256 = "c" * 64


def test_build_manifest_round_trip_uses_expected_provenance(tmp_path: Path):
    manifest_path = tmp_path / "release-manifest.json"

    release_manifest.write_build_manifest(
        output=manifest_path,
        commit=COMMIT,
        short_commit=SHORT_COMMIT,
        source_sha256=SOURCE_SHA256,
        frontend_sha256=FRONTEND_SHA256,
        build_time="2026-08-02T00:00:00Z",
        branch="codex/release",
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if os.name == "posix":
        assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o644
    assert payload == {
        "schema_version": 2,
        "commit": COMMIT,
        "short_commit": SHORT_COMMIT,
        "source_sha256": SOURCE_SHA256,
        "frontend_sha256": FRONTEND_SHA256,
        "build_time": "2026-08-02T00:00:00Z",
        "branch": "codex/release",
    }
    assert release_manifest.validate_build_manifest_file(
        manifest_path,
        expected_commit=COMMIT,
        expected_short_commit=SHORT_COMMIT,
        expected_source_sha256=SOURCE_SHA256,
        expected_frontend_sha256=FRONTEND_SHA256,
    ) == payload


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("commit", "b" * 40, "commit mismatch"),
        ("source_sha256", "d" * 64, "source hash does not match"),
        ("frontend_sha256", "d" * 64, "frontend hash does not match"),
        ("build_time", "", "build_time is invalid"),
        ("branch", "", "branch is invalid"),
    ],
)
def test_build_manifest_validation_fails_closed(
    tmp_path: Path, field: str, value: str, message: str
):
    manifest_path = tmp_path / "release-manifest.json"
    release_manifest.write_build_manifest(
        output=manifest_path,
        commit=COMMIT,
        short_commit=SHORT_COMMIT,
        source_sha256=SOURCE_SHA256,
        frontend_sha256=FRONTEND_SHA256,
        build_time="2026-08-02T00:00:00Z",
        branch="codex/release",
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload[field] = value
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(release_manifest.ReleaseManifestError, match=message):
        release_manifest.validate_build_manifest_file(
            manifest_path,
            expected_commit=COMMIT,
            expected_short_commit=SHORT_COMMIT,
            expected_source_sha256=SOURCE_SHA256,
            expected_frontend_sha256=FRONTEND_SHA256,
        )


def test_build_manifest_validation_can_require_publish_branch(tmp_path: Path):
    manifest_path = tmp_path / "release-manifest.json"
    release_manifest.write_build_manifest(
        output=manifest_path,
        commit=COMMIT,
        short_commit=SHORT_COMMIT,
        source_sha256=SOURCE_SHA256,
        frontend_sha256=FRONTEND_SHA256,
        build_time="2026-08-02T00:00:00Z",
        branch="codex/test",
    )

    with pytest.raises(
        release_manifest.ReleaseManifestError, match="branch mismatch"
    ):
        release_manifest.validate_build_manifest_file(
            manifest_path,
            expected_commit=COMMIT,
            expected_short_commit=SHORT_COMMIT,
            expected_branch="codex/publish-main",
            expected_source_sha256=SOURCE_SHA256,
            expected_frontend_sha256=FRONTEND_SHA256,
        )


def test_artifact_manifest_preserves_sha256sums_metadata(tmp_path: Path):
    version_file = tmp_path / "VERSION"
    manifest_path = tmp_path / "release-manifest.json"
    version = "commit=" + COMMIT + "\nbuild_time=2026-08-02T00:00:00Z\n"
    version_file.write_text(version, encoding="utf-8")

    release_manifest.write_artifact_manifest(
        output=manifest_path,
        artifact="watch-assistant-aaaaaaa-20260802-0000.tar.gz",
        commit=COMMIT,
        short_commit=SHORT_COMMIT,
        package_sha256="d" * 64,
        package_size_bytes=1234,
        source_sha256=SOURCE_SHA256,
        frontend_sha256=FRONTEND_SHA256,
        version_file=version_file,
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if os.name == "posix":
        assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o644
    assert payload["commit"] == COMMIT
    assert payload["short_commit"] == SHORT_COMMIT
    assert payload["package_size_bytes"] == 1234
    assert payload["source_sha256"] == SOURCE_SHA256
    assert payload["frontend_sha256"] == FRONTEND_SHA256
    assert payload["version"] == version


@pytest.mark.parametrize(
    "version",
    [
        "commit=" + COMMIT + "\ncommit=" + COMMIT + "\n",
        "commit=not-a-release\n",
    ],
)
def test_version_commit_validation_rejects_ambiguous_or_invalid_version(
    tmp_path: Path, version: str
):
    version_file = tmp_path / "VERSION"
    version_file.write_text(version, encoding="utf-8")

    with pytest.raises(release_manifest.ReleaseManifestError, match="VERSION is invalid"):
        release_manifest.validate_version_commit_file(version_file)


def test_version_commit_validation_requires_expected_full_commit(tmp_path: Path):
    version_file = tmp_path / "VERSION"
    version_file.write_text("commit=" + COMMIT + "\n", encoding="utf-8")

    with pytest.raises(
        release_manifest.ReleaseManifestError, match="VERSION commit mismatch"
    ):
        release_manifest.validate_version_commit_file(
            version_file, expected_commit="b" * 40
        )


def test_version_commit_validation_can_require_publish_branch(tmp_path: Path):
    version_file = tmp_path / "VERSION"
    version_file.write_text(
        "commit=" + COMMIT + "\nbuild_time=2026-08-02T00:00:00Z\n"
        "branch=codex/test\n",
        encoding="utf-8",
    )

    with pytest.raises(
        release_manifest.ReleaseManifestError, match="VERSION branch mismatch"
    ):
        release_manifest.validate_version_commit_file(
            version_file,
            expected_commit=COMMIT,
            expected_branch="codex/publish-main",
        )


def test_runtime_manifest_validation_keeps_startup_provenance_gate(tmp_path: Path):
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "commit": COMMIT,
                "source_sha256": SOURCE_SHA256,
                "frontend_sha256": FRONTEND_SHA256,
            }
        ),
        encoding="utf-8",
    )

    assert release_manifest.validate_runtime_manifest_file(manifest_path, COMMIT)[
        "commit"
    ] == COMMIT
    with pytest.raises(release_manifest.ReleaseManifestError, match="commit mismatch"):
        release_manifest.validate_runtime_manifest_file(manifest_path, "b" * 40)

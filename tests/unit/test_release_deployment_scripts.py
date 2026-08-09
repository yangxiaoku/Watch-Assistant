import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


systemd_release = _load_script("systemd_release_update")
postdeploy_release = _load_script("postdeploy_release_check")


def _write_version(path: Path, commit: str = "abcdef1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"commit={commit}\nbuild_time=2026-08-02T00:00:00Z\n"
        "branch=codex/publish-main\n",
        encoding="utf-8",
    )


def _write_manifest(path: Path, commit: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "commit": commit,
                "short_commit": commit[:7],
                "source_sha256": "a" * 64,
                "frontend_sha256": "b" * 64,
                "build_time": "2026-08-02T00:00:00Z",
                "branch": "codex/publish-main",
            }
        ),
        encoding="utf-8",
    )


def _systemd_identity(current_root: Path, release_env: Path) -> str:
    return (
        f"EnvironmentFiles=/etc/watch-assistant.env (ignore_errors=no) "
        f"-{release_env} (ignore_errors=yes)\n"
        "MainPID=1234\n"
        f"WorkingDirectory={current_root}\n"
    )


def _mock_systemctl(
    monkeypatch: pytest.MonkeyPatch, current_root: Path, release_env: Path
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=_systemd_identity(current_root, release_env),
            stderr="",
        )

    monkeypatch.setattr(postdeploy_release.subprocess, "run", run)
    return calls


def test_systemd_release_update_is_atomic_and_preserves_stale_drop_in(tmp_path: Path):
    version_file = tmp_path / "VERSION"
    release_env = tmp_path / "state" / "release.env"
    stale_drop_in = tmp_path / "systemd" / "release.conf"
    _write_version(version_file)
    release_env.parent.mkdir()
    release_env.write_text("WATCH_ASSISTANT_RELEASE=0123456\n", encoding="utf-8")
    stale_drop_in.parent.mkdir()
    stale_drop_in.write_text("[Service]\nEnvironment=WATCH_ASSISTANT_RELEASE=0123456\n")

    release = systemd_release.update_release_metadata(
        version_file=version_file,
        release_env=release_env,
        stale_drop_in=stale_drop_in,
    )

    assert release == "abcdef1"
    assert release_env.read_text(encoding="utf-8") == "WATCH_ASSISTANT_RELEASE=abcdef1\n"
    assert stale_drop_in.read_text(encoding="utf-8") == (
        "[Service]\nEnvironment=WATCH_ASSISTANT_RELEASE=0123456\n"
    )
    assert list(release_env.parent.glob(".release.env.*.tmp")) == []


def test_release_build_and_verify_use_provenance_and_project_venv():
    build = (ROOT / "scripts" / "build_release.sh").read_text(encoding="utf-8")
    verify = (ROOT / "scripts" / "verify_release_artifact.sh").read_text(
        encoding="utf-8"
    )
    verify_gate = (ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")

    assert 'git archive --format=tar "$EXPECTED_COMMIT"' in build
    assert 'FRONTEND_PM="${WATCH_ASSISTANT_FRONTEND_PM:-}"' in build
    assert 'command -v npm' in build
    assert 'command -v pnpm' in build
    assert '"$FRONTEND_PM" ci' in build
    assert '"$FRONTEND_PM" run build' in build
    assert '"$FRONTEND_PM" --dir frontend install --no-lockfile' in build
    assert '"$FRONTEND_PM" --dir frontend run build' in build
    assert 'must point to npm or pnpm' in build
    assert "release-manifest.json" in build
    assert "--source-sha256" in build and "--frontend-sha256" in build
    assert "Environment=PYTHONPATH=/opt/watch-assistant/current/src" in (
        ROOT / "deploy" / "watch-assistant.service"
    ).read_text(encoding="utf-8")
    assert "release_startup_smoke.py" in verify
    assert "git archive --format=tar \"$EXPECTED_COMMIT\"" in verify
    assert "--frontend-sha256" in verify
    assert "release_manifest.py" in build
    assert "release_manifest.py" in verify
    assert '"$ROOT_DIR/scripts/release_manifest.py"' in verify
    assert '--expected-branch "$RELEASE_BRANCH"' in verify
    assert "--unit-sha256" in build
    assert "--expected-unit-sha256" in verify
    assert "wc -c" in verify
    for required in (
        "deploy/watch-assistant.service",
        "scripts/deploy_systemd_release.sh",
        "scripts/systemd_release_prepare.py",
        "scripts/systemd_release_update.py",
        "scripts/postdeploy_release_check.py",
        "scripts/systemd_unit.py",
        "scripts/systemd_backup.py",
    ):
        assert required in verify
    assert "cmp -s" in verify
    assert "jq" not in build.lower()
    assert "jq" not in verify.lower()
    assert "${PYTHON_BIN:-python}" not in build
    assert "${PYTHON_BIN:-python}" not in verify_gate
    assert "worktree .venv Python is required" in verify_gate
    for script in (build, verify):
        assert 'git ls-remote --exit-code origin "$REMOTE_REF"' in script
        assert "could not be verified" in script
        assert 'refs/remotes/origin/${RELEASE_BRANCH}' in script


def test_release_package_gate_is_limited_to_publish_main():
    workflow = (ROOT / ".github" / "workflows" / "systemd-release.yml").read_text(
        encoding="utf-8"
    )
    build = (ROOT / "scripts" / "build_release.sh").read_text(encoding="utf-8")
    verify = (ROOT / "scripts" / "verify_release_artifact.sh").read_text(
        encoding="utf-8"
    )

    assert "codex/ci-release-artifact" not in workflow
    assert 'test "${GITHUB_REF}" = "refs/heads/codex/publish-main"' in workflow
    assert 'test "$(git branch --show-current)" = "codex/publish-main"' in workflow
    assert 'refs/remotes/origin/codex/publish-main' in workflow
    assert 'PYTHON_VERSION: "3.12.13"' in workflow
    assert 'NODE_VERSION: "24"' in workflow
    assert 'NPM_VERSION: "11.9.0"' in workflow
    for script in (build, verify):
        assert 'RELEASE_BRANCH="codex/publish-main"' in script
        assert 'refs/remotes/origin/${RELEASE_BRANCH}' in script


def test_ci_workflows_cover_locked_offline_browser_compose_and_release_gates():
    verify_workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text(
        encoding="utf-8"
    )
    release_workflow = (ROOT / ".github" / "workflows" / "systemd-release.yml").read_text(
        encoding="utf-8"
    )
    verify_gate = (ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")

    assert 'PYTHON_VERSION: "3.12.13"' in verify_workflow
    assert 'NODE_VERSION: "24"' in verify_workflow
    assert 'NPM_VERSION: "11.9.0"' in verify_workflow
    assert 'python-version: ${{ env.PYTHON_VERSION }}' in verify_workflow
    assert 'test "$(python --version 2>&1)" = "Python ${PYTHON_VERSION}"' in verify_workflow
    assert '"$PYTHON_BIN" scripts/generate_pip_constraints.py' in verify_workflow
    assert "--lock-file uv.lock" in verify_workflow
    assert '--constraint "$LOCK_CONSTRAINTS"' in verify_workflow
    assert '"$PYTHON_BIN" -m pip check' in verify_workflow
    assert "run: bash scripts/verify.sh" in verify_workflow

    assert "offline_verify:" in verify_workflow
    assert "e2e:" in verify_workflow
    assert "npx playwright install --with-deps chromium" in verify_workflow
    assert "npm --prefix frontend run test:e2e" in verify_workflow
    assert "needs:\n      - offline_verify\n      - e2e\n      - compose" in verify_workflow
    assert 'OFFLINE_VERIFY_RESULT: ${{ needs.offline_verify.result }}' in verify_workflow
    assert 'E2E_RESULT: ${{ needs.e2e.result }}' in verify_workflow
    assert 'COMPOSE_RESULT: ${{ needs.compose.result }}' in verify_workflow
    assert 'WATCH_ASSISTANT_RELEASE="${{ github.sha }}" docker compose' in verify_workflow
    assert "runs-on: ubuntu-latest" not in verify_workflow

    for required in (
        "run_parallel_pytest_files unit-tests tests/unit 8",
        "run_parallel_pytest_files integration-tests tests/integration 1",
        "run_stage contract-tests",
        "run_stage ruff",
        "run_stage frontend-tests",
        "run_stage frontend-build",
    ):
        assert required in verify_gate

    assert 'PYTHON_VERSION: "3.12.13"' in release_workflow
    assert "run: bash scripts/verify.sh" in release_workflow
    assert "docker compose --env-file .env.example config --quiet" in release_workflow
    assert 'bash scripts/build_release.sh "$GITHUB_SHA"' in release_workflow
    assert "bash scripts/verify_release_artifact.sh" in release_workflow


def test_verify_gate_scopes_pytest_collection_to_offline_test_roots():
    verify_gate = (ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")

    assert 'find "$test_root" -maxdepth 1 -type f -name \'test_*.py\'' in verify_gate
    assert 'find "$ROOT_DIR" -type f -name \'test_*.py\'' not in verify_gate
    assert "release-archive" not in verify_gate
    find_commands = [
        line for line in verify_gate.splitlines() if line.lstrip().startswith("find ")
    ]
    assert all("node_modules" not in line for line in find_commands)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_release_shell_scripts_have_valid_bash_syntax():
    for name in (
        "build_release.sh",
        "verify_release_artifact.sh",
        "deploy_systemd_release.sh",
    ):
        result = subprocess.run(
            ["bash", "-n", str(ROOT / "scripts" / name)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def _create_build_release_fixture(
    tmp_path: Path,
) -> tuple[Path, str, Path, Path, Path, dict[str, str]]:
    fixture_root = tmp_path / "fixture-repo"
    (fixture_root / "scripts").mkdir(parents=True)
    (fixture_root / "frontend").mkdir()
    (fixture_root / "deploy").mkdir()
    shutil.copy2(ROOT / "scripts" / "build_release.sh", fixture_root / "scripts")
    shutil.copy2(
        ROOT / "scripts" / "verify_release_artifact.sh", fixture_root / "scripts"
    )
    (fixture_root / "frontend" / "package.json").write_text("{}\n", encoding="utf-8")
    (fixture_root / "deploy" / "watch-assistant.service").write_text(
        "[Unit]\nDescription=fixture\n", encoding="utf-8"
    )
    shutil.copy2(
        ROOT / "scripts" / "release_manifest.py",
        fixture_root / "scripts" / "release_manifest.py",
    )
    startup_smoke = fixture_root / "scripts" / "release_startup_smoke.py"
    startup_smoke.write_text("# fixture\n", encoding="utf-8")
    startup_smoke.chmod(0o755)
    for relative in (
        "src/watch_assistant/app.py",
        "src/watch_assistant/release_metadata.py",
        "scripts/deploy_systemd_release.sh",
        "scripts/systemd_release_prepare.py",
        "scripts/systemd_release_update.py",
        "scripts/postdeploy_release_check.py",
        "scripts/systemd_unit.py",
        "scripts/systemd_backup.py",
    ):
        destination = fixture_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)

    subprocess.run(["git", "init", "-q"], cwd=fixture_root, check=True)
    subprocess.run(
        ["git", "config", "user.name", "release-fixture"],
        cwd=fixture_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "release-fixture@example.invalid"],
        cwd=fixture_root,
        check=True,
    )
    (fixture_root / ".git" / "info" / "exclude").write_text(
        ".venv/\n", encoding="utf-8"
    )

    python_stub = fixture_root / ".venv" / "bin" / "python"
    python_stub.parent.mkdir(parents=True)
    python_stub.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    python_stub.chmod(0o755)

    subprocess.run(["git", "add", "."], cwd=fixture_root, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fixture"], cwd=fixture_root, check=True
    )
    subprocess.run(
        ["git", "switch", "-q", "-c", "codex/publish-main"],
        cwd=fixture_root,
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=fixture_root, text=True
    ).strip()

    remote_root = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote_root)], check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote_root)],
        cwd=fixture_root,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "push",
            "-q",
            "origin",
            "codex/publish-main:refs/heads/codex/publish-main",
        ],
        cwd=fixture_root,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "update-ref",
            "refs/remotes/origin/codex/publish-main",
            commit,
        ],
        cwd=fixture_root,
        check=True,
    )

    tool_bin = tmp_path / "tools"
    tool_bin.mkdir()
    npm_stub = tool_bin / "npm"
    npm_stub.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"run\" ] && [ \"${2:-}\" = \"build\" ]; then\n"
        "    mkdir -p dist\n"
        "    printf '<!doctype html>\\n' > dist/index.html\n"
        "fi\n",
        encoding="utf-8",
    )
    npm_stub.chmod(0o755)

    tar_path = Path(shutil.which("tar")).resolve()
    tar_log = tmp_path / "tar.log"
    tar_stub = tool_bin / "tar"
    tar_stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(tar_log))}\n"
        f"exec {shlex.quote(str(tar_path))} \"$@\"\n",
        encoding="utf-8",
    )
    tar_stub.chmod(0o755)

    output_dir = tmp_path / "release-output"
    environment = os.environ.copy()
    environment["PATH"] = f"{tool_bin}{os.pathsep}{environment['PATH']}"
    environment["RELEASE_OUTPUT_DIR"] = str(output_dir)
    environment["WATCH_ASSISTANT_FRONTEND_PM"] = str(npm_stub)
    return fixture_root, commit, remote_root, output_dir, tar_log, environment


def _run_fixture_build(
    fixture_root: Path, commit: str, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(fixture_root / "scripts" / "build_release.sh"), commit],
        cwd=fixture_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _set_fixture_remote_commit(
    fixture_root: Path, parent_commit: str
) -> str:
    tree = subprocess.check_output(
        ["git", "rev-parse", f"{parent_commit}^{{tree}}"],
        cwd=fixture_root,
        text=True,
    ).strip()
    drift_commit = subprocess.check_output(
        ["git", "commit-tree", tree, "-p", parent_commit],
        cwd=fixture_root,
        input="remote drift\n",
        text=True,
    ).strip()
    subprocess.run(
        [
            "git",
            "push",
            "-q",
            "origin",
            f"{drift_commit}:refs/heads/codex/publish-main",
        ],
        cwd=fixture_root,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "update-ref",
            "refs/remotes/origin/codex/publish-main",
            parent_commit,
        ],
        cwd=fixture_root,
        check=True,
    )
    cached_commit = subprocess.check_output(
        ["git", "rev-parse", "refs/remotes/origin/codex/publish-main"],
        cwd=fixture_root,
        text=True,
    ).strip()
    assert cached_commit == parent_commit
    return drift_commit


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell release semantics are required")
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.skipif(shutil.which("tar") is None, reason="tar is required")
@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="sha256sum is required")
def test_build_release_normalizes_archive_uid_and_gid(tmp_path: Path):
    fixture_root, commit, _, output_dir, tar_log, environment = (
        _create_build_release_fixture(tmp_path)
    )
    result = _run_fixture_build(fixture_root, commit, environment)
    assert result.returncode == 0, result.stderr

    packages = list(output_dir.glob("watch-assistant-*.tar.gz"))
    assert len(packages) == 1
    with tarfile.open(packages[0], "r:gz") as archive:
        members = archive.getmembers()
    assert members
    assert all(
        member.uid == 0 and member.gid == 0 for member in members
    ), [(member.name, member.uid, member.gid) for member in members]
    assert any(
        "--owner=0" in line and "--group=0" in line
        for line in tar_log.read_text(encoding="utf-8").splitlines()
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell release semantics are required")
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.skipif(shutil.which("tar") is None, reason="tar is required")
@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="sha256sum is required")
def test_release_scripts_accept_current_remote_publish_main(tmp_path: Path):
    fixture_root, commit, _, output_dir, _, environment = _create_build_release_fixture(
        tmp_path
    )
    build_result = _run_fixture_build(fixture_root, commit, environment)
    assert build_result.returncode == 0, build_result.stderr
    package_file = next(output_dir.glob("watch-assistant-*.tar.gz"))

    verify_result = subprocess.run(
        [
            "bash",
            str(fixture_root / "scripts" / "verify_release_artifact.sh"),
            str(package_file),
            commit,
            str(tmp_path / "verified-output"),
        ],
        cwd=fixture_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify_result.returncode == 0, verify_result.stderr
    assert "release artifact verified" in verify_result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell release semantics are required")
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.skipif(shutil.which("tar") is None, reason="tar is required")
@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="sha256sum is required")
def test_verify_release_rejects_non_executable_package_script(tmp_path: Path):
    fixture_root, commit, _, output_dir, _, environment = _create_build_release_fixture(
        tmp_path
    )
    build_result = _run_fixture_build(fixture_root, commit, environment)
    assert build_result.returncode == 0, build_result.stderr
    package_file = next(output_dir.glob("watch-assistant-*.tar.gz"))

    extracted_root = tmp_path / "tampered-release"
    with tarfile.open(package_file, "r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    package_root = tmp_path / f"watch-assistant-{commit[:7]}"
    package_root.rename(extracted_root)
    (extracted_root / "scripts" / "deploy_systemd_release.sh").chmod(0o644)
    with tarfile.open(package_file, "w:gz") as archive:
        archive.add(extracted_root, arcname=f"watch-assistant-{commit[:7]}")

    result = subprocess.run(
        [
            "bash",
            str(fixture_root / "scripts" / "verify_release_artifact.sh"),
            str(package_file),
            commit,
            str(tmp_path / "verified-output"),
        ],
        cwd=fixture_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "required release script is not executable" in result.stderr


@pytest.mark.parametrize("remote_failure", ["drift", "unavailable"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX shell release semantics are required")
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.skipif(shutil.which("tar") is None, reason="tar is required")
@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="sha256sum is required")
def test_build_release_fails_closed_on_remote_publish_main_failure(
    tmp_path: Path, remote_failure: str
):
    fixture_root, commit, _, output_dir, _, environment = (
        _create_build_release_fixture(tmp_path)
    )
    if remote_failure == "drift":
        _set_fixture_remote_commit(fixture_root, commit)
        expected_message = "not the current remote"
    else:
        subprocess.run(
            [
                "git",
                "remote",
                "set-url",
                "origin",
                str(tmp_path / "missing-remote.git"),
            ],
            cwd=fixture_root,
            check=True,
        )
        expected_message = "could not be verified"

    result = _run_fixture_build(fixture_root, commit, environment)
    assert result.returncode != 0
    assert "release refused" in result.stderr
    assert expected_message in result.stderr
    if remote_failure == "unavailable":
        assert str(tmp_path / "missing-remote.git") not in result.stderr
    assert not list(output_dir.glob("watch-assistant-*.tar.gz"))


@pytest.mark.parametrize("remote_failure", ["drift", "unavailable"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX shell release semantics are required")
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.skipif(shutil.which("tar") is None, reason="tar is required")
@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="sha256sum is required")
def test_verify_release_fails_closed_on_remote_publish_main_failure(
    tmp_path: Path, remote_failure: str
):
    fixture_root, commit, _, output_dir, _, environment = (
        _create_build_release_fixture(tmp_path)
    )
    build_result = _run_fixture_build(fixture_root, commit, environment)
    assert build_result.returncode == 0, build_result.stderr
    package_file = next(output_dir.glob("watch-assistant-*.tar.gz"))

    if remote_failure == "drift":
        _set_fixture_remote_commit(fixture_root, commit)
        expected_message = "not the current remote"
    else:
        subprocess.run(
            [
                "git",
                "remote",
                "set-url",
                "origin",
                str(tmp_path / "missing-remote.git"),
            ],
            cwd=fixture_root,
            check=True,
        )
        expected_message = "could not be verified"

    result = subprocess.run(
        [
            "bash",
            str(fixture_root / "scripts" / "verify_release_artifact.sh"),
            str(package_file),
            commit,
            str(tmp_path / "verified-output"),
        ],
        cwd=fixture_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "release artifact refused" in result.stderr
    assert expected_message in result.stderr
    if remote_failure == "unavailable":
        assert str(tmp_path / "missing-remote.git") not in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="Linux shell release semantics are required")
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_verify_release_rejects_tampered_package_helper_or_manifest(tmp_path: Path):
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    short_commit = commit[:7]
    source_sha256 = hashlib.sha256(
        subprocess.check_output(["git", "archive", "--format=tar", commit], cwd=ROOT)
    ).hexdigest()

    frontend_root = tmp_path / "frontend" / "dist"
    frontend_root.mkdir(parents=True)
    frontend_file = frontend_root / "index.html"
    frontend_file.write_text("<!doctype html>", encoding="utf-8")
    frontend_sha256 = hashlib.sha256(
        (
            hashlib.sha256(frontend_file.read_bytes()).hexdigest()
            + "  ./index.html\n"
        ).encode()
    ).hexdigest()

    for tampered_file in ("helper", "manifest"):
        package_root = tmp_path / f"watch-assistant-{short_commit}-{tampered_file}"
        (package_root / "frontend" / "dist").mkdir(parents=True)
        (package_root / "src" / "watch_assistant").mkdir(parents=True)
        (package_root / "scripts").mkdir()
        (package_root / "deploy").mkdir()
        (package_root / "VERSION").write_text(
            f"commit={commit}\nbuild_time=2026-08-02T00:00:00Z\n",
            encoding="utf-8",
        )
        (package_root / "frontend" / "dist" / "index.html").write_text(
            frontend_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
        for relative in (
            "src/watch_assistant/app.py",
            "src/watch_assistant/release_metadata.py",
            "scripts/release_startup_smoke.py",
        ):
            path = package_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("raise SystemExit(0)\n", encoding="utf-8")
            if relative.startswith("scripts/"):
                path.chmod(0o755)

        for relative in (
            "deploy/watch-assistant.service",
            "scripts/deploy_systemd_release.sh",
            "scripts/systemd_release_prepare.py",
            "scripts/systemd_release_update.py",
            "scripts/postdeploy_release_check.py",
            "scripts/systemd_unit.py",
            "scripts/systemd_backup.py",
        ):
            path = package_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / relative).read_bytes())
            if relative.startswith("scripts/"):
                path.chmod(0o755)

        helper = package_root / "scripts" / "release_manifest.py"
        helper.write_bytes((ROOT / "scripts" / "release_manifest.py").read_bytes())
        helper.chmod(0o755)
        manifest = {
            "schema_version": 2,
            "commit": commit,
            "short_commit": short_commit,
            "source_sha256": source_sha256,
            "frontend_sha256": frontend_sha256,
            "unit_sha256": hashlib.sha256(
                (package_root / "deploy/watch-assistant.service").read_bytes()
            ).hexdigest(),
            "build_time": "2026-08-02T00:00:00Z",
            "branch": "codex/publish-main",
        }
        if tampered_file == "helper":
            helper.write_text("# tampered helper\n", encoding="utf-8")
        else:
            manifest["source_sha256"] = "0" * 64
        (package_root / "release-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

        package_file = tmp_path / (
            f"watch-assistant-{short_commit}-20260802-0000.tar.gz"
        )
        with tarfile.open(package_file, "w:gz") as archive:
            archive.add(package_root, arcname=f"watch-assistant-{short_commit}")

        result = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts" / "verify_release_artifact.sh"),
                str(package_file),
                commit,
                str(tmp_path / f"output-{tampered_file}"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "release artifact refused" in result.stderr


def test_compose_and_docker_accept_local_or_git_sha_release():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    # Compose defaults to "local" so non-git downloads can still start.
    assert "WATCH_ASSISTANT_RELEASE:-local" in compose
    # The Dockerfile still refuses arbitrary strings: only "local" or git SHAs.
    assert "^(local|[0-9a-fA-F]{7}|[0-9a-fA-F]{40})$" in dockerfile
    assert "ARG WATCH_ASSISTANT_RELEASE" in dockerfile
    assert "WATCH_ASSISTANT_RELEASE=unknown" not in compose


def test_systemd_release_update_rejects_invalid_version_without_mutation(tmp_path: Path):
    version_file = tmp_path / "VERSION"
    release_env = tmp_path / "release.env"
    stale_drop_in = tmp_path / "release.conf"
    version_file.write_text("commit=not-a-release\n", encoding="utf-8")
    release_env.write_text("WATCH_ASSISTANT_RELEASE=abcdef1\n", encoding="utf-8")
    stale_drop_in.write_text("stale\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid_release_version"):
        systemd_release.update_release_metadata(
            version_file=version_file,
            release_env=release_env,
            stale_drop_in=stale_drop_in,
        )

    assert release_env.read_text(encoding="utf-8") == "WATCH_ASSISTANT_RELEASE=abcdef1\n"
    assert stale_drop_in.exists()


def test_postdeploy_check_requires_matching_version_systemd_and_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    current_root = tmp_path / "current"
    version_file = current_root / "VERSION"
    release_env = tmp_path / "state" / "release.env"
    stale_drop_in = tmp_path / "release.conf"
    _write_version(version_file)
    release_env.parent.mkdir()
    release_env.write_text(
        "WATCH_ASSISTANT_RELEASE=abcdef1\nOTHER_SECRET=must-not-be-read-as-release\n",
        encoding="utf-8",
    )

    calls = _mock_systemctl(monkeypatch, current_root, release_env)
    monkeypatch.setattr(
        postdeploy_release,
        "_read_health",
        lambda _url, _timeout: "abcdef1",
    )

    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        stale_drop_in=stale_drop_in,
        release_env=release_env,
        current_root=current_root,
    ) == (True, "ok")
    assert "--property=Environment" not in calls[0]
    assert "--property=EnvironmentFiles" in calls[0]
    assert "must-not-be-read-as-release" not in capsys.readouterr().out


def test_postdeploy_check_fails_closed_when_health_does_not_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    current_root = tmp_path / "current"
    version_file = current_root / "VERSION"
    release_env = tmp_path / "release.env"
    _write_version(version_file)
    release_env.write_text("WATCH_ASSISTANT_RELEASE=abcdef1\n", encoding="utf-8")
    _mock_systemctl(monkeypatch, current_root, release_env)
    monkeypatch.setattr(postdeploy_release, "_read_health", lambda *_args: "0123456")

    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
        health_timeout=0.01,
        health_poll_interval=0.005,
    ) == (False, "release_mismatch")


def test_postdeploy_check_fails_when_release_env_is_not_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    current_root = tmp_path / "current"
    version_file = current_root / "VERSION"
    release_env = tmp_path / "release.env"
    _write_version(version_file)
    release_env.write_text("WATCH_ASSISTANT_RELEASE=abcdef1\n", encoding="utf-8")
    monkeypatch.setattr(
        postdeploy_release.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout="EnvironmentFiles=/etc/watch-assistant.env\n", stderr=""
        ),
    )

    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
    ) == (False, "release_env_not_loaded")


def test_postdeploy_check_requires_ok_health_status_and_safe_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    commit = "a" * 40
    current_root = tmp_path / "current"
    version_file = current_root / "VERSION"
    release_env = tmp_path / "release.env"
    _write_version(version_file, commit)
    release_env.write_text(f"WATCH_ASSISTANT_RELEASE={commit}\n", encoding="utf-8")
    _mock_systemctl(monkeypatch, current_root, release_env)
    monkeypatch.setattr(postdeploy_release, "_read_health", lambda *_args: commit)
    monkeypatch.setattr(
        postdeploy_release,
        "_read_diagnostics",
        lambda *_args: "database_integrity_not_supported",
    )

    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
        expected_release=commit,
        diagnostics_url="http://fixture.invalid/diagnostics",
        diagnostics_token="fixture-token",
    ) == (False, "database_integrity_not_supported")

    monkeypatch.setattr(postdeploy_release, "_read_diagnostics", lambda *_args: "ok")
    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
        expected_release=commit,
        diagnostics_url="http://fixture.invalid/diagnostics",
        diagnostics_token="fixture-token",
    ) == (True, "ok")


def test_read_health_rejects_non_ok_status_without_exposing_payload(
    monkeypatch: pytest.MonkeyPatch,
):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"status": "degraded", "release": "a" * 40}).encode()

        def getcode(self):
            return 200

    monkeypatch.setattr(postdeploy_release, "urlopen", lambda *_args, **_kwargs: _Response())
    assert postdeploy_release._read_health("http://fixture.invalid/health", 1.0) is None


def test_postdeploy_health_polls_until_expected_release(
    monkeypatch: pytest.MonkeyPatch,
):
    responses = iter((None, "0123456", "abcdef1"))
    calls: list[tuple[str, float]] = []

    def read_health(url: str, timeout: float) -> str | None:
        calls.append((url, timeout))
        return next(responses)

    monkeypatch.setattr(postdeploy_release, "_read_health", read_health)
    monkeypatch.setattr(postdeploy_release.time, "sleep", lambda _seconds: None)

    assert postdeploy_release._wait_for_health(
        "http://fixture.invalid/health",
        5.0,
        "abcdef1",
        0.1,
        0.01,
    ) == ("abcdef1", True)
    assert len(calls) == 3
    assert all(timeout <= 0.1 for _, timeout in calls)


def test_postdeploy_health_polling_has_a_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = 0

    def read_health(_url: str, _timeout: float) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(postdeploy_release, "_read_health", read_health)
    monkeypatch.setattr(postdeploy_release.time, "sleep", lambda _seconds: None)

    assert postdeploy_release._wait_for_health(
        "http://fixture.invalid/health",
        5.0,
        "abcdef1",
        0.02,
        0.01,
    ) == (None, False)
    assert 1 <= calls <= 4


def test_postdeploy_rejects_unbounded_health_polling_configuration():
    assert postdeploy_release.check_release_consistency(
        version_file=Path("/missing/VERSION"),
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        health_timeout=float("inf"),
    ) == (False, "health_polling_invalid")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink replacement is required")
def test_systemd_release_switch_records_previous_metadata_and_rolls_back(
    tmp_path: Path,
):
    old_commit = "a" * 40
    new_commit = "b" * 40
    releases_root = tmp_path / "releases"
    old_root = releases_root / "watch-assistant-old"
    new_root = releases_root / "watch-assistant-new"
    old_root.mkdir(parents=True)
    new_root.mkdir()
    _write_version(old_root / "VERSION", old_commit)
    _write_version(new_root / "VERSION", new_commit)
    _write_manifest(new_root / "release-manifest.json", new_commit)
    current_root = tmp_path / "current"
    current_root.symlink_to(old_root, target_is_directory=True)
    release_env = tmp_path / "state" / "release.env"
    release_env.parent.mkdir()
    release_env.write_text(f"WATCH_ASSISTANT_RELEASE={old_commit}\n", encoding="utf-8")
    state_file = tmp_path / "state" / "rollback.json"

    state = systemd_release.switch_release(
        release_root=new_root,
        expected_release=new_commit,
        current_root=current_root,
        release_env=release_env,
        state_file=state_file,
        allowed_releases_root=releases_root,
    )
    assert state["previous_release"] == old_commit
    assert current_root.resolve() == new_root.resolve()
    assert release_env.read_text(encoding="utf-8") == (
        f"WATCH_ASSISTANT_RELEASE={new_commit}\n"
    )
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["previous_current_target"] == str(old_root.resolve())
    assert saved["target_release"] == new_commit

    state_file.write_text(
        json.dumps(
            {
                key: value
                for key, value in saved.items()
                if key != "target_manifest_sha256"
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="rollback_state_invalid"):
        systemd_release.rollback_release(
            current_root=current_root,
            release_env=release_env,
            state_file=state_file,
            allowed_releases_root=releases_root,
        )
    state_file.write_text(json.dumps(saved), encoding="utf-8")

    manifest_path = new_root / "release-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="rollback_target_manifest_changed"):
        systemd_release.rollback_release(
            current_root=current_root,
            release_env=release_env,
            state_file=state_file,
            allowed_releases_root=releases_root,
        )
    manifest_path.write_bytes(manifest_bytes)

    systemd_release.rollback_release(
        current_root=current_root,
        release_env=release_env,
        state_file=state_file,
        allowed_releases_root=releases_root,
    )
    assert current_root.resolve() == old_root.resolve()
    assert release_env.read_text(encoding="utf-8") == (
        f"WATCH_ASSISTANT_RELEASE={old_commit}\n"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink replacement is required")
def test_systemd_release_switch_rejects_manifest_identity_mismatch(
    tmp_path: Path,
):
    old_commit = "a" * 40
    new_commit = "b" * 40
    releases_root = tmp_path / "releases"
    old_root = releases_root / "watch-assistant-old"
    new_root = releases_root / "watch-assistant-new"
    old_root.mkdir(parents=True)
    new_root.mkdir()
    _write_version(old_root / "VERSION", old_commit)
    _write_version(new_root / "VERSION", new_commit)
    _write_manifest(new_root / "release-manifest.json", "c" * 40)
    current_root = tmp_path / "current"
    current_root.symlink_to(old_root, target_is_directory=True)
    release_env = tmp_path / "state" / "release.env"
    release_env.parent.mkdir()
    release_env.write_text(f"WATCH_ASSISTANT_RELEASE={old_commit}\n", encoding="utf-8")
    state_file = tmp_path / "state" / "rollback.json"

    with pytest.raises(ValueError, match="release_manifest_commit_mismatch"):
        systemd_release.switch_release(
            release_root=new_root,
            expected_release=new_commit,
            current_root=current_root,
            release_env=release_env,
            state_file=state_file,
            allowed_releases_root=releases_root,
        )

    assert current_root.resolve() == old_root.resolve()
    assert release_env.read_text(encoding="utf-8") == (
        f"WATCH_ASSISTANT_RELEASE={old_commit}\n"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink replacement is required")
def test_systemd_release_rollback_restores_legacy_short_release_metadata(
    tmp_path: Path,
):
    old_commit = "abcdef1"
    new_commit = "b" * 40
    releases_root = tmp_path / "releases"
    old_root = releases_root / "watch-assistant-old"
    new_root = releases_root / "watch-assistant-new"
    old_root.mkdir(parents=True)
    new_root.mkdir()
    _write_version(old_root / "VERSION", old_commit)
    _write_version(new_root / "VERSION", new_commit)
    _write_manifest(new_root / "release-manifest.json", new_commit)
    current_root = tmp_path / "current"
    current_root.symlink_to(old_root, target_is_directory=True)
    release_env = tmp_path / "state" / "release.env"
    release_env.parent.mkdir()
    release_env.write_text(f"WATCH_ASSISTANT_RELEASE={old_commit}\n", encoding="utf-8")
    state_file = tmp_path / "state" / "rollback.json"

    systemd_release.switch_release(
        release_root=new_root,
        expected_release=new_commit,
        current_root=current_root,
        release_env=release_env,
        state_file=state_file,
        allowed_releases_root=releases_root,
    )
    systemd_release.rollback_release(
        current_root=current_root,
        release_env=release_env,
        state_file=state_file,
        allowed_releases_root=releases_root,
    )

    assert current_root.resolve() == old_root.resolve()
    assert release_env.read_text(encoding="utf-8") == (
        f"WATCH_ASSISTANT_RELEASE={old_commit}\n"
    )


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (None, "release_env_missing"),
        ("OTHER_SECRET=do-not-report\n", "release_env_invalid"),
        ("WATCH_ASSISTANT_RELEASE=not-a-release\n", "release_env_invalid"),
    ],
)
def test_postdeploy_check_fails_closed_on_missing_or_invalid_release_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str | None,
    expected_code: str,
):
    current_root = tmp_path / "current"
    version_file = current_root / "VERSION"
    release_env = tmp_path / "release.env"
    _write_version(version_file)
    if content is not None:
        release_env.write_text(content, encoding="utf-8")
    _mock_systemctl(monkeypatch, current_root, release_env)

    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
    ) == (False, expected_code)


def test_postdeploy_check_fails_closed_on_current_version_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    current_root = tmp_path / "current"
    version_file = tmp_path / "staged" / "VERSION"
    release_env = tmp_path / "release.env"
    _write_version(version_file, "abcdef1")
    _write_version(current_root / "VERSION", "0123456")
    release_env.write_text("WATCH_ASSISTANT_RELEASE=abcdef1\n", encoding="utf-8")

    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
    ) == (False, "current_release_mismatch")


def test_postdeploy_check_fails_closed_on_service_path_or_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    current_root = tmp_path / "current"
    version_file = current_root / "VERSION"
    release_env = tmp_path / "release.env"
    _write_version(version_file)
    release_env.write_text("WATCH_ASSISTANT_RELEASE=abcdef1\n", encoding="utf-8")
    monkeypatch.setattr(
        postdeploy_release.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                f"EnvironmentFiles={release_env}\n"
                "MainPID=0\n"
                f"WorkingDirectory={tmp_path / 'old-current'}\n"
            ),
            stderr="",
        ),
    )

    assert postdeploy_release.check_release_consistency(
        version_file=version_file,
        unit="watch-assistant.service",
        health_url="http://fixture.invalid/health",
        release_env=release_env,
        current_root=current_root,
    ) == (False, "service_not_running")

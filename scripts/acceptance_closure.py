#!/usr/bin/env python3
"""Run the bounded Watch Assistant acceptance closure.

The default live path is deliberately split into a read-only preview and an
explicit fixture execution.  Production inventory and organization planning
must succeed before any fixture write is considered.  Runner output is parsed
as public JSON and only redacted stage summaries are persisted.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXIT_SUCCESS = 0
EXIT_BLOCKED = 2
EXIT_UNCERTAIN = 3
DEFAULT_TIMEOUT_SECONDS = 30 * 60
PYTHON_ENV_MARKERS = (Path(".venv") / "bin", Path(".venv") / "Scripts")
PRODUCTION_ACCEPTANCE_ENV = "WATCH_ASSISTANT_PRODUCTION_ACCEPTANCE"
PRODUCTION_PYTHON_ENV = "WATCH_ASSISTANT_PRODUCTION_PYTHON"
C03_GATES = (
    "WATCH_ASSISTANT_P115_C03_WRITE",
    "WATCH_ASSISTANT_P115_C03_MANAGED_FIXTURE",
    "WATCH_ASSISTANT_P115_C03_CLEANUP_PLAN",
    "WATCH_ASSISTANT_P115_C03_LIVE",
)


class ClosureInputError(ValueError):
    """Raised for a preflight failure that must not start a subprocess."""


@dataclass(frozen=True)
class RuntimeSelection:
    """The selected interpreter and runtime contract for child stages."""

    python_executable: str
    environment: dict[str, str]
    audit: dict[str, str]


def _stable_id(value: object) -> bool:
    return isinstance(value, str) and value.isdigit() and not value.startswith("0")


def _require_stable_id(value: str | None, name: str) -> str:
    if not _stable_id(value):
        raise ClosureInputError(f"{name}_must_be_stable_decimal_id")
    return value


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _python_is_worktree_venv(root: Path) -> bool:
    # Keep the venv entry path instead of resolving a valid symlink to a
    # shared runtime outside the checkout.
    executable = Path(sys.executable)
    if not executable.is_absolute():
        executable = Path.cwd() / executable
    try:
        relative = executable.relative_to(root)
    except ValueError:
        return False
    return any(relative.parts[:2] == marker.parts for marker in PYTHON_ENV_MARKERS)


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _release_source_path(root: Path) -> Path:
    source = root / "src"
    if (
        source.is_symlink()
        or not source.is_dir()
        or not (source / "watch_assistant").is_dir()
    ):
        raise ClosureInputError("release_source_missing")
    return source.resolve()


def _validate_production_python(value: Path | None, root: Path) -> Path:
    if value is None:
        raise ClosureInputError("production_python_required")
    if not value.is_absolute():
        raise ClosureInputError("production_python_must_be_absolute")

    candidate = _absolute_path(value)
    release_root = _absolute_path(root)
    if _path_is_within(candidate, release_root) or _path_is_within(
        candidate.resolve(), release_root.resolve()
    ):
        raise ClosureInputError("production_python_must_be_external")
    if not candidate.is_file():
        raise ClosureInputError("production_python_file_missing")
    if not os.access(candidate, os.X_OK):
        raise ClosureInputError("production_python_not_executable")
    return candidate


def _json_dump(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n"


def _native_library_path(environment: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if environment is None else environment
    configured = source.get("WATCH_ASSISTANT_NATIVE_LIBRARY_PATH")
    if configured:
        return configured
    if sys.platform != "darwin":
        return None

    base_executable = Path(getattr(sys, "_base_executable", sys.executable))
    if not base_executable.is_absolute():
        base_executable = Path.cwd() / base_executable
    runtime_root = (base_executable.parent / "../..").resolve()
    candidates = (
        Path("/opt/homebrew/opt/openssl@3/lib"),
        Path("/usr/local/opt/openssl@3/lib"),
        runtime_root / "native/poppler/poppler/lib",
    )
    for candidate in candidates:
        if (candidate / "libssl.3.dylib").is_file() and (
            candidate / "libcrypto.3.dylib"
        ).is_file():
            return str(candidate)
    return None


def _runtime_environment(
    root: Path | None = None,
    *,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(os.environ if base_environment is None else base_environment)
    native_library_path = _native_library_path(environment)
    if native_library_path:
        environment["WATCH_ASSISTANT_NATIVE_LIBRARY_PATH"] = native_library_path
        environment["DYLD_FALLBACK_LIBRARY_PATH"] = (
            f"{native_library_path}"
            f"{os.pathsep}{environment['DYLD_FALLBACK_LIBRARY_PATH']}"
            if environment.get("DYLD_FALLBACK_LIBRARY_PATH")
            else native_library_path
        )
    if root is not None:
        # Source must win over an editable or stale package in an external venv.
        environment["PYTHONPATH"] = str(_release_source_path(root))
    return environment


def _production_authorization_source(
    args: argparse.Namespace, environment: Mapping[str, str]
) -> str | None:
    sources: list[str] = []
    if getattr(args, "production_acceptance", False):
        sources.append("cli")
    if environment.get(PRODUCTION_ACCEPTANCE_ENV) == "1":
        sources.append("environment")
    return "+".join(sources) or None


def _production_python_input(
    args: argparse.Namespace, environment: Mapping[str, str]
) -> tuple[Path | None, str | None]:
    raw_cli_value = getattr(args, "production_python", None)
    cli_value = None if raw_cli_value is None else Path(raw_cli_value)
    environment_value = environment.get(PRODUCTION_PYTHON_ENV)
    if cli_value is not None and environment_value:
        if _absolute_path(cli_value) != _absolute_path(Path(environment_value)):
            raise ClosureInputError("production_python_conflict")
    if cli_value is not None:
        return cli_value, "cli"
    if environment_value:
        return Path(environment_value), "environment"
    return None, None


def _select_runtime(
    args: argparse.Namespace,
    *,
    root: Path,
    base_environment: Mapping[str, str] | None = None,
) -> RuntimeSelection:
    environment = dict(os.environ if base_environment is None else base_environment)
    authorization_source = _production_authorization_source(args, environment)
    production_python, python_source = _production_python_input(args, environment)
    production_requested = authorization_source is not None or production_python is not None

    if production_requested:
        if authorization_source is None:
            raise ClosureInputError("production_acceptance_required")
        executable = _validate_production_python(production_python, root)
        return RuntimeSelection(
            python_executable=str(executable),
            environment=_runtime_environment(root, base_environment=environment),
            audit={
                "mode": "production_external",
                "authorization_source": authorization_source,
                "python_source": python_source or "unknown",
                "pythonpath_scope": "release_src",
            },
        )

    if not _python_is_worktree_venv(root):
        raise ClosureInputError("worktree_venv_required")
    return RuntimeSelection(
        python_executable=sys.executable,
        environment=_runtime_environment(root, base_environment=environment),
        audit={
            "mode": "worktree",
            "authorization_source": "default_worktree_venv",
            "python_source": "process",
            "pythonpath_scope": "release_src",
        },
    )


def _read_public_json(stdout: str) -> dict[str, Any] | None:
    try:
        value = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _stage_success(stage: str, result: Mapping[str, Any]) -> bool:
    if stage == "inventory":
        return result.get("complete") is True and result.get("root_identity_verified") is True
    if stage == "organization_plan":
        return (
            result.get("status") == "success"
            and result.get("write_started") is False
            and result.get("remote_write_calls") == 0
        )
    if stage == "fixture":
        return (
            result.get("status") == "success"
            and result.get("cleanup") == "complete"
            and isinstance(result.get("write_calls"), int)
            and result.get("write_calls", 0) > 0
        )
    if stage == "strm":
        return (
            result.get("status") == "success"
            and result.get("output_root_is_temporary") is True
            and result.get("permanent_delete_used") is False
        )
    if stage == "strm_and_plan_contracts":
        return result.get("return_code") == 0
    return False


def _stage_status(
    stage: str,
    result: Mapping[str, Any] | None,
    return_code: int,
    *,
    write_stage: bool,
) -> str:
    if result is not None and result.get("status") == "uncertain":
        return "uncertain"
    if write_stage and result is None:
        # A write-capable child may have changed the remote before it failed
        # to emit its public report.  Do not make an unknown outcome look
        # retryable.
        return "uncertain"
    if return_code == 0 and result is not None and _stage_success(stage, result):
        return "success"
    if write_stage and result is not None and result.get("write_started") is True:
        return "uncertain"
    return "blocked"


def _write_stage(output_dir: Path, stage: str, report: Mapping[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{stage}.json"
    path.write_text(_json_dump(report), encoding="utf-8")


def _run_stage(
    *,
    root: Path,
    output_dir: Path,
    stage: str,
    command: Sequence[str],
    environment: Mapping[str, str],
    timeout_seconds: float,
    write_stage: bool,
    expect_json: bool = True,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            list(command),
            cwd=root,
            env=dict(environment),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        report = {
            "stage": stage,
            "status": "uncertain" if write_stage else "blocked",
            "error_code": "stage_timeout",
            "write_started": write_stage,
        }
        _write_stage(output_dir, stage, report)
        return report
    result = _read_public_json(completed.stdout) if expect_json else None
    if not expect_json and completed.returncode == 0:
        result = {"return_code": 0}
    status = _stage_status(stage, result, completed.returncode, write_stage=write_stage)
    report: dict[str, Any] = {
        "stage": stage,
        "status": status,
        "return_code": completed.returncode,
        "write_started": bool(result and result.get("write_started") is True),
    }
    if result is None:
        report["error_code"] = "runner_output_invalid"
    else:
        report["result"] = result
        if status != "success":
            report["error_code"] = result.get("error_code") or "stage_failed"
    _write_stage(output_dir, stage, report)
    return report


def _summary(
    output_dir: Path,
    reports: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    runtime: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    statuses = [str(report.get("status")) for report in reports]
    if "uncertain" in statuses:
        status = "uncertain"
    elif any(value != "success" for value in statuses):
        status = "blocked"
    else:
        status = "success"
    summary = {
        "mode": mode,
        "status": status,
        "stages": list(reports),
        "production_write_started": False,
        "permanent_delete_used": False,
        "evidence_dir": str(output_dir),
    }
    if runtime is not None:
        summary["runtime"] = dict(runtime)
    (output_dir / "SUMMARY.json").write_text(_json_dump(summary), encoding="utf-8")
    return summary


def _base_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the bounded inventory, organization, fixture, and STRM closure"
    )
    parser.add_argument(
        "mode", choices=("preview", "execute", "offline", "dry-run"), help="closure phase"
    )
    parser.add_argument("--root-id", help="production root directory ID")
    parser.add_argument("--source-id", help="production source directory ID")
    parser.add_argument("--target-id", help="production target directory ID")
    parser.add_argument("--cookie-path", type=Path)
    parser.add_argument("--managed-scope-path", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--live-read", action="store_true")
    parser.add_argument("--confirm-fixture", action="store_true")
    parser.add_argument("--fixture-parent-id")
    parser.add_argument("--fixture-authorization-path", type=Path)
    parser.add_argument("--strm-root-id")
    parser.add_argument("--strm-file-id")
    parser.add_argument("--strm-rename-authorization", type=Path)
    parser.add_argument("--strm-restore-authorization", type=Path)
    parser.add_argument(
        "--production-acceptance",
        action="store_true",
        help="explicitly authorize use of an external production Python runtime",
    )
    parser.add_argument(
        "--production-python",
        type=Path,
        help="absolute external Python executable for production acceptance",
    )
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def _output_dir(value: Path | None) -> Path:
    if value is not None:
        return value.expanduser().resolve()
    return Path(tempfile.mkdtemp(prefix="watch-assistant-acceptance-"))


def _require_file(path: Path | None, name: str) -> Path:
    if path is None or not path.is_file():
        raise ClosureInputError(f"{name}_file_missing")
    return path


def _validate_timeout(value: float) -> float:
    if value <= 0 or value > DEFAULT_TIMEOUT_SECONDS:
        raise ClosureInputError("timeout_out_of_range")
    return value


def _validate_preview_args(args: argparse.Namespace) -> None:
    _require_stable_id(args.root_id, "root_id")
    _require_stable_id(args.source_id, "source_id")
    _require_stable_id(args.target_id, "target_id")
    if args.source_id == args.target_id:
        raise ClosureInputError("source_target_must_differ")
    _require_file(args.cookie_path, "cookie")
    _require_file(args.managed_scope_path, "managed_scope")
    if not args.live_read:
        raise ClosureInputError("live_read_confirmation_required")


def _preview(
    args: argparse.Namespace,
    *,
    root: Path,
    output_dir: Path,
    environment: Mapping[str, str],
    python_executable: str | None = None,
) -> list[dict[str, Any]]:
    _validate_preview_args(args)
    python = sys.executable if python_executable is None else python_executable
    inventory = _run_stage(
        root=root,
        output_dir=output_dir,
        stage="inventory",
        command=(
            python,
            "scripts/p115_production_inventory.py",
            "--root-id",
            args.root_id,
            "--cookie-path",
            str(args.cookie_path),
            "--output",
            str(output_dir / "inventory-result.json"),
        ),
        environment=environment,
        timeout_seconds=args.timeout_seconds,
        write_stage=False,
    )
    reports = [inventory]
    if inventory["status"] != "success":
        return reports
    organization = _run_stage(
        root=root,
        output_dir=output_dir,
        stage="organization_plan",
        command=(
            python,
            "scripts/p115_organization_application_live_runner.py",
            "--source-id",
            args.source_id,
            "--target-id",
            args.target_id,
            "--cookie-path",
            str(args.cookie_path),
            "--managed-scope-path",
            str(args.managed_scope_path),
            "--live",
        ),
        environment=environment,
        timeout_seconds=args.timeout_seconds,
        write_stage=False,
    )
    reports.append(organization)
    return reports


def _validate_execute_args(args: argparse.Namespace) -> None:
    if not args.confirm_fixture:
        raise ClosureInputError("fixture_confirmation_required")
    _validate_preview_args(args)
    fixture_parent = _require_stable_id(args.fixture_parent_id, "fixture_parent_id")
    strm_root = _require_stable_id(args.strm_root_id, "strm_root_id")
    _require_stable_id(args.strm_file_id, "strm_file_id")
    if fixture_parent == args.root_id or strm_root == args.root_id:
        raise ClosureInputError("fixture_scope_must_differ_from_production_root")
    _require_file(args.fixture_authorization_path, "fixture_authorization")
    _require_file(args.strm_rename_authorization, "strm_rename_authorization")
    _require_file(args.strm_restore_authorization, "strm_restore_authorization")


def _execute(
    args: argparse.Namespace,
    *,
    root: Path,
    output_dir: Path,
    environment: Mapping[str, str],
    python_executable: str | None = None,
) -> list[dict[str, Any]]:
    _validate_execute_args(args)
    python = sys.executable if python_executable is None else python_executable
    reports = _preview(
        args,
        root=root,
        output_dir=output_dir,
        environment=environment,
        python_executable=python,
    )
    if any(report["status"] != "success" for report in reports):
        return reports
    missing_gate = next((name for name in C03_GATES if environment.get(name) != "1"), None)
    if missing_gate is not None:
        report = {
            "stage": "fixture",
            "status": "blocked",
            "error_code": "fixture_gate_closed",
            "write_started": False,
        }
        _write_stage(output_dir, "fixture", report)
        reports.append(report)
        return reports
    fixture = _run_stage(
        root=root,
        output_dir=output_dir,
        stage="fixture",
        command=(
            python,
            "scripts/p115_c03_live_runner.py",
            "--parent-id",
            args.fixture_parent_id,
            "--cookie-path",
            str(args.cookie_path),
            "--authorization-path",
            str(args.fixture_authorization_path),
            "--managed-scope-path",
            str(args.managed_scope_path),
            "--live",
        ),
        environment=environment,
        timeout_seconds=args.timeout_seconds,
        write_stage=True,
    )
    reports.append(fixture)
    if fixture["status"] != "success":
        return reports
    strm = _run_stage(
        root=root,
        output_dir=output_dir,
        stage="strm",
        command=(
            python,
            "scripts/p115_strm_application_live_runner.py",
            "--root-id",
            args.strm_root_id,
            "--file-id",
            args.strm_file_id,
            "--cookie-path",
            str(args.cookie_path),
            "--rename-authorization",
            str(args.strm_rename_authorization),
            "--restore-authorization",
            str(args.strm_restore_authorization),
            "--managed-scope-path",
            str(args.managed_scope_path),
            "--live",
            "--output",
            str(output_dir / "strm-result.json"),
        ),
        environment=environment,
        timeout_seconds=args.timeout_seconds,
        write_stage=True,
    )
    reports.append(strm)
    return reports


def _offline(
    args: argparse.Namespace,
    *,
    root: Path,
    output_dir: Path,
    environment: Mapping[str, str],
    python_executable: str | None = None,
) -> list[dict[str, Any]]:
    if not args.confirm_fixture:
        raise ClosureInputError("fixture_confirmation_required")
    python = sys.executable if python_executable is None else python_executable
    fixture_parent = _require_stable_id(args.fixture_parent_id or "7000", "fixture_parent_id")
    offline_env = dict(environment)
    for name in C03_GATES[:3]:
        offline_env[name] = "1"
    fixture = _run_stage(
        root=root,
        output_dir=output_dir,
        stage="fixture",
        command=(
            python,
            "scripts/p115_c03_fixture_probe.py",
            "--parent-id",
            fixture_parent,
            "--offline-fixture",
            "success",
        ),
        environment=offline_env,
        timeout_seconds=args.timeout_seconds,
        write_stage=True,
    )
    if fixture["status"] != "success":
        return [fixture]
    tests = _run_stage(
        root=root,
        output_dir=output_dir,
        stage="strm_and_plan_contracts",
        command=(
            python,
            "-m",
            "pytest",
            "-q",
            "tests/integration/test_organization_preview_api.py",
            "tests/integration/test_strm_operations_api.py",
        ),
        environment=offline_env,
        timeout_seconds=args.timeout_seconds,
        write_stage=False,
        expect_json=False,
    )
    return [fixture, tests]


def _dry_run(args: argparse.Namespace, output_dir: Path) -> dict[str, Any]:
    stages = [
        {"stage": "inventory", "write_capability": "none"},
        {"stage": "organization_plan", "write_capability": "none"},
    ]
    if args.mode == "execute":
        stages.extend(
            [
                {"stage": "fixture", "write_capability": "scoped_and_one_shot"},
                {"stage": "strm", "write_capability": "scoped_and_reversible"},
            ]
        )
    summary = {
        "mode": args.mode,
        "status": "dry_run",
        "stages": stages,
        "production_write_started": False,
        "permanent_delete_used": False,
        "evidence_dir": str(output_dir),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "SUMMARY.json").write_text(_json_dump(summary), encoding="utf-8")
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = _base_parser()
    args = parser.parse_args(argv)
    root = _root()
    output_dir = _output_dir(args.output_dir)
    try:
        args.timeout_seconds = _validate_timeout(args.timeout_seconds)
        if args.mode == "dry-run":
            summary = _dry_run(args, output_dir)
        else:
            runtime = _select_runtime(args, root=root)
            if args.mode == "preview":
                reports = _preview(
                    args,
                    root=root,
                    output_dir=output_dir,
                    environment=runtime.environment,
                    python_executable=runtime.python_executable,
                )
            elif args.mode == "execute":
                reports = _execute(
                    args,
                    root=root,
                    output_dir=output_dir,
                    environment=runtime.environment,
                    python_executable=runtime.python_executable,
                )
            else:
                reports = _offline(
                    args,
                    root=root,
                    output_dir=output_dir,
                    environment=runtime.environment,
                    python_executable=runtime.python_executable,
                )
            summary = _summary(output_dir, reports, mode=args.mode, runtime=runtime.audit)
    except ClosureInputError as error:
        summary = {
            "mode": args.mode,
            "status": "blocked",
            "error_code": str(error),
            "stages": [],
            "production_write_started": False,
            "permanent_delete_used": False,
            "evidence_dir": str(output_dir),
            "runtime": {"mode": "unselected"},
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "SUMMARY.json").write_text(_json_dump(summary), encoding="utf-8")
    print(_json_dump(summary), end="")
    if summary["status"] in {"success", "dry_run"}:
        return EXIT_SUCCESS
    if summary["status"] == "uncertain":
        return EXIT_UNCERTAIN
    return EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())

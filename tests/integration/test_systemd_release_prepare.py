import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_prepare_script():
    path = ROOT / "scripts" / "systemd_release_prepare.py"
    spec = importlib.util.spec_from_file_location("systemd_release_prepare_integration", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prepare_cli_reports_stable_machine_code_and_chinese_error(
    tmp_path: Path, capsys
):
    prepare = _load_prepare_script()
    allowed_root = tmp_path / "releases"
    release_root = allowed_root / "release"
    release_root.mkdir(parents=True)
    release_root.chmod(0o700)

    original_allowed_root = prepare.DEFAULT_RELEASES_ROOT
    prepare.DEFAULT_RELEASES_ROOT = allowed_root
    try:
        result = prepare.main(
            [
                "--release-root",
                str(release_root),
                "--expected-release",
                "abcdef1",
            ]
        )
    finally:
        prepare.DEFAULT_RELEASES_ROOT = original_allowed_root

    output = capsys.readouterr().out
    assert result == 1
    assert "SYSTEMD_RELEASE_PREPARE_CODE=required_file_missing" in output
    assert "systemd 发布预检失败" in output
    assert str(release_root) not in output

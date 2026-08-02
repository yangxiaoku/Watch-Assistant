import subprocess
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).parents[2] / "deploy"


def test_qbittorrent_unit_keeps_startup_dependency_one_way():
    qbittorrent_unit = (DEPLOY_DIR / "watch-assistant-qbittorrent.service").read_text(
        encoding="utf-8"
    )
    app_override = (DEPLOY_DIR / "watch-assistant-inspection.override.conf").read_text(
        encoding="utf-8"
    )

    assert "PartOf=watch-assistant.service" not in qbittorrent_unit
    assert "Before=watch-assistant.service" in qbittorrent_unit
    assert "Requires=watch-assistant-qbittorrent.service" in app_override
    assert "After=watch-assistant-qbittorrent.service" in app_override


def test_watch_assistant_unit_uses_writable_home_for_p115_cache():
    unit = (DEPLOY_DIR / "watch-assistant.service").read_text(encoding="utf-8")

    assert "Environment=HOME=/var/lib/watch-assistant" in {
        line.strip() for line in unit.splitlines()
    }
    assert "StateDirectory=watch-assistant" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=true" in unit
    assert "UMask=0077" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ReadWritePaths=/opt" not in unit


def test_release_startup_smoke_is_tracked_as_executable():
    result = subprocess.run(
        ["git", "ls-files", "--stage", "scripts/release_startup_smoke.py"],
        cwd=DEPLOY_DIR.parent,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("git metadata is unavailable")

    assert result.stdout.split(maxsplit=1)[0] == "100755"


def test_systemd_release_deploy_runs_prepare_before_metadata_update():
    script = (DEPLOY_DIR.parent / "scripts" / "deploy_systemd_release.sh").read_text(
        encoding="utf-8"
    )

    prepare_position = script.index("systemd_release_prepare.py")
    update_position = script.index("systemd_release_update.py")
    assert prepare_position < update_position
    assert "--expected-release \"$EXPECTED_RELEASE\"" in script
    assert "--allowed-releases-root \"$RELEASES_ROOT\"" in script
    assert "--service-user \"$SERVICE_USER\"" in script

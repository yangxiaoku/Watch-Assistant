from pathlib import Path

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

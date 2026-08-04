import json
from pathlib import Path

import pytest

from scripts.p115_strm_playback_live_runner import (
    _find_fixture_manifest,
    main,
    run_acceptance,
)


class _Response:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


class _ManifestClient:
    def __init__(self, bodies):
        self.bodies = iter(bodies)
        self.paths = []

    async def get(self, path, *, headers):
        del headers
        self.paths.append(path)
        return _Response(next(self.bodies))


def test_playback_runner_is_closed_without_live_gate(tmp_path, capsys):
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({"parent_ids": ["7"]}), encoding="utf-8")

    assert (
        run_acceptance(
            root_id="7",
            file_id="8",
            cookie_path=tmp_path / "cookie",
            managed_scope_path=scope,
            environment={},
        )
        == {"status": "blocked", "error_code": "live_gate_closed", "write_started": False}
    )
    assert capsys.readouterr().out == ""


def test_playback_runner_cli_requires_explicit_live_flag(tmp_path, capsys):
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({"parent_ids": ["7"]}), encoding="utf-8")

    assert (
        main(
            [
                "--root-id",
                "7",
                "--file-id",
                "8",
                "--cookie-path",
                str(tmp_path / "cookie"),
                "--managed-scope-path",
                str(scope),
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["error_code"] == "live_gate_closed"


def test_playback_runner_keeps_redirect_and_range_status_contracts():
    source = Path(__file__).resolve().parents[2].joinpath(
        "scripts/p115_strm_playback_live_runner.py"
    ).read_text(encoding="utf-8")
    run_source = source.split("async def _run", 1)[1].split(
        "async def _login", 1
    )[0]

    assert run_source.count("await client.get(") == 3
    assert "get_redirect = await client.get(" in run_source
    assert "get_range = await client.get(" in run_source
    assert (
        "get_redirect, PLAYBACK_REDIRECT_STATUS, \"playback_get_redirect_failed\""
        in run_source
    )
    assert "get_range, PLAYBACK_RANGE_STATUS, \"playback_range_failed\"" in run_source


@pytest.mark.asyncio
async def test_playback_runner_selects_authorized_file_from_multi_file_scope():
    client = _ManifestClient(
        [
            {
                "items": [
                    {"cloud_file_id": "other-file", "manifest_id": "manifest-a"},
                    {"cloud_file_id": "fixture-file", "manifest_id": "manifest-b"},
                ],
                "page": 1,
                "page_size": 100,
                "total": 2,
                "total_pages": 1,
            }
        ]
    )

    item = await _find_fixture_manifest(client, {}, "fixture-file")

    assert item == {"cloud_file_id": "fixture-file", "manifest_id": "manifest-b"}
    assert client.paths == [
        "/api/v1/libraries/strm-playback-live/strm-manifest?page=1&page_size=100"
    ]


@pytest.mark.asyncio
async def test_playback_runner_rejects_authorized_file_outside_media_manifest():
    client = _ManifestClient(
        [
            {
                "items": [
                    {"cloud_file_id": "other-file", "manifest_id": "manifest-a"}
                ],
                "page": 1,
                "page_size": 100,
                "total": 1,
                "total_pages": 1,
            }
        ]
    )

    with pytest.raises(RuntimeError, match="fixture_file_not_media"):
        await _find_fixture_manifest(client, {}, "wav-fixture")


@pytest.mark.asyncio
async def test_playback_runner_finds_authorized_file_on_later_manifest_page():
    client = _ManifestClient(
        [
            {
                "items": [{"cloud_file_id": "other-file", "manifest_id": "manifest-a"}],
                "page": 1,
                "page_size": 100,
                "total": 101,
                "total_pages": 2,
            },
            {
                "items": [{"cloud_file_id": "fixture-file", "manifest_id": "manifest-b"}],
                "page": 2,
                "page_size": 100,
                "total": 101,
                "total_pages": 2,
            },
        ]
    )

    item = await _find_fixture_manifest(client, {}, "fixture-file")

    assert item["manifest_id"] == "manifest-b"
    assert client.paths == [
        "/api/v1/libraries/strm-playback-live/strm-manifest?page=1&page_size=100",
        "/api/v1/libraries/strm-playback-live/strm-manifest?page=2&page_size=100",
    ]

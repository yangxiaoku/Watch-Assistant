import json

import scripts.p115_strm_readonly_live_runner as runner
from scripts.acceptance_closure import _stage_success
from scripts.p115_strm_readonly_live_runner import (
    EXPECTED_P115CLIENT_VERSION,
    LIVE_ENV,
    main,
    run_acceptance,
)


class _Source:
    def __init__(self, value="synthetic-credential"):
        self.value = value
        self.calls = 0

    def load(self):
        self.calls += 1
        return self.value


class _Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def fs_files(self, payload, *, timeout_seconds):
        self.calls.append(("fs_files", dict(payload), timeout_seconds))
        return self.responses.pop(0)

    async def fs_info(self, payload, *, timeout_seconds):
        self.calls.append(("fs_info", dict(payload), timeout_seconds))
        return {
            "state": True,
            "data": {
                "file_category": "1",
                "fid": payload["fid"],
                "cid": "7001",
                "file_name": "Sensitive Episode.mkv",
                "size": "123",
                "pick_code": "SYNTHETIC_PICKCODE_SECRET",
            },
        }


def _page(records, *, offset=0, count=1):
    return {
        "state": True,
        "data": records,
        "offset": offset,
        "limit": 1,
        "count": count,
    }


def _directory():
    return {
        "fc": 0,
        "cid": "7001",
        "pid": "7000",
        "fn": "Sensitive Show",
    }


def _file(name):
    return {
        "fc": 1,
        "fid": "7101",
        "cid": "7001",
        "fn": name,
        "s": "123",
        "pick_code": "SYNTHETIC_PICKCODE_SECRET",
    }


def _responses():
    return (
        _page([_directory()]),
        _page([_file("Episode.mkv")]),
        _page([_directory()]),
        _page([_file("Episode-renamed.mkv")]),
    )


def _patch_client_version(monkeypatch):
    monkeypatch.setattr(runner, "version", lambda _: EXPECTED_P115CLIENT_VERSION)


def test_default_gate_blocks_before_cookie_or_transport_creation(monkeypatch):
    source = _Source()
    factory_calls = []

    report = run_acceptance(
        root_id="7000",
        cookie_path=None,
        credential_source=source,
        transport_factory=lambda cookie: factory_calls.append(cookie),
        environment={},
    )

    assert report["status"] == "blocked"
    assert report["error_code"] == "live_gate_closed"
    assert report["error_message"] == "只读 STRM 实时验收开关未开启。"
    assert report["fail_closed"] is True
    assert report["remote_write_calls"] == 0
    assert source.calls == 0
    assert factory_calls == []


def test_success_path_outputs_complete_scans_and_zero_remote_writes(monkeypatch):
    _patch_client_version(monkeypatch)
    source = _Source()
    transport = _Transport(_responses())

    report = run_acceptance(
        root_id="7000",
        cookie_path=None,
        credential_source=source,
        transport_factory=lambda _cookie: transport,
        environment={LIVE_ENV: "1"},
    )

    assert report["status"] == "success"
    assert report["complete"] is True
    assert report["root_identity_verified"] is True
    assert report["scope"]["recursive"] is True
    assert report["scope"]["page_size"] == 1
    assert report["initial_scan"]["complete"] is True
    assert report["incremental_scan"]["complete"] is True
    assert report["full_output"]["generated"] == 1
    assert report["incremental_output"]["generated"] == 1
    assert report["incremental_output"]["retire_removed"] is False
    assert report["remote"]["allowed_methods"] == ["fs_files", "fs_info"]
    assert report["remote_write_calls"] == 0
    assert report["write_started"] is False
    assert report["write_calls"] == 0
    assert report["output_root_is_temporary"] is True
    assert report["database_is_temporary"] is True
    assert report["remote_rename"] is False
    assert report["remote_restore"] is False
    assert report["remote_cleanup"] is False
    assert report["remote_playback"] is False
    assert _stage_success("strm_readonly", report)
    assert [call[0] for call in transport.calls] == [
        "fs_files",
        "fs_files",
        "fs_files",
        "fs_files",
    ]
    assert source.calls == 1

    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    assert "Sensitive" not in rendered
    assert "PICKCODE" not in rendered
    assert "uid" not in rendered
    assert "Episode" not in rendered


def test_incomplete_remote_scan_is_fail_closed(monkeypatch):
    _patch_client_version(monkeypatch)
    source = _Source()
    transport = _Transport(
        (
            {
                "state": True,
                "data": [_directory()],
                "offset": 0,
                "limit": 1,
                "count": 2,
            },
        )
    )

    report = run_acceptance(
        root_id="7000",
        cookie_path=None,
        credential_source=source,
        transport_factory=lambda _cookie: transport,
        environment={LIVE_ENV: "1"},
    )

    assert report["status"] == "blocked"
    assert report["error_code"] == "scan_incomplete"
    assert report["fail_closed"] is True
    assert report["remote_write_calls"] == 0
    assert report["output_root_is_temporary"] is True


def test_client_version_gate_blocks_before_credential_load(monkeypatch):
    monkeypatch.setattr(runner, "version", lambda _: "unexpected")
    source = _Source()

    report = run_acceptance(
        root_id="7000",
        cookie_path=None,
        credential_source=source,
        environment={LIVE_ENV: "1"},
    )

    assert report["status"] == "blocked"
    assert report["error_code"] == "client_version_unverified"
    assert report["remote_write_calls"] == 0
    assert source.calls == 0


def test_cli_without_live_flag_does_not_make_external_calls(capsys, tmp_path):
    assert main(["--root-id", "7000", "--cookie-path", str(tmp_path / "cookie")]) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "blocked"
    assert report["error_code"] == "live_gate_closed"
    assert report["remote_write_calls"] == 0

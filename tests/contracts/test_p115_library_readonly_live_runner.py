import json

from scripts.p115_library_readonly_live_runner import (
    LIVE_ENV,
    main,
    run_live_acceptance,
)


class _Source:
    def __init__(self, value="SYNTHETIC_COOKIE_SECRET"):
        self.value = value
        self.calls = 0

    def load(self):
        self.calls += 1
        return self.value


class _Transport:
    def __init__(self, pages, details):
        self.pages = list(pages)
        self.details = dict(details)
        self.calls = []

    async def fs_files_app(self, payload, *, timeout_seconds):
        self.calls.append(("fs_files_app", dict(payload), timeout_seconds))
        return self.pages.pop(0)

    async def fs_files(self, payload, *, timeout_seconds):
        self.calls.append(("fs_files", dict(payload), timeout_seconds))
        return self.pages.pop(0)

    async def fs_info_app(self, payload, *, timeout_seconds):
        self.calls.append(("fs_info_app", dict(payload), timeout_seconds))
        return self.details[payload.get("fid") or payload["cid"]]

    async def fs_info(self, payload, *, timeout_seconds):
        self.calls.append(("fs_info", dict(payload), timeout_seconds))
        return self.details[payload.get("fid") or payload["cid"]]


def _page(records, *, offset=0, count=1):
    return {
        "state": True,
        "data": records,
        "offset": offset,
        "limit": 1,
        "count": count,
    }


def _file(file_id="101", parent_id="7"):
    return {
        "fc": 1,
        "fid": file_id,
        "cid": parent_id,
        "fn": "sensitive-file-name.wav",
        "s": "123",
        "pick_code": "SYNTHETIC_PICKCODE_SECRET",
    }


def _file_detail(file_id="101", parent_id="7"):
    return {
        "state": True,
        "file_category": "1",
        "fid": file_id,
        "cid": parent_id,
        "file_name": "sensitive-file-name.wav",
        "size": "123",
        "ptime": "1710000000",
        "pick_code": "SYNTHETIC_PICKCODE_SECRET",
    }


def test_default_gate_blocks_before_cookie_or_transport_creation():
    source = _Source()
    factory_calls = []

    report = run_live_acceptance(
        directory_id="7",
        cookie_path=None,
        credential_source=source,
        transport_factory=lambda cookie: factory_calls.append(cookie),
        env={},
    )

    assert report.to_public_dict() == {
        "status": "blocked",
        "error_code": "blocked_environment",
        "pages_read": 0,
        "entries_seen": 0,
        "file_details": 0,
        "directory_details": 0,
        "complete": False,
    }
    assert source.calls == 0
    assert factory_calls == []


def test_success_path_has_one_list_one_detail_and_redacted_public_report():
    source = _Source()
    transport = _Transport((_page([_file()]),), {"101": _file_detail()})
    factory_calls = []

    def factory(cookie):
        factory_calls.append(cookie)
        return transport

    report = run_live_acceptance(
        directory_id="7",
        cookie_path=None,
        credential_source=source,
        transport_factory=factory,
        env={LIVE_ENV: "1"},
    )

    assert report.status == "success"
    assert report.pages_read == 1
    assert report.entries_seen == 1
    assert report.file_details == 1
    assert report.directory_details == 0
    assert [call[0] for call in transport.calls] == ["fs_files_app", "fs_info_app"]
    assert transport.calls[0][1]["cid"] == "7"
    assert transport.calls[1][1] == {"fid": "101"}
    assert source.calls == 1
    assert factory_calls == ["SYNTHETIC_COOKIE_SECRET"]
    rendered = repr(report) + json.dumps(report.to_public_dict())
    assert "sensitive" not in rendered
    assert "PICKCODE" not in rendered
    assert "COOKIE" not in rendered


def test_incomplete_directory_stops_at_page_budget_without_detail_calls():
    source = _Source()
    transport = _Transport(
        (
            _page([_file("101")], offset=0, count=3),
            _page([_file("102")], offset=1, count=3),
        ),
        {},
    )

    report = run_live_acceptance(
        directory_id="7",
        cookie_path=None,
        credential_source=source,
        transport_factory=lambda _cookie: transport,
        env={LIVE_ENV: "1"},
    )

    assert report.status == "partial"
    assert report.error_code == "page_budget_exhausted"
    assert report.pages_read == 2
    assert report.entries_seen == 2
    assert [call[0] for call in transport.calls] == ["fs_files_app", "fs_files_app"]


def test_cli_without_live_flag_has_no_external_calls(capsys):
    assert main(["--directory-id", "7"]) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "blocked"
    assert report["error_code"] == "blocked_environment"

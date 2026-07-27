import pytest

from scripts.p115_c03_live_runner import (
    C03_CLEANUP_PLAN_ENV,
    C03_LIVE_ENV,
    C03_MANAGED_FIXTURE_ENV,
    C03_WRITE_ENABLED_ENV,
    run_live_probe,
)
from scripts.p115_c03_live_runner import (
    main as live_runner_main,
)
from watch_assistant.adapters.p115_c03_fixture_probe import C03ProbeStatus
from watch_assistant.adapters.p115_c03_live_transport import (
    MAX_FS_FILES_PAGE_CALLS,
    P115C03LiveTransport,
)
from watch_assistant.adapters.p115_library_write_contract import (
    WriteStatus,
    prepare_mkdir,
    prepare_move,
    prepare_recycle,
    prepare_rename,
)


class _FakeP115Client:
    def __init__(self, responses):
        self.responses = {name: list(values) for name, values in responses.items()}
        self.calls = []

    def _response(self, name, payload, **kwargs):
        self.calls.append((name, dict(payload), dict(kwargs)))
        value = self.responses[name].pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def fs_mkdir(self, payload, **kwargs):
        return self._response("fs_mkdir", payload, **kwargs)

    def fs_move(self, payload, **kwargs):
        return self._response("fs_move", payload, **kwargs)

    def fs_rename(self, payload, **kwargs):
        return self._response("fs_rename", payload, **kwargs)

    def fs_delete(self, payload, **kwargs):
        return self._response("fs_delete", payload, **kwargs)

    def fs_info(self, payload, **kwargs):
        return self._response("fs_info", payload, **kwargs)

    def fs_files(self, payload, **kwargs):
        return self._response("fs_files", payload, **kwargs)

    def __repr__(self):
        return f"_FakeP115Client(call_count={len(self.calls)})"


def _success():
    return {"state": True}


def _page(records, *, offset, count):
    return {
        "state": True,
        "data": records,
        "offset": offset,
        "limit": 1,
        "count": count,
    }


def _directory(file_id, parent_id, name):
    return {
        "fc": 0,
        "cid": file_id,
        "pid": parent_id,
        "n": name,
        "pick_code": "SECRET_PICKCODE",
        "path": "/private/secret",
    }


@pytest.mark.asyncio
async def test_live_transport_uses_fixed_payloads_and_redacts_write_results():
    client = _FakeP115Client(
        {
            "fs_mkdir": [{"state": True, "data": {"cid": "101"}}],
            "fs_move": [_success()],
            "fs_rename": [_success()],
            "fs_delete": [_success()],
            "fs_info": [],
            "fs_files": [],
        }
    )
    transport = P115C03LiveTransport(client)

    mkdir = await transport.execute(prepare_mkdir("7", "source"))
    move = await transport.execute(prepare_move("101", "8"))
    rename = await transport.execute(prepare_rename("101", "source-renamed"))
    recycle = await transport.execute(prepare_recycle("101"))

    assert mkdir.status is WriteStatus.SUCCESS
    assert mkdir.file_id == "101"
    assert move.status is WriteStatus.SUCCESS
    assert rename.status is WriteStatus.SUCCESS
    assert recycle.status is WriteStatus.SUCCESS
    assert [call[0] for call in client.calls] == [
        "fs_mkdir",
        "fs_move",
        "fs_rename",
        "fs_delete",
    ]
    assert client.calls[0][1] == {"pid": "7", "cname": "source"}
    assert client.calls[1][1] == {"fid": "101", "pid": "8"}
    assert client.calls[2][1] == {"files_new_name[101]": "source-renamed"}
    assert client.calls[3][1] == {"fid": "101"}
    assert all(call[2] == {"async_": False} for call in client.calls)
    rendered = repr(mkdir) + repr(transport) + repr(client)
    assert "101" not in rendered
    assert "source" not in rendered
    assert "SECRET_PICKCODE" not in rendered


@pytest.mark.asyncio
async def test_live_transport_normalizes_directory_info_and_bounded_pages():
    client = _FakeP115Client(
        {
            "fs_mkdir": [],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
            "fs_info": [{"state": True, "data": _directory("101", "7", "source")}],
            "fs_files": [
                _page([_directory("101", "7", "source")], offset=0, count=2),
                _page([_directory("102", "7", "quarantine")], offset=1, count=2),
            ],
        }
    )
    transport = P115C03LiveTransport(client)

    entry = await transport.read("101")
    listing = await transport.list_children("7")

    assert entry is not None
    assert (entry.file_id, entry.parent_id, entry.name, entry.is_directory) == (
        "101",
        "7",
        "source",
        True,
    )
    assert listing.complete is True
    assert [entry.file_id for entry in listing.entries] == ["101", "102"]
    assert listing.page_calls == 2
    assert len(client.calls) == 3
    assert client.calls[0][1] == {"cid": "101"}
    assert client.calls[1][1]["offset"] == 0
    assert client.calls[2][1]["offset"] == 1
    assert client.calls[1][1]["limit"] == 1
    assert client.calls[1][1]["record_open_time"] == 0
    assert client.calls[1][1]["show_dir"] == 1


@pytest.mark.asyncio
async def test_live_transport_fails_closed_on_unknown_write_or_pagination():
    incomplete_pages = [
        _page([_directory(str(index), "7", f"dir-{index}")], offset=index - 1, count=6)
        for index in range(1, MAX_FS_FILES_PAGE_CALLS + 1)
    ]
    client = _FakeP115Client(
        {
            "fs_mkdir": [{"state": True, "data": {"name": "no-id"}}],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
            "fs_info": [{"state": True, "data": {"fc": 0, "pid": "7"}}],
            "fs_files": incomplete_pages,
        }
    )
    transport = P115C03LiveTransport(client)

    unknown_write = await transport.execute(prepare_mkdir("7", "source"))
    bad_info = await transport.read("101")
    incomplete = await transport.list_children("7")

    assert unknown_write.status is WriteStatus.UNCERTAIN
    assert bad_info is None
    assert incomplete.complete is False
    assert incomplete.page_calls == MAX_FS_FILES_PAGE_CALLS == 4
    assert len(client.calls) == 1 + 1 + MAX_FS_FILES_PAGE_CALLS


def _enabled_env() -> dict[str, str]:
    return {
        C03_WRITE_ENABLED_ENV: "1",
        C03_MANAGED_FIXTURE_ENV: "1",
        C03_CLEANUP_PLAN_ENV: "1",
        C03_LIVE_ENV: "1",
    }


def test_live_runner_requires_live_flag_and_gates_before_cookie_or_client(
    monkeypatch, capsys
):
    for name, value in _enabled_env().items():
        monkeypatch.setenv(name, value)
    assert live_runner_main(["--parent-id", "7", "--cookie-path", "secret.cookie"]) == 1
    public = capsys.readouterr().out
    assert '"status": "blocked"' in public
    assert "live_flag_required" in public
    assert "secret.cookie" not in public

    called = False

    def client_factory(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("client must not be created")

    env = _enabled_env()
    env.pop(C03_LIVE_ENV)
    report = run_live_probe(
        parent_id="7",
        cookie_path="secret.cookie",
        env=env,
        client_factory=client_factory,
    )
    assert report.status is C03ProbeStatus.BLOCKED
    assert report.error_code == "live_disabled"
    assert called is False


def test_live_runner_uses_positional_cookie_and_disables_qrcode(tmp_path):
    cookie_path = tmp_path / "cookie.txt"
    cookie_path.write_text("UID=u; CID=c; KID=k; SEID=s", encoding="ascii")
    calls = []

    def client_factory(cookie, *, console_qrcode):
        calls.append((cookie, console_qrcode))
        return object()

    report = run_live_probe(
        parent_id="7",
        cookie_path=cookie_path,
        env=_enabled_env(),
        client_factory=client_factory,
    )

    assert calls == [("UID=u; CID=c; KID=k; SEID=s", False)]
    assert report.status is C03ProbeStatus.UNCERTAIN
    assert "UID=u" not in repr(report)

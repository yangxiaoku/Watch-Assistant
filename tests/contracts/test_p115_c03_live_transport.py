import time

import pytest

from scripts.p115_c03_live_runner import (
    C03_CLEANUP_PLAN_ENV,
    C03_LIVE_ENV,
    C03_MANAGED_FIXTURE_ENV,
    C03_WRITE_ENABLED_ENV,
    _p115client_timeout_executor,
    run_live_probe,
)
from scripts.p115_c03_live_runner import (
    main as live_runner_main,
)
from watch_assistant.adapters.p115_c03_fixture_probe import (
    C03ProbeReport,
    C03ProbeStatus,
)
from watch_assistant.adapters.p115_c03_live_transport import (
    MAX_FS_FILES_PAGE_CALLS,
    P115C03CallTimeoutUnavailable,
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


async def _call_executor(method, payload, *, timeout_seconds):
    assert timeout_seconds > 0
    return method(payload, async_=False)


def test_p115client_timeout_executor_passes_hook_and_does_not_retry(monkeypatch):
    observed = {}

    def fake_urllib3_request(*, async_, **kwargs):
        observed["async"] = async_
        observed.update(kwargs)
        return {"state": True}

    monkeypatch.setattr("urllib3_future_request.request", fake_urllib3_request)

    def method(payload, *, async_, request):
        return request(url="https://example.invalid", method="POST", async_=async_)

    assert _p115client_timeout_executor(method, {"fid": "1"}, timeout_seconds=3.5) == {
        "state": True
    }
    assert observed["async"] is False
    assert observed["timeout"] == 3.5
    assert observed["retries"] is False


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
    transport = P115C03LiveTransport(client, call_executor=_call_executor)

    mkdir = await transport.execute(prepare_mkdir("7", "source"), timeout_seconds=10)
    move = await transport.execute(prepare_move("101", "8"), timeout_seconds=10)
    rename = await transport.execute(
        prepare_rename("101", "source-renamed"), timeout_seconds=10
    )
    recycle = await transport.execute(prepare_recycle("101"), timeout_seconds=10)

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
async def test_live_transport_accepts_verified_blank_mkdir_errno_only():
    client = _FakeP115Client(
        {
            "fs_mkdir": [
                {
                    "state": True,
                    "errno": "",
                    "cid": "101",
                    "file_id": "101",
                }
            ],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
            "fs_info": [],
            "fs_files": [],
        }
    )
    transport = P115C03LiveTransport(client, call_executor=_call_executor)

    accepted = await transport.execute(prepare_mkdir("7", "source"), timeout_seconds=10)
    assert accepted.status is WriteStatus.SUCCESS
    assert accepted.file_id == "101"

    client.responses["fs_mkdir"] = [
        {"state": True, "errno": "unexpected", "cid": "102", "file_id": "102"}
    ]
    rejected = await transport.execute(prepare_mkdir("7", "other"), timeout_seconds=10)
    assert rejected.status is WriteStatus.UNCERTAIN


@pytest.mark.asyncio
async def test_live_transport_requires_listing_for_exact_directory_identity():
    client = _FakeP115Client(
        {
            "fs_mkdir": [],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
            "fs_info": [
                {
                    "state": True,
                    "data": {"file_category": "0", "file_name": "source"},
                }
            ],
            "fs_files": [
                _page([_directory("101", "7", "source")], offset=0, count=2),
                _page([_directory("102", "7", "quarantine")], offset=1, count=2),
            ],
        }
    )
    transport = P115C03LiveTransport(client, call_executor=_call_executor)

    entry = await transport.read("101", timeout_seconds=10)
    listing = await transport.list_children("7", timeout_seconds=10)

    assert entry is None
    assert (
        listing.entries[0].file_id,
        listing.entries[0].parent_id,
        listing.entries[0].name,
        listing.entries[0].is_directory,
    ) == (
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
async def test_live_transport_passes_decreasing_timeout_to_each_page():
    client = _FakeP115Client(
        {
            "fs_mkdir": [],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
            "fs_info": [],
            "fs_files": [
                _page([_directory("101", "7", "source")], offset=0, count=2),
                _page([_directory("102", "7", "quarantine")], offset=1, count=2),
            ],
        }
    )
    observed = []

    async def executor(method, payload, *, timeout_seconds):
        observed.append(timeout_seconds)
        return method(payload, async_=False)

    listing = await P115C03LiveTransport(client, call_executor=executor).list_children(
        "7", timeout_seconds=10
    )

    assert listing.complete is True
    assert len(observed) == 2
    assert observed[0] >= observed[1] > 0


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
    transport = P115C03LiveTransport(client, call_executor=_call_executor)

    unknown_write = await transport.execute(
        prepare_mkdir("7", "source"), timeout_seconds=10
    )
    bad_info = await transport.read("101", timeout_seconds=10)
    incomplete = await transport.list_children("7", timeout_seconds=10)

    assert unknown_write.status is WriteStatus.UNCERTAIN
    assert bad_info is None
    assert incomplete.complete is False
    assert incomplete.page_calls == MAX_FS_FILES_PAGE_CALLS == 4
    assert len(client.calls) == 1 + 1 + MAX_FS_FILES_PAGE_CALLS


@pytest.mark.asyncio
async def test_live_transport_rejects_duplicate_directory_entries_across_pages():
    client = _FakeP115Client(
        {
            "fs_mkdir": [],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
            "fs_info": [],
            "fs_files": [
                _page([_directory("101", "7", "source")], offset=0, count=2),
                _page([_directory("101", "7", "source")], offset=1, count=2),
            ],
        }
    )

    listing = await P115C03LiveTransport(
        client, call_executor=_call_executor
    ).list_children("7", timeout_seconds=10)

    assert listing.complete is False
    assert listing.page_calls == 2
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_live_transport_without_timeout_executor_fails_before_client_call():
    client = _FakeP115Client(
        {
            "fs_mkdir": [_success()],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
            "fs_info": [],
            "fs_files": [],
        }
    )
    transport = P115C03LiveTransport(client)
    with pytest.raises(P115C03CallTimeoutUnavailable):
        await transport.execute(prepare_mkdir("7", "source"), timeout_seconds=10)
    assert client.calls == []


def _enabled_env() -> dict[str, str]:
    return {
        C03_WRITE_ENABLED_ENV: "1",
        C03_MANAGED_FIXTURE_ENV: "1",
        C03_CLEANUP_PLAN_ENV: "1",
        C03_LIVE_ENV: "1",
    }


def _authorization(path, parent_id="7", *, expires_at=None):
    if expires_at is None:
        expires_at = time.time() + 60
    path.write_text(
        f'{{"version": 1, "parent_id": "{parent_id}", '
        f'"expires_at": {expires_at}, "nonce": "offline"}}',
        encoding="ascii",
    )


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
    assert report.error_code == "blocked_environment"
    assert called is False


def test_live_runner_cli_injects_bounded_executor(monkeypatch, capsys):
    observed = {}

    def fake_run_live_probe(**kwargs):
        observed.update(kwargs)
        return C03ProbeReport(
            status=C03ProbeStatus.SUCCESS,
            fixture_fingerprint="offline",
            steps=(),
            write_calls=1,
            read_calls=1,
            list_calls=1,
            page_calls=1,
            cleanup="confirmed",
        )

    monkeypatch.setattr(
        "scripts.p115_c03_live_runner.run_live_probe", fake_run_live_probe
    )

    assert (
        live_runner_main(
            [
                "--parent-id",
                "7",
                "--cookie-path",
                "cookie.txt",
                "--authorization-path",
                "authorization.json",
                "--managed-scope-path",
                "scope.json",
                "--live",
            ]
        )
        == 0
    )
    assert observed["call_executor"] is _p115client_timeout_executor
    assert '"status": "success"' in capsys.readouterr().out


def test_live_runner_uses_positional_cookie_and_disables_qrcode(tmp_path, monkeypatch):
    cookie_path = tmp_path / "cookie.txt"
    cookie_path.write_text("UID=u; CID=c; KID=k; SEID=s", encoding="ascii")
    calls = []

    def client_factory(cookie, *, console_qrcode):
        calls.append((cookie, console_qrcode))
        return object()

    authorization_path = tmp_path / "authorization.json"
    _authorization(authorization_path)
    monkeypatch.setattr(
        "scripts.p115_c03_live_runner._p115client_version",
        lambda: "0.0.9.6.5.1",
    )

    report = run_live_probe(
        parent_id="7",
        cookie_path=cookie_path,
        env=_enabled_env(),
        client_factory=client_factory,
        authorization_path=authorization_path,
        managed_parent_ids=("7",),
        call_executor=_call_executor,
    )

    assert calls == [("UID=u; CID=c; KID=k; SEID=s", False)]
    assert report.status is C03ProbeStatus.UNCERTAIN
    assert "UID=u" not in repr(report)


def test_live_runner_scope_and_timeout_gates_precede_cookie_and_client(
    tmp_path, monkeypatch
):
    cookie_path = tmp_path / "cookie.txt"
    cookie_path.write_text("SENSITIVE_COOKIE", encoding="ascii")
    authorization_path = tmp_path / "authorization.json"
    _authorization(authorization_path)
    monkeypatch.setattr(
        "scripts.p115_c03_live_runner._p115client_version",
        lambda: "0.0.9.6.5.1",
    )
    called = []

    def client_factory(*args, **kwargs):
        called.append(True)
        return object()

    report = run_live_probe(
        parent_id="7000",
        cookie_path=cookie_path,
        authorization_path=authorization_path,
        managed_parent_ids=("7",),
        call_executor=_call_executor,
        env=_enabled_env() | {C03_LIVE_ENV: "1"},
        client_factory=client_factory,
    )
    assert report.status is C03ProbeStatus.BLOCKED
    assert report.error_code == "blocked_environment"
    assert called == []

    authorization_path = tmp_path / "authorization-unsupported-signature.json"
    _authorization(authorization_path)

    def unsupported_executor(method, payload):
        return method(payload, async_=False)

    report = run_live_probe(
        parent_id="7",
        cookie_path=cookie_path,
        authorization_path=authorization_path,
        managed_parent_ids=("7",),
        call_executor=unsupported_executor,
        env=_enabled_env() | {C03_LIVE_ENV: "1"},
        client_factory=client_factory,
    )
    assert report.status is C03ProbeStatus.BLOCKED
    assert report.error_code == "blocked_environment"
    assert called == []
    assert report.write_calls == report.read_calls == report.list_calls == 0

    authorization_path = tmp_path / "authorization-unsupported.json"
    _authorization(authorization_path)
    report = run_live_probe(
        parent_id="7",
        cookie_path=cookie_path,
        authorization_path=authorization_path,
        managed_parent_ids=("7",),
        env=_enabled_env() | {C03_LIVE_ENV: "1"},
        client_factory=client_factory,
    )
    assert report.status is C03ProbeStatus.BLOCKED
    assert report.error_code == "blocked_environment"
    assert called == []


def test_live_runner_authorization_is_atomic_and_one_shot(tmp_path, monkeypatch):
    cookie_path = tmp_path / "cookie.txt"
    cookie_path.write_text("SENSITIVE_COOKIE", encoding="ascii")
    authorization_path = tmp_path / "authorization.json"
    _authorization(authorization_path)
    monkeypatch.setattr(
        "scripts.p115_c03_live_runner._p115client_version",
        lambda: "0.0.9.6.5.1",
    )
    factory_calls = []

    def client_factory(*args, **kwargs):
        factory_calls.append(True)
        return object()

    kwargs = {
        "parent_id": "7",
        "cookie_path": cookie_path,
        "authorization_path": authorization_path,
        "managed_parent_ids": ("7",),
        "call_executor": _call_executor,
        "env": _enabled_env() | {C03_LIVE_ENV: "1"},
        "client_factory": client_factory,
    }
    first = run_live_probe(**kwargs)
    second = run_live_probe(**kwargs)
    assert first.status is C03ProbeStatus.UNCERTAIN
    assert second.status is C03ProbeStatus.BLOCKED
    assert second.error_code == "blocked_environment"
    assert len(factory_calls) == 1
    assert (tmp_path / "authorization.json.consumed").read_bytes() == b"consumed\n"
    rendered = repr(first) + repr(second)
    assert "SENSITIVE_COOKIE" not in rendered

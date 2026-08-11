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
    C03RemoteEntry,
)
from watch_assistant.adapters.p115_c03_live_transport import (
    MAX_FS_FILES_PAGE_CALLS,
    PRODUCTION_FS_FILES_PAGE_CALLS,
    PRODUCTION_FS_FILES_PAGE_SIZE,
    P115C03CallTimeoutUnavailable,
    P115C03LiveTransport,
    P115C03ProductionTransport,
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
        values = self.responses.get(name)
        if values is None and name in {"fs_files_app", "fs_info_app"}:
            # app-first 语义下 transport 优先调用 proapi 读接口;未显式
            # 提供 app 响应时复用旧接口响应(数据同源)。
            values = self.responses.get(name.removesuffix("_app"))
        value = values.pop(0)
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

    def fs_info_app(self, payload, **kwargs):
        return self._response("fs_info_app", payload, **kwargs)

    def fs_files_app(self, payload, **kwargs):
        return self._response("fs_files_app", payload, **kwargs)

    def __repr__(self):
        return f"_FakeP115Client(call_count={len(self.calls)})"


def _success():
    return {"state": True}


def _page(records, *, offset, count, limit=1):
    return {
        "state": True,
        "data": records,
        "offset": offset,
        "limit": limit,
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


def _file(file_id, parent_id, name):
    return {
        "fc": 1,
        "fid": file_id,
        "cid": parent_id,
        "n": name,
    }


class _MethodNotAllowed(RuntimeError):
    status_code = 405


async def _call_executor(method, payload, *, timeout_seconds):
    assert timeout_seconds > 0
    return method(payload, async_=False)


def test_p115client_timeout_executor_retries_990009_once_after_three_seconds(monkeypatch):
    observed = {}
    sleeps = []
    calls = 0

    def fake_urllib3_request(*, async_, **kwargs):
        observed["async"] = async_
        observed.update(kwargs)
        return {"state": True}

    monkeypatch.setattr("urllib3_future_request.request", fake_urllib3_request)
    monkeypatch.setattr("scripts.p115_c03_live_runner.time.sleep", sleeps.append)

    def method(payload, *, async_, request):
        nonlocal calls
        calls += 1
        if calls == 1:
            error = RuntimeError("redacted")
            error.errno = 990009
            raise error
        return request(url="https://example.invalid", method="POST", async_=async_)

    assert _p115client_timeout_executor(method, {"fid": "1"}, timeout_seconds=3.5) == {
        "state": True
    }
    assert observed["async"] is False
    assert observed["timeout"] == 3.5
    assert observed["retries"] is False
    assert calls == 2
    assert sleeps == [3.0]


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
async def test_live_transport_falls_back_to_legacy_reads_only_for_http_405():
    # app-first 语义:读接口优先调用 proapi app 端点;app 端点 405 时回退
    # 旧接口;非 405 异常必须 fail-closed(不得静默回退过期索引)。
    client = _FakeP115Client(
        {
            "fs_mkdir": [],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
            "fs_info_app": [_MethodNotAllowed()],
            "fs_files_app": [_MethodNotAllowed()],
            "fs_info": [
                {
                    "state": True,
                    "data": {
                        "fc": 0,
                        "cid": "101",
                        "pid": "7",
                        "n": "source",
                    },
                }
            ],
            "fs_files": [_page([_directory("101", "7", "source")], offset=0, count=1)],
        }
    )
    transport = P115C03LiveTransport(client, call_executor=_call_executor)

    assert await transport.read("101", timeout_seconds=10) == C03RemoteEntry(
        "101", "7", "source", True
    )
    listing = await transport.list_children("7", timeout_seconds=10)

    assert listing.complete is True
    assert [call[0] for call in client.calls] == [
        "fs_info_app",
        "fs_info",
        "fs_files_app",
        "fs_files",
    ]


@pytest.mark.asyncio
async def test_live_transport_fails_closed_when_app_read_fails_without_405():
    observed = []

    async def failing_app(method, payload, *, timeout_seconds):
        observed.append(method.__name__)
        if method.__name__ in {"fs_info_app", "fs_files_app"}:
            raise OSError("network broken")
        return method(payload, async_=False)

    client = _FakeP115Client(
        {
            "fs_mkdir": [],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
            "fs_info": [],
            "fs_files": [],
        }
    )
    transport = P115C03LiveTransport(client, call_executor=failing_app)

    with pytest.raises(OSError, match="network broken"):
        await transport.read("101", timeout_seconds=10)
    listing = await transport.list_children("7", timeout_seconds=10)

    assert listing.complete is False
    assert listing.page_calls == 1
    assert observed == ["fs_info_app", "fs_files_app"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_live_transport_lists_existing_files_without_rejecting_the_page():
    client = _FakeP115Client(
        {
            "fs_mkdir": [],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
            "fs_info": [],
            "fs_files": [
                _page([_file("101", "7", "existing-file")], offset=0, count=2),
                _page([_directory("102", "7", "fixture-root")], offset=1, count=2),
            ],
        }
    )

    listing = await P115C03LiveTransport(
        client, call_executor=_call_executor
    ).list_children("7", timeout_seconds=10)

    assert listing.complete is True
    assert [(entry.file_id, entry.is_directory) for entry in listing.entries] == [
        ("101", False),
        ("102", True),
    ]


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
    total = MAX_FS_FILES_PAGE_CALLS + 1
    incomplete_pages = [
        _page([_directory(str(index), "7", f"dir-{index}")], offset=index - 1, count=total)
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
    assert incomplete.page_calls == MAX_FS_FILES_PAGE_CALLS
    assert len(client.calls) == 1 + 1 + MAX_FS_FILES_PAGE_CALLS


@pytest.mark.asyncio
async def test_production_transport_reads_more_than_eight_entries_completely():
    # 回归：生产执行路径（P115C03ProductionTransport）必须能完整读取 >8 条的
    # 真实媒体目录；旧实现 8 页 × 1 条/页会让此类目录在首次写入前即
    # complete=False → observation_unverified → 永久 UNCERTAIN。
    records = [_file(f"30{index}", "7", f"movie-{index}.mkv") for index in range(12)]
    client = _FakeP115Client(
        {
            "fs_mkdir": [],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
            "fs_info": [],
            "fs_files": [_page(records, offset=0, count=12, limit=50)],
        }
    )
    transport = P115C03ProductionTransport(client, call_executor=_call_executor)

    listing = await transport.list_children("7", timeout_seconds=10)

    assert listing.complete is True
    assert [entry.file_id for entry in listing.entries] == [
        f"30{index}" for index in range(12)
    ]
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_live_transport_page_parameters_are_configurable_and_validated():
    # 逐实例分页参数：显式覆盖生效；非法值拒绝；不传则保持 C03 验证默认。
    assert P115C03LiveTransport(
        client=object(), call_executor=_call_executor  # type: ignore[arg-type]
    )._max_page_calls == MAX_FS_FILES_PAGE_CALLS
    assert P115C03LiveTransport(
        client=object(),  # type: ignore[arg-type]
        call_executor=_call_executor,
        max_page_calls=3,
        page_size=2,
    )._max_page_calls == 3
    assert (
        P115C03LiveTransport(
            client=object(),  # type: ignore[arg-type]
            call_executor=_call_executor,
            max_page_calls=3,
            page_size=2,
        )._page_size
        == 2
    )
    production = P115C03ProductionTransport(
        client=object(), call_executor=_call_executor  # type: ignore[arg-type]
    )
    assert production._max_page_calls == PRODUCTION_FS_FILES_PAGE_CALLS
    assert production._page_size == PRODUCTION_FS_FILES_PAGE_SIZE
    for bad in (0, -1, True, 2.5):
        with pytest.raises(ValueError):
            P115C03LiveTransport(
                client=object(),  # type: ignore[arg-type]
                call_executor=_call_executor,
                max_page_calls=bad,
            )
        with pytest.raises(ValueError):
            P115C03LiveTransport(
                client=object(),  # type: ignore[arg-type]
                call_executor=_call_executor,
                page_size=bad,
            )


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
async def test_live_transport_rejects_total_count_drift_across_pages():
    client = _FakeP115Client(
        {
            "fs_mkdir": [],
            "fs_move": [],
            "fs_rename": [],
            "fs_delete": [],
            "fs_info": [],
            "fs_files": [
                _page([_directory("101", "7", "source")], offset=0, count=2),
                _page([_directory("102", "7", "quarantine")], offset=1, count=3),
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


@pytest.mark.asyncio
async def test_sync_call_executor_runs_in_a_worker_thread():
    """同步 executor(生产 p115_c03_timeout_executor,含 busy 重试 time.sleep)
    必须在线程池执行:executor 线程不能是事件循环线程,否则 sleep 会冻结
    整个 loop。"""
    import threading

    loop_thread_id = threading.get_ident()
    executor_thread_ids = []
    client = _FakeP115Client({"fs_move": [_success()]})

    def executor(method, payload, *, timeout_seconds):
        executor_thread_ids.append(threading.get_ident())
        return method(payload, async_=False)

    transport = P115C03LiveTransport(client, call_executor=executor)

    receipt = await transport.execute(prepare_move("100", "9000"), timeout_seconds=30)

    assert receipt.status is WriteStatus.SUCCESS
    assert executor_thread_ids and executor_thread_ids[0] != loop_thread_id

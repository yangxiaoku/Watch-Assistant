import json
import os
from pathlib import Path

import httpx
import pytest
import respx

from scripts.tgto_contract_check import ContractResult, run_contract_check
from scripts.tgto_contract_probe import extract_api_paths, main, probe_routes

BASE_URL = "http://tgto.test"


def _write_contract(
    path: Path,
    *,
    supported: bool,
    status_supported: bool = True,
) -> None:
    contract = {
        "schema_version": 1,
        "supported": supported,
        "login": {
            "method": "POST",
            "path": "/api/login",
            "content_type": "application/json",
            "username_field": "username",
            "password_field": "password",
            "success_field": "success",
        },
        "submit": (
            {
                "method": "POST",
                "path": "/api/tasks",
                "remote_reference_field": "id",
            }
            if supported
            else None
        ),
        "status": (
            {
                "method": "GET",
                "path_template": "/api/tasks/{remote_reference}",
            }
            if supported and status_supported
            else None
        ),
        "checks": (
            [
                {
                    "method": "GET",
                    "path": "/api/tasks/metadata",
                    "accept_status": [200],
                }
            ]
            if supported
            else []
        ),
    }
    path.write_text(json.dumps(contract), encoding="utf-8")


def _configure_environment(
    monkeypatch: pytest.MonkeyPatch, contract_path: Path
) -> None:
    monkeypatch.setenv("TGTO_BASE_URL", BASE_URL)
    monkeypatch.setenv("TGTO_WEB_USER", "contract-user")
    monkeypatch.setenv("TGTO_WEB_PASSWORD", "contract-password")
    monkeypatch.setenv("TGTO_CONTRACT_PATH", str(contract_path))


@pytest.mark.integration
async def test_configured_tgto_contract_is_readable():
    contract_path = Path(__file__).parents[2] / "config" / "tgto-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if not contract["supported"]:
        pytest.skip("committed TgtoDrive contract explicitly reports supported=false")

    missing = [
        name
        for name in ("TGTO_BASE_URL", "TGTO_WEB_USER", "TGTO_WEB_PASSWORD")
        if not os.environ.get(name)
    ]
    if missing:
        pytest.skip(f"live TgtoDrive credentials are absent: {', '.join(missing)}")

    result = await run_contract_check()
    assert result.login_ok is True
    assert result.submit_supported is True
    assert result.status_supported is True or result.uncertain_fallback is True


def test_committed_tgto_contract_disables_unverified_push():
    contract_path = Path(__file__).parents[2] / "config" / "tgto-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))

    assert contract["schema_version"] == 1
    assert contract["supported"] is False
    assert contract["submit"] is None
    assert contract["status"] is None
    assert contract["checks"] == []
    assert contract["login"] == {
        "method": "POST",
        "path": "/api/login",
        "content_type": "application/json",
        "username_field": "username",
        "password_field": "password",
        "success_field": "success",
    }


@respx.mock
async def test_supported_contract_passes_harmless_checks(tmp_path, monkeypatch):
    contract_path = tmp_path / "contract.json"
    _write_contract(contract_path, supported=True)
    _configure_environment(monkeypatch, contract_path)
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    metadata = respx.get(f"{BASE_URL}/api/tasks/metadata").mock(
        return_value=httpx.Response(200, json={"kind": "metadata"})
    )

    result = await run_contract_check()

    assert result == ContractResult(True, True, True, False)
    assert metadata.called


@respx.mock
async def test_unsupported_contract_keeps_push_disabled(tmp_path, monkeypatch):
    contract_path = tmp_path / "contract.json"
    _write_contract(contract_path, supported=False)
    _configure_environment(monkeypatch, contract_path)
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    result = await run_contract_check()

    assert result == ContractResult(True, False, False, False)


@respx.mock
async def test_unsupported_flag_overrides_complete_route_declarations(
    tmp_path, monkeypatch
):
    contract_path = tmp_path / "contract.json"
    _write_contract(contract_path, supported=True)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["supported"] = False
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    _configure_environment(monkeypatch, contract_path)
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    result = await run_contract_check()

    assert result == ContractResult(True, False, False, False)


@respx.mock
async def test_submit_requires_remote_reference_field(tmp_path, monkeypatch):
    contract_path = tmp_path / "contract.json"
    _write_contract(contract_path, supported=True)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    del contract["submit"]["remote_reference_field"]
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    _configure_environment(monkeypatch, contract_path)
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    respx.get(f"{BASE_URL}/api/tasks/metadata").mock(return_value=httpx.Response(200))

    result = await run_contract_check()

    assert result == ContractResult(True, False, False, False)


@pytest.mark.parametrize(
    "path_template",
    [
        "/api/tasks/status",
        "/api/proxy/https://source.example/{remote_reference}",
        "/api/../admin/{remote_reference}",
        "/api//tasks/{remote_reference}",
    ],
)
@respx.mock
async def test_status_requires_safe_remote_reference_template(
    tmp_path, monkeypatch, path_template
):
    contract_path = tmp_path / "contract.json"
    _write_contract(contract_path, supported=True)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["status"]["path_template"] = path_template
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    _configure_environment(monkeypatch, contract_path)
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    respx.get(f"{BASE_URL}/api/tasks/metadata").mock(return_value=httpx.Response(200))

    result = await run_contract_check()

    assert result == ContractResult(True, True, False, True)


@respx.mock
async def test_supported_contract_requires_harmless_check(tmp_path, monkeypatch):
    contract_path = tmp_path / "contract.json"
    _write_contract(contract_path, supported=True)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["checks"] = []
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    _configure_environment(monkeypatch, contract_path)
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    result = await run_contract_check()

    assert result == ContractResult(True, False, False, False)


@respx.mock
async def test_login_failure_disables_supported_routes(tmp_path, monkeypatch):
    contract_path = tmp_path / "contract.json"
    _write_contract(contract_path, supported=True)
    _configure_environment(monkeypatch, contract_path)
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(401, json={"success": False})
    )

    result = await run_contract_check()

    assert result == ContractResult(False, False, False, False)


@pytest.mark.parametrize("contract_text", ["{broken", "[]"])
async def test_invalid_contract_data_fails_closed(tmp_path, monkeypatch, contract_text):
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(contract_text, encoding="utf-8")
    _configure_environment(monkeypatch, contract_path)

    result = await run_contract_check()

    assert result == ContractResult(False, False, False, False)


@respx.mock
async def test_login_json_array_fails_closed(tmp_path, monkeypatch):
    contract_path = tmp_path / "contract.json"
    _write_contract(contract_path, supported=True)
    _configure_environment(monkeypatch, contract_path)
    respx.post(f"{BASE_URL}/api/login").mock(return_value=httpx.Response(200, json=[]))

    result = await run_contract_check()

    assert result == ContractResult(False, False, False, False)


@pytest.mark.parametrize("accept_status", [[True], [99], [600]])
@respx.mock
async def test_invalid_accept_status_fails_closed(tmp_path, monkeypatch, accept_status):
    contract_path = tmp_path / "contract.json"
    _write_contract(contract_path, supported=True)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["checks"][0]["accept_status"] = accept_status
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    _configure_environment(monkeypatch, contract_path)
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    result = await run_contract_check()

    assert result == ContractResult(True, False, False, False)


@respx.mock
async def test_missing_status_route_enables_uncertain_fallback(tmp_path, monkeypatch):
    contract_path = tmp_path / "contract.json"
    _write_contract(contract_path, supported=True, status_supported=False)
    _configure_environment(monkeypatch, contract_path)
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    respx.get(f"{BASE_URL}/api/tasks/metadata").mock(return_value=httpx.Response(200))

    result = await run_contract_check()

    assert result == ContractResult(True, True, False, True)


@respx.mock
async def test_probe_output_redacts_credentials_and_query_values(monkeypatch, capsys):
    monkeypatch.setenv("TGTO_BASE_URL", BASE_URL)
    monkeypatch.setenv("TGTO_WEB_USER", "sensitive-user")
    monkeypatch.setenv("TGTO_WEB_PASSWORD", "sensitive-password")
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "echo": "sensitive-password"},
        )
    )
    respx.get(f"{BASE_URL}/static/script.js").mock(
        return_value=httpx.Response(
            200,
            text=(
                'fetch("/api/debug/sensitive-user"); '
                'fetch("/api/debug/sensitive-password"); '
                'fetch("/api/session/550e8400-e29b-41d4-a716-446655440000"); '
                'fetch("/api/token/abc123"); '
                'fetch("/api/tasks?token=response-secret"); '
                'fetch("/api/status/1")'
            ),
        )
    )

    await probe_routes()

    output = capsys.readouterr().out
    assert "200 /api/login" in output
    assert "200 /static/script.js" in output
    assert "/api/tasks" in output
    assert "/api/status/<redacted>" in output
    assert "/api/<redacted>" in output
    assert "/api/session/<redacted>" in output
    assert "/api/token/<redacted>" in output
    assert "sensitive-user" not in output
    assert "sensitive-password" not in output
    assert "550e8400-e29b-41d4-a716-446655440000" not in output
    assert "abc123" not in output
    assert "response-secret" not in output


def test_extract_api_paths_stops_before_unsafe_source_values():
    script = (
        'fetch("/api/proxy/http/source.example/private/path") '
        'fetch("/api/debug/sk-abcdefghijklmnopqrstuvwx") '
        'fetch("/api/files/private%2Ftoken") '
        'fetch("/api/auth/eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature") '
        'fetch("/api/download/0123456789abcdef0123456789abcdef") '
        'fetch("/api/session/superSecretBearerValue123456789") '
        'fetch("/api/tasks?token=query-secret") '
        'fetch("/api/status/1#fragment-secret")'
    )
    paths = extract_api_paths(script)

    assert paths == [
        "/api/<redacted>",
        "/api/auth/<redacted>",
        "/api/session/<redacted>",
        "/api/status/<redacted>",
        "/api/tasks",
    ]
    output = "\n".join(paths)
    assert "source.example" not in output
    assert "%" not in output
    assert "eyJ" not in output
    assert "0123456789abcdef" not in output
    assert "superSecretBearerValue" not in output
    assert "query-secret" not in output
    assert "fragment-secret" not in output
    assert "sk-abcdefghijklmnopqrstuvwx" not in output
    assert "source.example" not in output


def test_extract_api_paths_blocks_explicit_forbidden_values():
    paths = extract_api_paths(
        'fetch("/api/debug/current-user"); '
        'fetch("/api/debug/current-password"); '
        'fetch("/api/115/qr/status")',
        forbidden_values=("current-user", "current-password"),
    )

    assert paths == ["/api/115/qr/status", "/api/<redacted>"]


def test_extract_api_paths_keeps_observed_static_route_skeleton():
    paths = extract_api_paths('fetch("/api/1.0/mac/1.0/qrcode?uid=dynamic")')

    assert paths == ["/api/1.0/mac/1.0/qrcode"]


@pytest.mark.parametrize(
    "marker",
    [
        "auth",
        "authorization",
        "cookie",
        "password",
        "secret",
        "session",
        "token",
    ],
)
def test_extract_api_paths_stops_after_credential_marker(marker):
    paths = extract_api_paths(f'fetch("/api/{marker}/short-value")')

    assert paths == [f"/api/{marker}/<redacted>"]


@respx.mock
def test_probe_cli_redacts_network_exception_and_returns_nonzero(monkeypatch, capsys):
    monkeypatch.setenv("TGTO_BASE_URL", BASE_URL)
    monkeypatch.setenv("TGTO_WEB_USER", "sensitive-user")
    monkeypatch.setenv("TGTO_WEB_PASSWORD", "sensitive-password")
    respx.post(f"{BASE_URL}/api/login").mock(
        side_effect=httpx.ConnectError(
            "sensitive-password at https://private.example/path"
        )
    )

    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "ERROR /api/probe\n"


@respx.mock
def test_probe_cli_handles_error_response_without_printing_body(monkeypatch, capsys):
    monkeypatch.setenv("TGTO_BASE_URL", BASE_URL)
    monkeypatch.setenv("TGTO_WEB_USER", "sensitive-user")
    monkeypatch.setenv("TGTO_WEB_PASSWORD", "sensitive-password")
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(
            503,
            text="sensitive-password https://private.example /api/debug/secret",
        )
    )

    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == "503 /api/login\n"
    assert captured.err == "ERROR /api/probe\n"


def test_probe_cli_handles_invalid_url_without_echoing_it(monkeypatch, capsys):
    monkeypatch.setenv("TGTO_BASE_URL", "http://[sensitive-password")
    monkeypatch.setenv("TGTO_WEB_USER", "sensitive-user")
    monkeypatch.setenv("TGTO_WEB_PASSWORD", "sensitive-password")

    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "ERROR /api/probe\n"

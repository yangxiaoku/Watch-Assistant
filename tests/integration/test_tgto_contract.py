import json
import os
from pathlib import Path

import httpx
import pytest
import respx

from scripts.tgto_contract_check import ContractResult, run_contract_check
from scripts.tgto_contract_probe import probe_routes

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


def _configure_environment(monkeypatch: pytest.MonkeyPatch, contract_path: Path) -> None:
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
async def test_login_failure_disables_supported_routes(tmp_path, monkeypatch):
    contract_path = tmp_path / "contract.json"
    _write_contract(contract_path, supported=True)
    _configure_environment(monkeypatch, contract_path)
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(401, json={"success": False})
    )

    result = await run_contract_check()

    assert result == ContractResult(False, False, False, False)


@respx.mock
async def test_missing_status_route_enables_uncertain_fallback(tmp_path, monkeypatch):
    contract_path = tmp_path / "contract.json"
    _write_contract(contract_path, supported=True, status_supported=False)
    _configure_environment(monkeypatch, contract_path)
    respx.post(f"{BASE_URL}/api/login").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    respx.get(f"{BASE_URL}/api/tasks/metadata").mock(
        return_value=httpx.Response(200)
    )

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
            text='fetch("/api/tasks?token=response-secret"); fetch("/api/status/1")',
        )
    )

    await probe_routes()

    output = capsys.readouterr().out
    assert "200 /api/login" in output
    assert "200 /static/script.js" in output
    assert "/api/tasks" in output
    assert "/api/status/1" in output
    assert "sensitive-user" not in output
    assert "sensitive-password" not in output
    assert "response-secret" not in output

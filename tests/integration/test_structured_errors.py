from pathlib import Path

import pytest

from watch_assistant.app import create_app


@pytest.mark.anyio
async def test_http_errors_include_structured_safe_payload_and_legacy_detail(tmp_path: Path):
    import httpx

    app = create_app(frontend_dir=tmp_path / "missing-frontend")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        response = await client.get("/route-that-does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "not_found"
    assert body["error"]["code"] == "not_found"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]
    assert body["error"]["correlation_id"] == response.headers["X-Correlation-ID"]
    assert body["error"]["message_zh"]
    assert body["error"]["field_errors"] == []


@pytest.mark.anyio
async def test_validation_errors_are_structured_without_raw_framework_message(tmp_path: Path):
    import httpx
    from fastapi import Query

    app = create_app(frontend_dir=tmp_path / "missing-frontend")

    @app.get("/_test-validation")
    async def test_validation(page: int = Query(ge=1)):
        return {"page": page}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://app.test"
    ) as client:
        response = await client.get("/_test-validation?page=not-a-number")

    assert response.status_code == 422
    body = response.json()
    assert body["detail"] == "validation_error"
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["field_errors"]
    assert all(item["message_zh"] == "字段内容格式不正确。" for item in body["error"]["field_errors"])

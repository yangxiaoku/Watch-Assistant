from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx


def _module():
    path = Path(__file__).parents[2] / "scripts" / "mcp_stdio.py"
    spec = importlib.util.spec_from_file_location("mcp_stdio", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_invalid_stdio_payload_is_a_jsonrpc_error_without_forwarding():
    module = _module()
    result = module._error_response(None, "invalid_request", "MCP 请求必须是 JSON 对象。")
    assert result["jsonrpc"] == "2.0"
    assert result["id"] is None
    assert result["error"]["data"]["error_code"] == "invalid_request"


def test_forward_maps_http_and_shape_failures_to_structured_errors():
    module = _module()

    class Client:
        def __init__(self, response):
            self.response = response

        def post(self, *_args, **_kwargs):
            return self.response

    request = httpx.Request("POST", "http://example.test/api/v1/mcp")
    response = httpx.Response(401, json={"detail": "unauthorized"}, request=request)
    result = module._forward(Client(response), "http://example.test", {"jsonrpc": "2.0", "id": 7}, "token")
    assert result["id"] == 7
    assert result["error"]["data"]["error_code"] == "mcp_transport_error"
    assert "unauthorized" not in str(result)

    response = httpx.Response(200, json=["not", "an", "object"], request=request)
    result = module._forward(Client(response), "http://example.test", {"jsonrpc": "2.0", "id": 8}, "token")
    assert result["error"]["data"]["error_code"] == "mcp_invalid_response"

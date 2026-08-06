import json

import pytest

import scripts.p115_organization_live_runner as runner
from watch_assistant.adapters.p115_c03_fixture_probe import (
    C03DirectoryListing,
    C03RemoteEntry,
    C03WriteReceipt,
)
from watch_assistant.adapters.p115_library_write_contract import (
    WriteOperation,
    WriteStatus,
)
from watch_assistant.adapters.p115_organization_transport import (
    OrganizationObjectIntent,
)
from watch_assistant.services.organization_execution_contract import RemoteObjectState
from watch_assistant.services.organization_executor import OrganizationTransportStatus


@pytest.mark.asyncio
async def test_confirmed_probe_opens_write_gate_for_move_and_restore(
    monkeypatch, tmp_path
):
    authorization = tmp_path / "authorization.json"
    authorization.write_text(json.dumps({"unused": True}), encoding="utf-8")

    temporary_name = runner._temporary_directory_name("101", "source.mkv")
    source = C03RemoteEntry("101", "202", "source.mkv", is_directory=False)
    temporary = C03RemoteEntry("303", "202", temporary_name, is_directory=True)

    class _ListingTransport:
        def __init__(self):
            self.created = False
            self.recycled = False
            self.operations = []

        async def list_children(self, parent_id, *, timeout_seconds):
            assert timeout_seconds > 0
            if parent_id != "202":
                raise AssertionError("unexpected listing scope")
            if self.recycled:
                return C03DirectoryListing((source,))
            if self.created:
                return C03DirectoryListing((source, temporary))
            return C03DirectoryListing((source,))

        async def execute(self, request, *, timeout_seconds):
            assert timeout_seconds > 0
            self.operations.append(request.operation)
            if request.operation is WriteOperation.MKDIR:
                self.created = True
                return C03WriteReceipt(WriteStatus.SUCCESS, file_id="303")
            if request.operation is WriteOperation.RECYCLE:
                self.recycled = True
                return C03WriteReceipt(WriteStatus.SUCCESS)
            raise AssertionError("unexpected write operation")

    listing_transport = _ListingTransport()
    monkeypatch.setattr(
        runner,
        "P115C03LiveTransport",
        lambda client, call_executor: listing_transport,
    )

    captured = []

    class _OrganizationTransport:
        def __init__(self, intent):
            self.intent = intent
            self.state = RemoteObjectState(
                intent.object_id, intent.source_parent_id, intent.source_name
            )
            self.receipts = []

        async def read_object(self, object_id):
            assert object_id == self.intent.object_id
            return self.state

        async def read_target(self, parent_id, name):
            assert (parent_id, name) == (
                self.intent.target_parent_id,
                self.intent.target_name,
            )

        async def move(self, object_id, target_parent_id):
            assert object_id == self.intent.object_id
            self.state = RemoteObjectState(
                object_id, target_parent_id, self.state.name
            )
            result = type(
                "Result",
                (),
                {"status": OrganizationTransportStatus.SUCCESS},
            )()
            self.receipts.append(result)
            return result

        async def rename(self, object_id, target_name):
            assert object_id == self.intent.object_id
            self.state = RemoteObjectState(
                object_id, self.state.parent_id, target_name
            )
            result = type(
                "Result",
                (),
                {"status": OrganizationTransportStatus.SUCCESS},
            )()
            self.receipts.append(result)
            return result

    def factory(**kwargs):
        captured.append(kwargs)
        return _OrganizationTransport(next(iter(kwargs["intents"])))

    monkeypatch.setattr(
        runner, "create_live_p115_organization_transport", factory
    )

    result = await runner._execute_and_restore(
        object(),
        "202",
        OrganizationObjectIntent(
            "101", "202", "source.mkv", "202", "wa-org-probe-token.mkv"
        ),
        original_name="source.mkv",
        authorization_path=authorization,
    )

    assert result["status"] == "success"
    assert listing_transport.operations == [WriteOperation.MKDIR, WriteOperation.RECYCLE]
    assert len(captured) == 2
    assert all(item["live_enabled"] is True for item in captured)
    assert all(item["write_enabled"] is True for item in captured)
    assert all(item["plan_confirmed"] is True for item in captured)

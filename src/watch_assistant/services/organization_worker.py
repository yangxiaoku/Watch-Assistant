"""Explicitly gated production worker for confirmed organization operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from typing import Any

from watch_assistant.adapters.p115_c03_live_transport import (
    EXPECTED_P115CLIENT_VERSION,
    p115_c03_timeout_executor,
)
from watch_assistant.adapters.p115_organization_transport import (
    OrganizationObjectIntent,
    create_live_p115_organization_transport,
)
from watch_assistant.models import OrganizationOperationStatus
from watch_assistant.services.maintenance_gate import MaintenanceGate
from watch_assistant.services.organization_executor import OrganizationExecutor
from watch_assistant.services.organization_operations import (
    OrganizationOperationService,
    OrganizationOperationStateError,
)
from watch_assistant.services.p115_credentials import CookieProvider

P115ClientFactory = Callable[[str], Any]


class OrganizationWorker:
    """Poll and execute only durable, explicitly approved organization work.

    The worker owns no plan generation and never changes an ``uncertain``
    operation back to ``planned``.  A caller must enable it explicitly and
    provide the production root identity separately from credential readiness.
    """

    def __init__(
        self,
        session_factory,
        operation_service: OrganizationOperationService,
        cookie_provider: CookieProvider,
        *,
        production_root_id: str,
        live_enabled: bool,
        poll_interval_seconds: float = 5.0,
        client_factory: P115ClientFactory | None = None,
        call_executor=None,
        event_logger=None,
        settings_service=None,
        maintenance_gate: MaintenanceGate | None = None,
    ) -> None:
        if not production_root_id.isdigit() or production_root_id.startswith("0"):
            raise ValueError("invalid_production_root_id")
        if live_enabled is not True:
            raise ValueError("organization_worker_requires_explicit_enablement")
        if poll_interval_seconds <= 0:
            raise ValueError("invalid_poll_interval")
        self._session_factory = session_factory
        self._operations = operation_service
        self._cookie_provider = cookie_provider
        self._production_root_id = production_root_id
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._client_factory = client_factory or _default_client_factory
        self._call_executor = call_executor or p115_c03_timeout_executor
        self._event_logger = event_logger
        self._settings_service = settings_service
        self._maintenance_gate = maintenance_gate
        self._stop = asyncio.Event()

    async def run_once(self) -> bool:
        if self._maintenance_gate is None:
            lease = await self._operations.claim_next()
        else:
            async with self._maintenance_gate.lock:
                if await self._maintenance_gate.is_active():
                    return False
                lease = await self._operations.claim_next()
        if lease is None:
            return False
        plan_scope = await self._operations.plan_execution_scope(lease.operation_id)
        if plan_scope is None or self._production_root_id not in plan_scope:
            await self._finish_failed(lease, "plan_prerequisites_changed")
            return True

        steps = await self._operations.load_execution_steps(lease.operation_id)
        if not steps:
            await self._finish_failed(lease, "plan_prerequisites_changed")
            return True
        intents = tuple(
            OrganizationObjectIntent(
                member.object_id,
                member.source_parent_id,
                member.source_name,
                member.target_parent_id,
                member.target_name,
            )
            for step in steps
            for member in step.members
        )
        if len({intent.object_id for intent in intents}) != len(intents):
            await self._finish_failed(lease, "plan_prerequisites_changed")
            return True

        client = await self._build_client()
        if client is None:
            await self._finish_failed(lease, "remote_write_failed")
            return True
        try:
            operation_delay = 0.25
            if self._settings_service is not None:
                try:
                    organization_settings = await self._settings_service.get_organization()
                    operation_delay = organization_settings.operation_delay_seconds
                except Exception:  # noqa: BLE001 - retain the conservative fallback
                    operation_delay = 0.25
            transport = create_live_p115_organization_transport(
                client=client,
                call_executor=self._call_executor,
                intents=intents,
                managed_directory_ids=plan_scope,
                scope_confirmed=True,
                live_enabled=True,
            )
            executor = OrganizationExecutor(
                self._operations,
                self._session_factory,
                transport,
                max_transport_calls=128,
                min_call_interval=operation_delay,
            )
            await executor.execute(
                lease.operation_id,
                expected_revision=lease.revision,
                lease_token=lease.lease_token,
                cancel_event=self._stop,
            )
        except asyncio.CancelledError:
            raise
        except OrganizationOperationStateError:
            return True
        except Exception as exc:  # noqa: BLE001 - remote details stay private
            del exc
            # The executor persists uncertainty for remote or local failures;
            # do not turn an ambiguous call into a retryable local failure.
            try:
                await self._operations.finish_after_lease_loss(
                    lease.operation_id,
                    expected_revision=lease.revision,
                    lease_token=lease.lease_token,
                )
            except Exception as exc:  # noqa: BLE001 - preserve the original uncertain state
                del exc
        finally:
            await _close_client(client)
        return True

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or self._stop
        while not stop.is_set():
            try:
                claimed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - worker loop must stay alive
                del exc
                claimed = True
            if claimed:
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                continue

    async def _build_client(self) -> Any | None:
        try:
            cookie = await asyncio.to_thread(self._cookie_provider.load)
            if not cookie:
                return None
            return await asyncio.to_thread(self._client_factory, cookie)
        except Exception as exc:  # noqa: BLE001 - credential details stay private
            del exc
            return None

    async def _finish_failed(self, lease, error_code: str) -> None:
        try:
            await self._operations.finish(
                lease.operation_id,
                expected_revision=lease.revision,
                lease_token=lease.lease_token,
                status=OrganizationOperationStatus.FAILED,
                error_code=error_code,
            )
        except Exception as exc:  # noqa: BLE001 - lease outcome stays private
            del exc
            try:
                await self._operations.finish_after_lease_loss(
                    lease.operation_id,
                    expected_revision=lease.revision,
                    lease_token=lease.lease_token,
                )
            except Exception as exc:  # noqa: BLE001 - preserve uncertainty
                del exc


def _default_client_factory(cookie: str) -> Any:
    if version("p115client") != EXPECTED_P115CLIENT_VERSION:
        raise RuntimeError("unsupported_p115client_version")
    from p115client import P115Client

    return P115Client(cookie, console_qrcode=False)


async def _close_client(client: Any) -> None:
    close = getattr(client, "close", None) or getattr(client, "aclose", None)
    if not callable(close):
        return
    try:
        result = close()
        if isinstance(result, Awaitable):
            await result
    except Exception as exc:  # noqa: BLE001 - client cleanup is best effort
        del exc


__all__ = ["OrganizationWorker"]

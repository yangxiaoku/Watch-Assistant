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
from watch_assistant.adapters.p115_library_write_contract import (
    OrganizationWriteGate,
    P115OrganizationContract,
    WriteOperation,
    evaluate_organization_read_gate,
    evaluate_organization_write_gate,
)
from watch_assistant.adapters.p115_organization_transport import (
    OrganizationObjectIntent,
    create_live_p115_organization_transport,
)
from watch_assistant.models import OrganizationOperationStatus
from watch_assistant.services.organization_executor import (
    OrganizationExecutionResult,
    OrganizationExecutionStatus,
    OrganizationExecutor,
)
from watch_assistant.services.organization_operations import (
    OrganizationOperationService,
    OrganizationOperationStateError,
)
from watch_assistant.services.p115_credentials import CookieProvider

P115ClientFactory = Callable[[str], Any]
OrganizationDirectoryProvisioner = Callable[[str], Awaitable[None]]


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
        write_enabled: bool = False,
        poll_interval_seconds: float = 5.0,
        client_factory: P115ClientFactory | None = None,
        call_executor=None,
        event_logger=None,
        settings_service=None,
        organization_contract: P115OrganizationContract | None = None,
        directory_provisioner: OrganizationDirectoryProvisioner | None = None,
    ) -> None:
        if not production_root_id.isdigit() or production_root_id.startswith("0"):
            raise ValueError("invalid_production_root_id")
        if live_enabled is not True:
            raise ValueError("organization_worker_requires_explicit_enablement")
        if poll_interval_seconds <= 0:
            raise ValueError("invalid_poll_interval")
        if organization_contract is None:
            raise ValueError("organization_contract_required")
        self._session_factory = session_factory
        self._operations = operation_service
        self._cookie_provider = cookie_provider
        self._production_root_id = production_root_id
        self._live_enabled = live_enabled is True
        self._write_enabled = write_enabled is True
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._client_factory = client_factory or _default_client_factory
        self._call_executor = call_executor or p115_c03_timeout_executor
        self._event_logger = event_logger
        self._settings_service = settings_service
        self._organization_contract = organization_contract
        self._directory_provisioner = directory_provisioner
        self._stop = asyncio.Event()

    async def run_once(self) -> bool:
        lease = await self._operations.claim_next()
        if lease is None:
            return False
        plan_scope = await self._operations.plan_execution_scope(
            lease.operation_id,
            expected_operation_revision=lease.revision,
        )
        if plan_scope is None or self._production_root_id not in plan_scope:
            await self._finish_failed(lease, "plan_prerequisites_changed")
            return True

        steps = await self._operations.load_execution_steps(
            lease.operation_id,
            expected_operation_revision=lease.revision,
        )
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
                target_directory_path=getattr(member, "target_directory_path", None),
            )
            for step in steps
            for member in step.members
        )
        if len({intent.object_id for intent in intents}) != len(intents):
            await self._finish_failed(lease, "plan_prerequisites_changed")
            return True

        scope_confirmed = self._production_root_id in plan_scope
        # ``load_execution_steps`` only returns durable plans in the confirmed
        # ``planned`` state, so its result is the worker's confirmation evidence.
        plan_confirmed = bool(steps)
        gate = OrganizationWriteGate(
            write_enabled=self._write_enabled,
            plan_confirmed=plan_confirmed,
            scope_confirmed=scope_confirmed,
            contract=self._organization_contract,
        )
        for operation in _required_write_operations(steps):
            decision = evaluate_organization_write_gate(gate, operation)
            if not decision.allowed:
                await self._finish_failed(
                    lease, decision.error_code or "contract_unverified"
                )
                return True

        if self._directory_provisioner is not None:
            try:
                lease = await self._operations.renew_lease(
                    lease.operation_id,
                    expected_revision=lease.revision,
                    lease_token=lease.lease_token,
                )
                await self._directory_provisioner(lease.operation_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - remote details stay private
                error_code = getattr(error, "code", None)
                if not isinstance(error_code, str) or not error_code:
                    error_code = "target_directory_create_failed"
                if getattr(error, "uncertain", False) is True:
                    await self._finish_uncertain(lease, error_code)
                else:
                    await self._finish_failed(lease, error_code)
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
                scope_confirmed=scope_confirmed,
                live_enabled=self._live_enabled,
                write_enabled=self._write_enabled,
                plan_confirmed=plan_confirmed,
                organization_contract=self._organization_contract,
                target_root_id=self._production_root_id,
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
        # 连续异常时指数退避(上限 30s,成功复位),避免 DB 锁定等故障下
        # 以满速热循环刷爆日志并加剧写锁竞争(与 worker.py 主循环一致)。
        backoff = 0.0
        while not stop.is_set():
            if backoff > 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                if stop.is_set():
                    break
                backoff = 0.0
            try:
                claimed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - worker loop must stay alive
                del exc
                backoff = min(30.0, (backoff or 1.0) * 2)
                continue
            backoff = 0.0
            if claimed:
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                continue

    async def reconcile_once(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        cancel_event: asyncio.Event | None = None,
    ) -> OrganizationExecutionResult:
        """Perform only read-only remote reconciliation for one uncertain run."""

        summary = await self._operations.get(operation_id)
        if (
            summary.status is not OrganizationOperationStatus.UNCERTAIN
            or summary.revision != expected_revision
        ):
            raise OrganizationOperationStateError("uncertain_requires_verification")
        plan_scope = await self._operations.plan_execution_scope(
            operation_id,
            expected_operation_revision=expected_revision,
        )
        steps = await self._operations.load_execution_steps(
            operation_id,
            expected_operation_revision=expected_revision,
        )
        if (
            not plan_scope
            or self._production_root_id not in plan_scope
            or not steps
        ):
            return OrganizationExecutionResult(
                operation_id,
                OrganizationExecutionStatus.UNCERTAIN,
                "scope_unverified",
                0,
                0,
            )
        intents = tuple(
            OrganizationObjectIntent(
                member.object_id,
                member.source_parent_id,
                member.source_name,
                member.target_parent_id,
                member.target_name,
                target_directory_path=getattr(member, "target_directory_path", None),
            )
            for step in steps
            for member in step.members
        )
        if len({intent.object_id for intent in intents}) != len(intents):
            return OrganizationExecutionResult(
                operation_id,
                OrganizationExecutionStatus.UNCERTAIN,
                "plan_prerequisites_changed",
                0,
                0,
            )
        scope_confirmed = self._production_root_id in plan_scope
        plan_confirmed = bool(steps)
        gate = OrganizationWriteGate(
            write_enabled=self._write_enabled,
            plan_confirmed=plan_confirmed,
            scope_confirmed=scope_confirmed,
            contract=self._organization_contract,
        )
        decision = evaluate_organization_read_gate(gate)
        if not decision.allowed:
            return OrganizationExecutionResult(
                operation_id,
                OrganizationExecutionStatus.UNCERTAIN,
                decision.error_code or "contract_unverified",
                0,
                0,
            )
        client = await self._build_client()
        if client is None:
            return OrganizationExecutionResult(
                operation_id,
                OrganizationExecutionStatus.UNCERTAIN,
                "credentials_unavailable",
                0,
                0,
            )
        try:
            transport = create_live_p115_organization_transport(
                client=client,
                call_executor=self._call_executor,
                intents=intents,
                managed_directory_ids=plan_scope,
                scope_confirmed=scope_confirmed,
                live_enabled=self._live_enabled,
                write_enabled=self._write_enabled,
                plan_confirmed=plan_confirmed,
                read_only=True,
                organization_contract=self._organization_contract,
                target_root_id=self._production_root_id,
            )
            executor = OrganizationExecutor(
                self._operations,
                self._session_factory,
                transport,
                max_transport_calls=128,
            )
            return await executor.reconcile_uncertain(
                operation_id,
                expected_revision=expected_revision,
                cancel_event=cancel_event,
            )
        except asyncio.CancelledError:
            raise
        except OrganizationOperationStateError:
            raise
        except Exception:  # noqa: BLE001 - remote details stay private
            return OrganizationExecutionResult(
                operation_id,
                OrganizationExecutionStatus.UNCERTAIN,
                "outcome_unknown",
                0,
                0,
            )
        finally:
            await _close_client(client)

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

    async def _finish_uncertain(self, lease, error_code: str) -> None:
        try:
            await self._operations.finish(
                lease.operation_id,
                expected_revision=lease.revision,
                lease_token=lease.lease_token,
                status=OrganizationOperationStatus.UNCERTAIN,
                error_code=error_code,
            )
        except Exception as exc:  # noqa: BLE001 - preserve uncertainty
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


def _required_write_operations(steps) -> tuple[WriteOperation, ...]:
    operations = [WriteOperation.MOVE, WriteOperation.RENAME]
    if any(step.replacement_object_id is not None for step in steps):
        operations.append(WriteOperation.RECYCLE)
    return tuple(operations)


__all__ = ["OrganizationWorker"]

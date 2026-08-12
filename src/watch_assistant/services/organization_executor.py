"""Offline organization executor using an explicitly injected transport.

This module has no P115 client, configuration lookup, worker hook, or retry
loop.  The durable plan loader and operation lease are the only sources of
execution authority.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.models import OrganizationOperationStatus
from watch_assistant.services.organization_companion_contract import (
    check_group_after_write,
    check_group_before_write,
    resolve_organization_companion_group,
)
from watch_assistant.services.organization_execution_contract import (
    OrganizationStepCheck,
    OrganizationStepExpectation,
    RemoteObjectState,
    reconcile_uncertain,
)
from watch_assistant.services.organization_operations import (
    VALID_OPERATION_ERROR_CODES,
    OrganizationOperationConflict,
    OrganizationOperationLease,
    OrganizationOperationLeaseUnavailable,
    OrganizationOperationService,
    OrganizationOperationStateError,
)
from watch_assistant.services.organization_plan import (
    OrganizationPlanExecutionStep,
)


class OrganizationTransportOperation(StrEnum):
    MOVE = "move"
    RENAME = "rename"
    RECYCLE = "recycle"


class OrganizationTransportStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationTransportResult:
    operation: OrganizationTransportOperation
    status: OrganizationTransportStatus
    error_code: str | None = None

    def __repr__(self) -> str:
        return (
            "OrganizationTransportResult(operation="
            f"{self.operation.value!r}, status={self.status.value!r}, "
            f"error_code={_safe_transport_code(self.error_code)!r})"
        )


class OrganizationExecutorTransport(Protocol):
    """Read/write seam for a future controlled gateway; fake-only in Phase 2."""

    async def read_object(self, object_id: str) -> RemoteObjectState | None: ...

    async def read_object_in_scope(
        self, object_id: str, parent_ids: Collection[str]
    ) -> RemoteObjectState | None: ...

    async def read_target(
        self, parent_id: str, name: str
    ) -> RemoteObjectState | None: ...

    async def move(
        self, object_id: str, target_parent_id: str
    ) -> OrganizationTransportResult: ...

    async def rename(
        self, object_id: str, target_name: str
    ) -> OrganizationTransportResult: ...

    async def recycle(
        self, object_id: str, parent_id: str, name: str
    ) -> OrganizationTransportResult: ...


class OrganizationExecutionStatus(StrEnum):
    ORGANIZED = "organized"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True, slots=True, repr=False)
class OrganizationExecutionResult:
    operation_id: str
    status: OrganizationExecutionStatus
    error_code: str | None
    completed_steps: int
    transport_calls: int

    def __repr__(self) -> str:
        return (
            "OrganizationExecutionResult(operation_id=<redacted>, "
            f"status={self.status.value!r}, error_code={self.error_code!r}, "
            f"completed_steps={self.completed_steps}, "
            f"transport_calls={self.transport_calls})"
        )


class OrganizationExecutorError(ValueError):
    """Stable local executor boundary error."""


@dataclass(slots=True)
class _ExecutionContext:
    lease: OrganizationOperationLease
    completed_steps: int = 0
    transport_calls: int = 0
    last_call_at: float | None = None
    write_started: bool = False
    write_confirmed: bool = False
    plan_revision_changed: bool = False


class _TransportFailure(Exception):
    def __init__(self, error_code: str, *, uncertain: bool = True) -> None:
        self.error_code = error_code
        self.uncertain = uncertain


class _RateLimitReached(Exception):
    pass


class _ExecutionCancelled(Exception):
    pass


class _LeaseLost(Exception):
    pass


class OrganizationExecutor:
    """Execute one already-claimed local operation through an injected seam."""

    def __init__(
        self,
        operation_service: OrganizationOperationService,
        session_factory: async_sessionmaker[AsyncSession],
        transport: OrganizationExecutorTransport,
        *,
        max_transport_calls: int = 128,
        min_call_interval: float = 0.0,
        write_interval_seconds: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if (
            isinstance(max_transport_calls, bool)
            or not isinstance(max_transport_calls, int)
            or max_transport_calls <= 0
            or not isinstance(min_call_interval, (int, float))
            or isinstance(min_call_interval, bool)
            or min_call_interval < 0
            or (
                write_interval_seconds is not None
                and (
                    not isinstance(write_interval_seconds, (int, float))
                    or isinstance(write_interval_seconds, bool)
                    or write_interval_seconds < 0
                )
            )
        ):
            raise OrganizationExecutorError("invalid_execution_budget")
        self._operation_service = operation_service
        self._session_factory = session_factory
        self._transport = transport
        self._max_transport_calls = max_transport_calls
        self._min_call_interval = float(min_call_interval)
        if write_interval_seconds is None:
            # 写操作专属间隔:未显式配置时退避到 min_call_interval*3,
            # 且至少固定 3.0s(缓解 115 高频写操作风控)。
            self._write_interval = max(self._min_call_interval * 3, 3.0)
        else:
            self._write_interval = float(write_interval_seconds)
        self._sleep = sleep

    async def execute(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        lease_token: str,
        cancel_event: asyncio.Event | None = None,
        now=None,
    ) -> OrganizationExecutionResult:
        summary = await self._operation_service.get(operation_id)
        if (
            summary.status is not OrganizationOperationStatus.ORGANIZING
            or summary.revision != expected_revision
        ):
            raise OrganizationOperationStateError("operation_is_not_claimed")
        steps = await self._operation_service.load_execution_steps(
            operation_id,
            expected_operation_revision=expected_revision,
        )
        context = _ExecutionContext(
            lease=OrganizationOperationLease(
                operation_id=operation_id,
                revision=expected_revision,
                lease_token=lease_token,
                lease_expires_at=now,
            )
        )
        if steps is None:
            return await self._finish(
                context,
                OrganizationExecutionStatus.FAILED,
                "plan_prerequisites_changed",
                now=now,
            )
        try:
            return await self._run_steps(
                context, steps, cancel_event=cancel_event, now=now
            )
        except asyncio.CancelledError:
            await self._finish(
                context,
                OrganizationExecutionStatus.UNCERTAIN,
                "cancelled",
                now=now,
            )
            raise
        except Exception:  # any unexpected executor failure is uncertainty
            # 未预期异常(OSError/TypeError 等)逃逸 _run_steps 时,worker 只会用
            # 陈旧的初始 lease 调 finish_after_lease_loss,其 CAS 因 revision 已
            # 被 _renew 递增而失败并被吞掉,操作卡在 ORGANIZING 最长 5 分钟。
            # 这里用 executor 当前持有的最新 lease 直接终态化,避免写已发生但
            # 状态长期悬置、错误码退化为 outcome_unknown。
            await self._finish(
                context,
                OrganizationExecutionStatus.UNCERTAIN,
                "outcome_unknown",
                now=now,
            )
            raise

    async def reconcile_uncertain(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        cancel_event: asyncio.Event | None = None,
        now=None,
    ) -> OrganizationExecutionResult:
        """Read the remote state and resolve an uncertain operation by CAS.

        This path intentionally does not claim a lease and never calls a
        transport write method.  A source match makes the operation retryable;
        only an exact target match is committed as organized.  Mixed or
        incomplete observations remain uncertain.
        """

        summary = await self._operation_service.get(operation_id)
        if (
            summary.status is not OrganizationOperationStatus.UNCERTAIN
            or summary.revision != expected_revision
        ):
            raise OrganizationOperationStateError("uncertain_requires_verification")
        steps = await self._operation_service.load_execution_steps(
            operation_id,
            expected_operation_revision=expected_revision,
        )
        if not steps:
            return OrganizationExecutionResult(
                operation_id,
                OrganizationExecutionStatus.UNCERTAIN,
                "plan_prerequisites_changed",
                0,
                0,
            )

        observations: list[OrganizationStepCheck] = []
        replacement_observations: list[str] = []
        transport_calls = 0
        last_error = "outcome_unknown"
        for step in steps:
            for member in step.members:
                if _is_cancelled(cancel_event):
                    return OrganizationExecutionResult(
                        operation_id,
                        OrganizationExecutionStatus.UNCERTAIN,
                        "cancelled",
                        0,
                        transport_calls,
                    )
                if not await self._operation_service.plan_revision_is_current(
                    operation_id, expected_operation_revision=expected_revision
                ):
                    return OrganizationExecutionResult(
                        operation_id,
                        OrganizationExecutionStatus.UNCERTAIN,
                        "plan_prerequisites_changed",
                        0,
                        transport_calls,
                    )
                if transport_calls >= self._max_transport_calls:
                    return OrganizationExecutionResult(
                        operation_id,
                        OrganizationExecutionStatus.UNCERTAIN,
                        "rate_limited",
                        0,
                        transport_calls,
                    )
                expectation = OrganizationStepExpectation(
                    object_id=member.object_id,
                    source_parent_id=member.source_parent_id,
                    source_name=member.source_name,
                    target_parent_id=member.target_parent_id,
                    target_name=member.target_name,
                )
                transport_calls += 1
                try:
                    observed = await self._transport.read_object(member.object_id)
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    observations.append(OrganizationStepCheck.UNCERTAIN)
                    last_error = "timeout"
                    continue
                except Exception:  # noqa: BLE001 - remote details stay private
                    observations.append(OrganizationStepCheck.UNCERTAIN)
                    last_error = "outcome_unknown"
                    continue
                result = reconcile_uncertain(expectation, observed=observed)
                observations.append(result.status)
                if result.error_code is not None:
                    last_error = result.error_code

            if step.replacement_object_id is None:
                continue
            if _is_cancelled(cancel_event):
                return OrganizationExecutionResult(
                    operation_id,
                    OrganizationExecutionStatus.UNCERTAIN,
                    "cancelled",
                    0,
                    transport_calls,
                )
            if not await self._operation_service.plan_revision_is_current(
                operation_id, expected_operation_revision=expected_revision
            ):
                return OrganizationExecutionResult(
                    operation_id,
                    OrganizationExecutionStatus.UNCERTAIN,
                    "plan_prerequisites_changed",
                    0,
                    transport_calls,
                )
            if transport_calls >= self._max_transport_calls:
                return OrganizationExecutionResult(
                    operation_id,
                    OrganizationExecutionStatus.UNCERTAIN,
                    "rate_limited",
                    0,
                    transport_calls,
                )
            transport_calls += 1
            try:
                replacement = await self._transport.read_object_in_scope(
                    step.replacement_object_id,
                    step.scope_directory_ids,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                replacement_observations.append("uncertain")
                last_error = "timeout"
                continue
            except Exception:  # noqa: BLE001 - remote details stay private
                replacement_observations.append("uncertain")
                last_error = "outcome_unknown"
                continue
            if replacement is None:
                # The transport searched every directory frozen by the plan.
                # An object absent from that complete scope has left the
                # managed tree, which is the recycle postcondition.
                replacement_observations.append("removed")
            elif (
                replacement.object_id == step.replacement_object_id
                and replacement.parent_id == step.replacement_parent_id
                and replacement.name == step.replacement_name
                and replacement.is_directory is False
            ):
                replacement_observations.append("present")
            else:
                replacement_observations.append("uncertain")
                last_error = "replacement_reconciliation_unverified"

        if (
            all(status is OrganizationStepCheck.ALREADY_APPLIED for status in observations)
            and all(status == "removed" for status in replacement_observations)
        ):
            if not await self._operation_service.plan_revision_is_current(
                operation_id, expected_operation_revision=expected_revision
            ):
                return OrganizationExecutionResult(
                    operation_id,
                    OrganizationExecutionStatus.UNCERTAIN,
                    "plan_prerequisites_changed",
                    0,
                    transport_calls,
                )
            first = steps[0].members[0]
            try:
                await self._operation_service.reconcile_organized(
                    operation_id,
                    expected_revision=expected_revision,
                    source_directory_id=first.source_parent_id,
                    target_directory_id=first.target_parent_id,
                    directory_ids={
                        directory_id
                        for step in steps
                        for directory_id in step.scope_directory_ids
                    },
                    now=now,
                )
            except OrganizationOperationConflict as error:
                if str(error) == "plan_revision_changed":
                    return OrganizationExecutionResult(
                        operation_id,
                        OrganizationExecutionStatus.UNCERTAIN,
                        "plan_prerequisites_changed",
                        0,
                        transport_calls,
                    )
                return OrganizationExecutionResult(
                    operation_id,
                    OrganizationExecutionStatus.LEASE_LOST,
                    "lease_lost",
                    0,
                    transport_calls,
                )
            return OrganizationExecutionResult(
                operation_id,
                OrganizationExecutionStatus.ORGANIZED,
                None,
                len(steps),
                transport_calls,
            )

        if (
            all(status is OrganizationStepCheck.NOT_APPLIED for status in observations)
            and all(status in {"present", "removed"} for status in replacement_observations)
        ):
            if not await self._operation_service.plan_revision_is_current(
                operation_id, expected_operation_revision=expected_revision
            ):
                return OrganizationExecutionResult(
                    operation_id,
                    OrganizationExecutionStatus.UNCERTAIN,
                    "plan_prerequisites_changed",
                    0,
                    transport_calls,
                )
            try:
                await self._operation_service.reconcile_not_applied(
                    operation_id,
                    expected_revision=expected_revision,
                    now=now,
                )
            except OrganizationOperationConflict as error:
                if str(error) == "plan_revision_changed":
                    return OrganizationExecutionResult(
                        operation_id,
                        OrganizationExecutionStatus.UNCERTAIN,
                        "plan_prerequisites_changed",
                        0,
                        transport_calls,
                    )
                return OrganizationExecutionResult(
                    operation_id,
                    OrganizationExecutionStatus.LEASE_LOST,
                    "lease_lost",
                    0,
                    transport_calls,
                )
            if replacement_observations and all(
                status == "removed" for status in replacement_observations
            ):
                # 成员仍在源(执行未发生)且垃圾文件已离开受管范围:回收已完成,
                # 剩余移动可安全重试。没有 replacement 的计划保持原有
                # remote_write_failed 语义(all([]) 为真但不产生该错误码)。
                retry_error_code = "replacement_already_removed"
            else:
                retry_error_code = "remote_write_failed"
            return OrganizationExecutionResult(
                operation_id,
                OrganizationExecutionStatus.FAILED,
                retry_error_code,
                0,
                transport_calls,
            )
        return OrganizationExecutionResult(
            operation_id,
            OrganizationExecutionStatus.UNCERTAIN,
            last_error,
            0,
            transport_calls,
        )

    async def _run_steps(
        self,
        context: _ExecutionContext,
        steps: tuple[OrganizationPlanExecutionStep, ...],
        *,
        cancel_event: asyncio.Event | None,
        now,
    ) -> OrganizationExecutionResult:
        all_scope_ids = {
            directory_id
            for step in steps
            for member in step.members
            for directory_id in (member.source_parent_id, member.target_parent_id)
        }
        for step in steps:
            if _is_cancelled(cancel_event):
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.CANCELLED,
                    "cancelled",
                    now=now,
                )
            if not await self._renew(context, now=now):
                return await self._handle_lease_loss(context, now=now)
            group = _group_for_step(step)
            if group is None:
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.FAILED,
                    "plan_prerequisites_changed",
                    now=now,
                )
            sources: dict[str, RemoteObjectState | None] = {}
            targets: dict[tuple[str, str], RemoteObjectState | None] = {}
            try:
                for member in step.members:
                    self._check_cancel(cancel_event)
                    if not await self._renew(context, now=now):
                        return await self._handle_lease_loss(context, now=now)
                    sources[member.object_id] = await self._call(
                        context,
                        self._transport.read_object,
                        member.object_id,
                        cancel_event=cancel_event,
                        now=now,
                    )
                    self._check_cancel(cancel_event)
                    if not await self._renew(context, now=now):
                        return await self._handle_lease_loss(context, now=now)
                    self._check_cancel(cancel_event)
                    targets[
                        (member.target_parent_id, member.target_name)
                    ] = await self._call(
                        context,
                        self._transport.read_target,
                        member.target_parent_id,
                        member.target_name,
                        cancel_event=cancel_event,
                        now=now,
                    )
            except _RateLimitReached:
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.FAILED,
                    "rate_limited",
                    now=now,
                )
            except _ExecutionCancelled:
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.CANCELLED,
                    "cancelled",
                    now=now,
                )
            except _LeaseLost:
                return await self._handle_lease_loss(context, now=now)
            except _TransportFailure as failure:
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.UNCERTAIN,
                    failure.error_code,
                    now=now,
                )
            result = check_group_before_write(group, sources=sources, targets=targets)
            if step.replacement_object_id is not None:
                replacement_target = targets.get(
                    (step.replacement_parent_id, step.replacement_name)
                )
                if replacement_target is None:
                    # 垃圾文件已不在目标位置:回收前提已满足(可能已被回收,
                    # 例如回收后崩溃再重放),跳过回收直接继续移动。
                    recycle_needed = False
                else:
                    recycle_needed = True
                    if (
                        replacement_target.object_id != step.replacement_object_id
                        or replacement_target.is_directory is not False
                    ):
                        return await self._finish(
                            context,
                            OrganizationExecutionStatus.FAILED,
                            "plan_prerequisites_changed",
                            now=now,
                        )
                if any(
                    target is not None
                    and (
                        target.object_id != step.replacement_object_id
                        or target.is_directory is not False
                    )
                    for key, target in targets.items()
                    if key != (step.replacement_parent_id, step.replacement_name)
                ):
                    return await self._finish(
                        context,
                        OrganizationExecutionStatus.FAILED,
                        "plan_prerequisites_changed",
                        now=now,
                    )
                effective_targets = dict(targets)
                effective_targets[(step.replacement_parent_id, step.replacement_name)] = None
                result = check_group_before_write(
                    group, sources=sources, targets=effective_targets
                )
                if result.status is not OrganizationStepCheck.READY:
                    return await self._finish(
                        context,
                        OrganizationExecutionStatus.FAILED,
                        "plan_prerequisites_changed",
                        now=now,
                    )
                if recycle_needed:
                    try:
                        recycled = await self._call(
                            context,
                            self._transport.recycle,
                            step.replacement_object_id,
                            step.replacement_parent_id,
                            step.replacement_name,
                            cancel_event=cancel_event,
                            now=now,
                            is_write=True,
                        )
                        self._check_write_result(
                            recycled, OrganizationTransportOperation.RECYCLE
                        )
                        context.write_confirmed = True
                        target_after_recycle = await self._call(
                            context,
                            self._transport.read_target,
                            step.replacement_parent_id,
                            step.replacement_name,
                            cancel_event=cancel_event,
                            now=now,
                        )
                        if target_after_recycle is not None:
                            return await self._finish(
                                context,
                                OrganizationExecutionStatus.UNCERTAIN,
                                "cleanup_postcondition_mismatch",
                                now=now,
                            )
                    except _RateLimitReached:
                        return await self._finish(
                            context,
                            OrganizationExecutionStatus.UNCERTAIN,
                            "rate_limited",
                            now=now,
                        )
                    except _ExecutionCancelled:
                        return await self._finish(
                            context,
                            OrganizationExecutionStatus.CANCELLED,
                            "cancelled",
                            now=now,
                        )
                    except _LeaseLost:
                        return await self._handle_lease_loss(context, now=now)
                    except _TransportFailure as failure:
                        return await self._finish(
                            context,
                            OrganizationExecutionStatus.UNCERTAIN
                            if failure.uncertain or context.write_confirmed
                            else OrganizationExecutionStatus.FAILED,
                            failure.error_code,
                            now=now,
                        )
            if result.status is OrganizationStepCheck.ALREADY_APPLIED and any(
                isinstance(target, RemoteObjectState)
                and target.object_id != member.object_id
                for member in step.members
                for target in (
                    targets.get((member.target_parent_id, member.target_name)),
                )
                if target is not None
            ):
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.FAILED,
                    "plan_prerequisites_changed",
                    now=now,
                )
            if result.status is OrganizationStepCheck.CONFLICT:
                if result.error_code == "group_partial_state":
                    return await self._finish(
                        context,
                        OrganizationExecutionStatus.UNCERTAIN,
                        "outcome_unknown",
                        now=now,
                    )
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.FAILED,
                    "plan_prerequisites_changed",
                    now=now,
                )
            if result.status is OrganizationStepCheck.UNCERTAIN:
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.UNCERTAIN,
                    "outcome_unknown",
                    now=now,
                )
            if result.status is OrganizationStepCheck.ALREADY_APPLIED:
                context.completed_steps += 1
                continue
            try:
                for member in step.members:
                    self._check_cancel(cancel_event)
                    if not await self._renew(context, now=now):
                        return await self._handle_lease_loss(context, now=now)
                    self._check_cancel(cancel_event)
                    move = await self._call(
                        context,
                        self._transport.move,
                        member.object_id,
                        member.target_parent_id,
                        cancel_event=cancel_event,
                        now=now,
                        is_write=True,
                    )
                    self._check_write_result(move, OrganizationTransportOperation.MOVE)
                    context.write_confirmed = True
                    self._check_cancel(cancel_event)
                    if not await self._renew(context, now=now):
                        return await self._handle_lease_loss(context, now=now)
                    self._check_cancel(cancel_event)
                    rename = await self._call(
                        context,
                        self._transport.rename,
                        member.object_id,
                        member.target_name,
                        cancel_event=cancel_event,
                        now=now,
                        is_write=True,
                    )
                    self._check_write_result(
                        rename, OrganizationTransportOperation.RENAME
                    )
                    context.write_confirmed = True
            except _RateLimitReached:
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.UNCERTAIN,
                    "rate_limited",
                    now=now,
                )
            except _ExecutionCancelled:
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.CANCELLED,
                    "cancelled",
                    now=now,
                )
            except _LeaseLost:
                return await self._handle_lease_loss(context, now=now)
            except _TransportFailure as failure:
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.UNCERTAIN
                    if failure.uncertain or context.write_confirmed
                    else OrganizationExecutionStatus.FAILED,
                    failure.error_code,
                    now=now,
                )
            observed: dict[str, RemoteObjectState | None] = {}
            try:
                for member in step.members:
                    self._check_cancel(cancel_event)
                    if not await self._renew(context, now=now):
                        return await self._handle_lease_loss(context, now=now)
                    self._check_cancel(cancel_event)
                    observed[member.object_id] = await self._call(
                        context,
                        self._transport.read_object,
                        member.object_id,
                        cancel_event=cancel_event,
                        now=now,
                    )
            except _RateLimitReached:
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.UNCERTAIN,
                    "rate_limited",
                    now=now,
                )
            except _ExecutionCancelled:
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.CANCELLED,
                    "cancelled",
                    now=now,
                )
            except _LeaseLost:
                return await self._handle_lease_loss(context, now=now)
            except _TransportFailure as failure:
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.UNCERTAIN,
                    failure.error_code,
                    now=now,
                )
            postcondition = check_group_after_write(group, observed=observed)
            if postcondition.status is OrganizationStepCheck.CONFLICT:
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.UNCERTAIN,
                    "postcondition_mismatch",
                    now=now,
                )
            if postcondition.status is not OrganizationStepCheck.ALREADY_APPLIED:
                return await self._finish(
                    context,
                    OrganizationExecutionStatus.UNCERTAIN,
                    "outcome_unknown",
                    now=now,
                )
            context.completed_steps += 1
        first = steps[0].members[0]
        return await self._finish(
            context,
            OrganizationExecutionStatus.ORGANIZED,
            None,
            source_directory_id=first.source_parent_id,
            target_directory_id=first.target_parent_id,
            directory_ids=all_scope_ids,
            now=now,
        )

    async def _renew(self, context: _ExecutionContext, *, now) -> bool:
        try:
            context.lease = await self._operation_service.renew_lease(
                context.lease.operation_id,
                expected_revision=context.lease.revision,
                lease_token=context.lease.lease_token,
                now=now,
            )
        except OrganizationOperationLeaseUnavailable as error:
            if str(error) == "plan_revision_changed":
                context.plan_revision_changed = True
            return False
        return True

    async def _call(
        self,
        context: _ExecutionContext,
        method,
        *args,
        cancel_event: asyncio.Event | None,
        now,
        is_write: bool = False,
    ):
        if context.transport_calls >= self._max_transport_calls:
            raise _RateLimitReached
        interval = self._write_interval if is_write else self._min_call_interval
        if context.last_call_at is not None and interval:
            elapsed = time.monotonic() - context.last_call_at
            if elapsed < interval:
                await self._sleep(interval - elapsed)
        self._check_cancel(cancel_event)
        if await self._operation_service.cancel_requested(context.lease.operation_id):
            raise _ExecutionCancelled
        if not await self._renew(context, now=now):
            raise _LeaseLost
        self._check_cancel(cancel_event)
        if is_write:
            context.write_started = True
        context.transport_calls += 1
        context.last_call_at = time.monotonic()
        try:
            result = await method(*args)
            if await self._operation_service.cancel_requested(
                context.lease.operation_id
            ):
                raise _ExecutionCancelled
            return result
        except asyncio.CancelledError:
            raise
        except _ExecutionCancelled:
            raise
        except TimeoutError:
            raise _TransportFailure("timeout") from None
        except Exception:  # noqa: BLE001 - transport details stay private
            raise _TransportFailure("outcome_unknown") from None

    def _check_cancel(self, cancel_event: asyncio.Event | None) -> None:
        if _is_cancelled(cancel_event):
            raise _ExecutionCancelled

    async def _handle_lease_loss(
        self, context: _ExecutionContext, *, now
    ) -> OrganizationExecutionResult:
        if context.plan_revision_changed:
            return OrganizationExecutionResult(
                context.lease.operation_id,
                OrganizationExecutionStatus.UNCERTAIN,
                "plan_prerequisites_changed",
                context.completed_steps,
                context.transport_calls,
            )
        if not context.write_started:
            return self._lease_lost(context)
        return await self._persist_lease_loss(context, now=now)

    async def _persist_lease_loss(
        self, context: _ExecutionContext, *, now
    ) -> OrganizationExecutionResult:
        try:
            await self._operation_service.finish_after_lease_loss(
                context.lease.operation_id,
                expected_revision=context.lease.revision,
                lease_token=context.lease.lease_token,
                now=now,
            )
        except OrganizationOperationLeaseUnavailable:
            return self._lease_lost(context)
        return OrganizationExecutionResult(
            context.lease.operation_id,
            OrganizationExecutionStatus.UNCERTAIN,
            "lease_lost",
            context.completed_steps,
            context.transport_calls,
        )

    def _check_write_result(
        self,
        result: OrganizationTransportResult,
        expected: OrganizationTransportOperation,
    ) -> None:
        if (
            not isinstance(result, OrganizationTransportResult)
            or result.operation is not expected
        ):
            raise _TransportFailure("outcome_unknown")
        if result.status is OrganizationTransportStatus.SUCCESS:
            return
        if result.status is OrganizationTransportStatus.UNCERTAIN:
            code = _safe_transport_code(result.error_code)
            raise _TransportFailure(code or "outcome_unknown")
        raise _TransportFailure("remote_write_failed", uncertain=False)

    async def _finish(
        self,
        context: _ExecutionContext,
        status: OrganizationExecutionStatus,
        error_code: str | None,
        *,
        source_directory_id: str | None = None,
        target_directory_id: str | None = None,
        directory_ids: set[str] | None = None,
        now,
    ) -> OrganizationExecutionResult:
        # 兜底:任何不在合法集合的 error_code 都映射为 outcome_unknown,
        # 避免 finish 的 _validate_error_code 抛 ValueError 冒泡成 lease_lost。
        error_code = _safe_error_code(error_code)
        try:
            if status is OrganizationExecutionStatus.ORGANIZED:
                await self._operation_service.complete_organized_with_dirty_events(
                    context.lease.operation_id,
                    expected_revision=context.lease.revision,
                    lease_token=context.lease.lease_token,
                    source_directory_id=source_directory_id,
                    target_directory_id=target_directory_id,
                    directory_ids=directory_ids,
                    now=now,
                )
            else:
                operation_status = (
                    OrganizationOperationStatus.CANCELLED
                    if status is OrganizationExecutionStatus.CANCELLED
                    and not context.write_started
                    else (
                        OrganizationOperationStatus.UNCERTAIN
                        if status
                        in {
                            OrganizationExecutionStatus.UNCERTAIN,
                            OrganizationExecutionStatus.CANCELLED,
                        }
                        else OrganizationOperationStatus.FAILED
                    )
                )
                await self._operation_service.finish(
                    context.lease.operation_id,
                    expected_revision=context.lease.revision,
                    lease_token=context.lease.lease_token,
                    status=operation_status,
                    error_code=error_code,
                    now=now,
                )
        except OrganizationOperationLeaseUnavailable:
            if context.write_started:
                return await self._persist_lease_loss(context, now=now)
            return self._lease_lost(context)
        except OrganizationOperationConflict:
            if status is OrganizationExecutionStatus.ORGANIZED:
                try:
                    await self._operation_service.finish(
                        context.lease.operation_id,
                        expected_revision=context.lease.revision,
                        lease_token=context.lease.lease_token,
                        status=OrganizationOperationStatus.FAILED,
                        error_code="local_failure",
                        now=now,
                    )
                except OrganizationOperationLeaseUnavailable:
                    return self._lease_lost(context)
                status = OrganizationExecutionStatus.FAILED
                error_code = "local_failure"
            else:
                return OrganizationExecutionResult(
                    context.lease.operation_id,
                    OrganizationExecutionStatus.FAILED,
                    "local_failure",
                    context.completed_steps,
                    context.transport_calls,
                )
        return OrganizationExecutionResult(
            context.lease.operation_id,
            status,
            error_code,
            context.completed_steps,
            context.transport_calls,
        )

    def _lease_lost(self, context: _ExecutionContext) -> OrganizationExecutionResult:
        return OrganizationExecutionResult(
            context.lease.operation_id,
            OrganizationExecutionStatus.LEASE_LOST,
            "lease_lost",
            context.completed_steps,
            context.transport_calls,
        )


def _group_for_step(step: OrganizationPlanExecutionStep):
    expectations = tuple(
        OrganizationStepExpectation(
            object_id=member.object_id,
            source_parent_id=member.source_parent_id,
            source_name=member.source_name,
            target_parent_id=member.target_parent_id,
            target_name=member.target_name,
        )
        for member in step.members
    )
    return resolve_organization_companion_group(
        expectations[0], companions=expectations[1:]
    )


def _is_cancelled(cancel_event: asyncio.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _safe_transport_code(value: object) -> str | None:
    if value in {"timeout", "outcome_unknown", "remote_write_failed"}:
        return value
    return None


def _safe_error_code(value: str | None) -> str | None:
    if value is None or value in VALID_OPERATION_ERROR_CODES:
        return value
    return "outcome_unknown"


__all__ = [
    "OrganizationExecutionResult",
    "OrganizationExecutionStatus",
    "OrganizationExecutor",
    "OrganizationExecutorError",
    "OrganizationExecutorTransport",
    "OrganizationTransportOperation",
    "OrganizationTransportResult",
    "OrganizationTransportStatus",
]

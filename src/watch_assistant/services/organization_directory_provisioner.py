"""Create only the missing directories required by an explicit organization run."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import PurePosixPath

from watch_assistant.adapters.p115_c03_live_transport import (
    P115C03CallExecutor,
    P115C03LiveTransport,
)
from watch_assistant.adapters.p115_library_write_contract import (
    OrganizationWriteGate,
    P115OrganizationContract,
    WriteOperation,
    WriteStatus,
    evaluate_organization_write_gate,
    prepare_mkdir,
)


class OrganizationDirectoryProvisionError(ValueError):
    """Stable local error for a failed directory provision step."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OrganizationDirectoryProvisioner:
    """Provision target directories after a user explicitly starts organizing."""

    def __init__(
        self,
        client,
        *,
        call_executor: P115C03CallExecutor,
        organization_contract: P115OrganizationContract | None = None,
        event_logger: object | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._client = client
        self._transport = P115C03LiveTransport(
            client,
            call_executor=call_executor,
        )
        self._timeout_seconds = timeout_seconds
        self._organization_contract = organization_contract or P115OrganizationContract()
        self._event_logger = event_logger

    async def ensure(
        self,
        *,
        target_root_id: str,
        existing_directories: Mapping[str, str],
        paths: Collection[str],
        write_enabled: bool = False,
        plan_confirmed: bool = False,
        scope_confirmed: bool = False,
        lease_active: bool = False,
    ) -> None:
        if lease_active is not True:
            raise OrganizationDirectoryProvisionError("organization_lease_required")
        decision = evaluate_organization_write_gate(
            OrganizationWriteGate(
                write_enabled=write_enabled is True,
                plan_confirmed=plan_confirmed,
                scope_confirmed=scope_confirmed,
                contract=self._organization_contract,
            ),
            WriteOperation.MKDIR,
        )
        if not decision.allowed:
            raise OrganizationDirectoryProvisionError(
                decision.error_code or "capability_unverified"
            )
        directory_ids = dict(existing_directories)
        directory_ids.setdefault("", target_root_id)
        normalized_paths = sorted(
            {
                _normalize_directory_path(path)
                for path in paths
                if _normalize_directory_path(path) is not None
            },
            key=lambda value: (value.count("/"), value),
        )
        for path in normalized_paths:
            parts = path.split("/")
            parent_path = ""
            for part in parts:
                current_path = "/".join(item for item in (parent_path, part) if item)
                if current_path in directory_ids:
                    parent_path = current_path
                    continue
                parent_id = directory_ids.get(parent_path)
                if parent_id is None:
                    raise OrganizationDirectoryProvisionError(
                        "target_directory_parent_missing"
                    )
                receipt = await self._transport.execute(
                    prepare_mkdir(parent_id, part),
                    timeout_seconds=self._timeout_seconds,
                )
                if receipt.status is not WriteStatus.SUCCESS or not receipt.file_id:
                    raise OrganizationDirectoryProvisionError(
                        "target_directory_create_failed"
                    )
                directory_ids[current_path] = receipt.file_id
                parent_path = current_path
        await self._log_provisioned(len(normalized_paths))

    async def _log_provisioned(self, count: int) -> None:
        method = getattr(self._event_logger, "log_event", None)
        if callable(method):
            await method(
                "organize.directory.provisioned",
                counts={"count": count},
            )


def _normalize_directory_path(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    normalized = "/".join(path.parts)
    return normalized if normalized and len(normalized) <= 4096 else None


__all__ = [
    "OrganizationDirectoryProvisionError",
    "OrganizationDirectoryProvisioner",
]

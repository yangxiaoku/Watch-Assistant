"""Runtime capability checks for confirmed organization execution."""

from __future__ import annotations

from watch_assistant.services.organization_worker import OrganizationWorker


def organization_execution_supported(application: object) -> bool:
    """Return true only when the gated organization worker is actually present."""

    state = getattr(application, "state", application)
    return isinstance(getattr(state, "organization_worker", None), OrganizationWorker)


__all__ = ["organization_execution_supported"]

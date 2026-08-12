"""Data models for offline organization plan previews.

Split from organization_plan (phase D): the model dataclasses are imported
by both the service and the payload/validation helpers, so they live below
both to avoid an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from watch_assistant.services.media_classification import NamingPlan
from watch_assistant.services.media_matcher import MatchDecision
from watch_assistant.services.organization_policy import (
    VersionDecision,
    VersionEvidence,
)


class OrganizationPlanStatus(StrEnum):
    NEEDS_REVIEW = "needs_review"
    PLANNED = "planned"
    INVALIDATED = "invalidated"
    IGNORED = "ignored"

class OrganizationPlanError(ValueError):
    """Stable local error without remote values or exception details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

@dataclass(frozen=True, slots=True, repr=False)
class PlanSource:
    """A caller-verified stable source observation for one planned object."""

    object_type: str
    object_id: str
    parent_id: str
    path: str
    remote_version: str
    is_directory: bool = False

    def __repr__(self) -> str:
        return "PlanSource(object_type=<redacted>, object_id=<redacted>)"

@dataclass(frozen=True, slots=True, repr=False)
class OrganizationPlanCompanion:
    """Optional companion source and its independently verified target."""

    source: PlanSource
    target_parent_id: str | None = None
    target_name: str | None = None

    def __repr__(self) -> str:
        return "OrganizationPlanCompanion(source=<redacted>, target=<redacted>)"

@dataclass(frozen=True, slots=True, repr=False)
class OrganizationPlanItem:
    source: PlanSource
    naming_plan: NamingPlan
    decision: MatchDecision
    target_parent_id: str | None = None
    target_name: str | None = None
    companions: tuple[OrganizationPlanCompanion, ...] = ()
    policy_decision: VersionDecision | None = None
    policy_evidence: VersionEvidence | None = None
    existing_evidence: VersionEvidence | None = None
    replacement_object_id: str | None = None
    replacement_parent_id: str | None = None
    replacement_name: str | None = None
    target_directory_path: str | None = None

    def __repr__(self) -> str:
        return "OrganizationPlanItem(source=<redacted>, naming_plan=<redacted>)"

@dataclass(frozen=True, slots=True)
class OrganizationExecutionBlocker:
    kind: str
    code: str
    message_zh: str
    next_step_zh: str

    def to_public_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "code": self.code,
            "message_zh": self.message_zh,
            "next_step_zh": self.next_step_zh,
        }

@dataclass(frozen=True, slots=True, repr=False)
class OrganizationPlanView:
    plan_id: str
    plan_hash: str
    status: OrganizationPlanStatus
    revision: int
    expires_at: datetime
    source_count: int
    action_count: int
    precondition_count: int
    executable_action_count: int
    review_action_count: int
    can_execute: bool
    execution_blockers: tuple[OrganizationExecutionBlocker, ...] = ()
    source_names: tuple[str, ...] = ()
    alias: str | None = None
    candidates: tuple[dict[str, object], ...] = ()

    def __repr__(self) -> str:
        return (
            "OrganizationPlanView(plan_id=<redacted>, plan_hash=<redacted>, "
            f"status={self.status.value!r}, revision={self.revision}, "
            f"source_count={self.source_count}, action_count={self.action_count}, "
            f"precondition_count={self.precondition_count}, alias=<redacted>)"
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "status": self.status.value,
            "revision": self.revision,
            "expires_at": self.expires_at.isoformat(),
            "source_count": self.source_count,
            "action_count": self.action_count,
            "precondition_count": self.precondition_count,
            "executable_action_count": self.executable_action_count,
            "review_action_count": self.review_action_count,
            "can_execute": self.can_execute,
            "execution_blockers": [
                blocker.to_public_dict() for blocker in self.execution_blockers
            ],
            "source_names": list(self.source_names),
            "alias": self.alias,
            "candidates": list(self.candidates),
        }

@dataclass(frozen=True, slots=True, repr=False)
class OrganizationPlanExecutionMember:
    object_type: str
    object_id: str
    source_parent_id: str
    source_path: str
    source_name: str
    source_version: str
    target_parent_id: str
    target_name: str
    target_directory_path: str | None = None

    def __repr__(self) -> str:
        return "OrganizationPlanExecutionMember(<redacted>)"

@dataclass(frozen=True, slots=True, repr=False)
class OrganizationPlanExecutionStep:
    order: int
    kind: str
    scope_directory_ids: tuple[str, ...]
    members: tuple[OrganizationPlanExecutionMember, ...]
    replacement_object_id: str | None = None
    replacement_parent_id: str | None = None
    replacement_name: str | None = None

    def __repr__(self) -> str:
        return (
            "OrganizationPlanExecutionStep(order="
            f"{self.order}, kind={self.kind!r}, member_count={len(self.members)})"
        )


__all__ = ["OrganizationExecutionBlocker", "OrganizationPlanCompanion", "OrganizationPlanError", "OrganizationPlanExecutionMember", "OrganizationPlanExecutionStep", "OrganizationPlanItem", "OrganizationPlanStatus", "OrganizationPlanView", "PlanSource"]

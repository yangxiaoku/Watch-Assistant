"""Payload builders, validation, and executable-step helpers for org plans.

Split from organization_plan (phase D): module-level helpers that do not
touch OrganizationPlanService instance state.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from watch_assistant.library_models import (
    LibraryScanCheckpoint,
    LibraryScanEntry,
    LibraryScanRun,
    MediaLibrary,
    OrganizationPlan,
)
from watch_assistant.services.identity import safe_identity as _safe_identity
from watch_assistant.services.library_index import (
    LibraryIndexError,
    ScanRunState,
    validate_complete_scan_evidence,
)
from watch_assistant.services.media_classification import (
    ClassificationStatus,
    NamingPlan,
)
from watch_assistant.services.media_matcher import (
    MatchConfidence,
    MatchDecision,
    MatchStatus,
    TmdbCandidate,
)
from watch_assistant.services.organization_plan_models import (
    OrganizationExecutionBlocker,
    OrganizationPlanCompanion,
    OrganizationPlanError,
    OrganizationPlanExecutionMember,
    OrganizationPlanExecutionStep,
    OrganizationPlanItem,
    OrganizationPlanStatus,
    OrganizationPlanView,
)
from watch_assistant.services.organization_policy import (
    OrganizationConflictPolicy,
    VersionDecision,
    VersionEvidence,
)
from watch_assistant.services.strm_scope import source_snapshot_is_current


def _build_payload(
    items: Sequence[OrganizationPlanItem],
    rows: Mapping[tuple[str, str], LibraryScanEntry],
    *,
    root_directory_id: str,
    target_root: str,
    target_directory_id: str | None,
    target_directories: Mapping[str, str],
    target_conflicts: Iterable[str],
    organization_policy: Mapping[str, object],
    source_snapshot_revision: int | None,
    parser_version: str,
    matcher_version: str,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    OrganizationPlanStatus,
]:
    policy = OrganizationConflictPolicy.from_mapping(organization_policy).to_dict()
    conflicts = {
        _normalize_target(_validate_relative_path(value)) for value in target_conflicts
    }
    source_snapshot: list[dict[str, object]] = []
    actions: list[dict[str, object]] = []
    preconditions: list[dict[str, object]] = []
    basis: list[dict[str, object]] = []
    targets: dict[str, int] = {}
    directory_rows: dict[str, list[LibraryScanEntry]] = {}
    for row in rows.values():
        if row.is_directory:
            directory_rows.setdefault(row.object_id, []).append(row)
    planned = True
    for index, item in enumerate(items):
        source = item.source
        key = (source.object_type, source.object_id)
        if key not in rows:
            raise OrganizationPlanError("source_snapshot_mismatch")
        target = _target_path(target_root, item.naming_plan.target_path)
        if (
            target is not None
            and item.target_parent_id == target_directory_id
            and _directory_id_for_path(
                target_directories,
                str(PurePosixPath(item.naming_plan.target_path).parent),
            )
            is None
        ):
            # 目标分类子目录尚不存在,执行会回退到目标根目录
            # (provisioner 仅创建缺失子目录供后续归档)。预览、确认、
            # 历史必须按实际落点展示,否则用户按完整路径找不到文件。
            target = _validate_relative_path(
                f"{target_root}/{item.target_name}"
                if target_root
                else item.target_name
            )
        source_snapshot.append(
            {
                "object_type": source.object_type,
                "object_id": source.object_id,
                "parent_id": source.parent_id,
                "path": source.path,
                "name": rows[key].name,
                "remote_version": source.remote_version,
                "is_directory": source.is_directory,
            }
        )
        preconditions.append(
            {
                "source_index": index,
                "object_type": source.object_type,
                "object_id": source.object_id,
                "parent_id": source.parent_id,
                "source_path": source.path,
                "source_name": rows[key].name,
                "remote_version": source.remote_version,
                "source_snapshot_revision": source_snapshot_revision,
                "target": target,
                "target_parent_id": item.target_parent_id,
                "target_name": item.target_name,
                "rule_version": item.naming_plan.rule_version,
                "parser_version": parser_version,
                "matcher_version": matcher_version,
                "organization_policy": policy,
            }
        )
        execution = _execution_payload(
            item,
            companions=rows,
            directory_rows=directory_rows,
            root_directory_id=root_directory_id,
            target_directory_id=target_directory_id,
            target_directories=target_directories,
            target=target,
            order=index,
        )
        preconditions[-1]["execution"] = execution
        actions.append(
            {
                "order": index,
                "kind": "move" if target is not None else "review",
                "object_type": source.object_type,
                "object_id": source.object_id,
                "source_parent_id": source.parent_id,
                "source_path": source.path,
                "source_name": rows[key].name,
                "source_version": source.remote_version,
                "target_parent_id": item.target_parent_id,
                "target_name": item.target_name,
                "target": target,
                "target_directory_path": item.target_directory_path,
                "execution": execution,
            }
        )
        if execution is not None:
            source_snapshot[-1]["companions"] = [
                {
                    "object_type": member["object_type"],
                    "object_id": member["object_id"],
                    "parent_id": member["source_parent_id"],
                    "path": member["source_path"],
                    "name": member["source_name"],
                    "remote_version": member["source_version"],
                    "is_directory": False,
                }
                for member in execution["members"][1:]
            ]
        if target is not None:
            targets[_normalize_target(target)] = (
                targets.get(_normalize_target(target), 0) + 1
            )
        accepted = (
            item.naming_plan.status is ClassificationStatus.PLANNED
            and item.decision.status is MatchStatus.ACCEPTED
            and item.decision.confidence is MatchConfidence.HIGH
            and item.decision.selected is not None
            and target is not None
            and execution is not None
        )
        if not accepted:
            planned = False
            actions[-1]["kind"] = "review"
        reasons = tuple(
            _safe_reason(reason.value if hasattr(reason, "value") else reason)
            for reason in item.naming_plan.reasons
        )
        basis.append(
            {
                "source_index": index,
                "source_object_id": source.object_id,
                "tmdb_id": (
                    item.decision.selected.tmdb_id
                    if item.decision.selected is not None
                    else None
                ),
                "media_type": (
                    item.decision.selected.media_type.value
                    if item.decision.selected is not None
                    else None
                ),
                "candidates": [
                    {
                        "tmdb_id": ranked.candidate.tmdb_id,
                        "title": ranked.candidate.title,
                        "media_type": ranked.candidate.media_type.value,
                        "release_year": ranked.candidate.release_year,
                        "original_title": ranked.candidate.original_title,
                        "origin_countries": list(ranked.candidate.origin_countries),
                        "kind": ranked.candidate.kind.value,
                        "special_kind": ranked.candidate.special_kind.value,
                        "seasons": [
                            {
                                "season_number": season.season_number,
                                "episode_count": season.episode_count,
                                "episode_numbers": list(season.episode_numbers),
                            }
                            for season in ranked.candidate.seasons
                        ],
                    }
                    for ranked in item.decision.ranked_candidates[:8]
                ],
                "title": (
                    item.decision.selected.title
                    if item.decision.selected is not None
                    else None
                ),
                "classification_status": item.naming_plan.status.value,
                "match_status": item.decision.status.value,
                "match_confidence": (
                    item.decision.confidence.value
                    if item.decision.confidence is not None
                    else None
                ),
                "accepted": accepted,
                "reasons": reasons,
                "policy": (
                    item.policy_decision.to_dict()
                    if item.policy_decision is not None
                    else None
                ),
                "policy_evidence": (
                    item.policy_evidence.to_dict()
                    if item.policy_evidence is not None
                    else None
                ),
                "existing_evidence": (
                    item.existing_evidence.to_dict()
                    if item.existing_evidence is not None
                    else None
                ),
            }
        )
    if any(count > 1 for count in targets.values()) or conflicts & set(targets):
        planned = False
    conflict_keys = conflicts | {key for key, count in targets.items() if count > 1}
    for action, precondition in zip(actions, preconditions, strict=True):
        target = action["target"]
        is_conflict = (
            isinstance(target, str) and _normalize_target(target) in conflict_keys
        )
        action["kind"] = "review" if is_conflict else action["kind"]
        precondition["target_conflict"] = is_conflict
    status = (
        OrganizationPlanStatus.PLANNED
        if planned
        else OrganizationPlanStatus.NEEDS_REVIEW
    )
    source_snapshot.sort(key=_source_key)
    actions.sort(key=lambda value: int(value["order"]))
    preconditions.sort(key=lambda value: int(value["source_index"]))
    basis.sort(key=lambda value: int(value["source_index"]))
    return source_snapshot, actions, preconditions, basis, status

def _target_set(plan: OrganizationPlan) -> set[str]:
    actions = _load_json_list(plan.actions_json)
    return {
        _normalize_target(action["target"])
        for action in actions
        if isinstance(action, dict) and isinstance(action.get("target"), str)
    }

def _execution_payload(
    item: OrganizationPlanItem,
    *,
    companions: Mapping[tuple[str, str], LibraryScanEntry],
    directory_rows: Mapping[str, Sequence[LibraryScanEntry]],
    root_directory_id: str,
    target_directory_id: str | None,
    target_directories: Mapping[str, str],
    target: str | None,
    order: int,
) -> dict[str, object] | None:
    if target is None or not _safe_identity(root_directory_id):
        return None
    target_parent_id = item.target_parent_id
    target_name = item.target_name
    if not _valid_target_name(target_name) or not _safe_identity(target_parent_id):
        return None
    if target_name != PurePosixPath(target).name:
        return None
    if not _target_directory_matches(
        target,
        target_parent_id=target_parent_id,
        directory_rows=directory_rows,
        root_directory_id=root_directory_id,
        target_directory_id=target_directory_id,
        target_directories=target_directories,
    ):
        return None
    members: list[dict[str, str]] = []
    member_keys: set[tuple[str, str]] = set()
    all_sources = (
        (item.source, target_parent_id, target_name),
        *(
            (companion.source, companion.target_parent_id, companion.target_name)
            for companion in item.companions
        ),
    )
    for source, member_target_parent, member_target_name in all_sources:
        key = (source.object_type, source.object_id)
        member_row = companions.get(key)
        if (
            key in member_keys
            or member_row is None
            or member_row.is_directory
            or not _safe_identity(source.object_type)
            or not _safe_identity(source.object_id)
            or not _safe_identity(source.parent_id)
            or not isinstance(source.remote_version, str)
            or not source.remote_version
            or not _valid_source_version(source.remote_version)
            or not _safe_identity(member_target_parent)
            or not _valid_target_name(member_target_name)
            or member_target_parent != target_parent_id
            or not _valid_source_path(source.path)
            or not _valid_target_name(member_row.name)
            or not _valid_source_path(member_row.path)
            or member_row.parent_id != source.parent_id
            or member_row.path != source.path
        ):
            return None
        if not _source_parent_is_managed(
            member_row.parent_id, directory_rows, root_directory_id
        ):
            return None
        member_keys.add(key)
        members.append(
            {
                "object_type": source.object_type,
                "object_id": source.object_id,
                "source_parent_id": member_row.parent_id,
                "source_path": member_row.path,
                "source_name": member_row.name,
                "source_version": source.remote_version,
                "target_parent_id": member_target_parent,
                "target_name": member_target_name,
                # 同一步骤的所有成员(主文件与伴生文件)落在同一目标目录。
                "target_directory_path": item.target_directory_path,
            }
        )
    scope_directory_ids = {
        root_directory_id,
        *(member["source_parent_id"] for member in members),
        *(member["target_parent_id"] for member in members),
    }
    if target_directory_id is not None:
        scope_directory_ids.add(target_directory_id)
    payload: dict[str, object] = {
        "order": order,
        "kind": "move",
        "scope_directory_ids": sorted(scope_directory_ids),
        "members": members,
    }
    replacement_values = (
        item.replacement_object_id,
        item.replacement_parent_id,
        item.replacement_name,
    )
    if any(value is not None for value in replacement_values):
        if (
            item.policy_decision is None
            or item.policy_decision.outcome != "candidate"
            or not _safe_identity(item.replacement_object_id)
            or not _safe_identity(item.replacement_parent_id)
            or not _valid_target_name(item.replacement_name)
            or item.replacement_parent_id != target_parent_id
            or item.replacement_name != target_name
            or item.replacement_object_id in {member["object_id"] for member in members}
        ):
            return None
        payload["replacement"] = {
            "object_id": item.replacement_object_id,
            "parent_id": item.replacement_parent_id,
            "name": item.replacement_name,
        }
    return payload

def _target_directory_matches(
    target: str,
    *,
    target_parent_id: str,
    directory_rows: Mapping[str, Sequence[LibraryScanEntry]],
    root_directory_id: str,
    target_directory_id: str | None,
    target_directories: Mapping[str, str],
) -> bool:
    parent_path = PurePosixPath(target).parent
    if str(parent_path) == ".":
        return target_parent_id in {root_directory_id, target_directory_id}
    mapped = target_directories.get(_index_path(str(parent_path)))
    if mapped is not None:
        return mapped == target_parent_id
    # The category sub-directory may not exist yet (fresh/empty target).  When
    # the plan pinned the target root as the immediate parent, the directory
    # provisioner creates the sub-directories at execution time.
    if target_parent_id == target_directory_id:
        return True
    directories = directory_rows.get(target_parent_id, ())
    if len(directories) != 1:
        return False
    directory = directories[0]
    if not isinstance(directory.path, str):
        return False
    return _index_path(directory.path) == _index_path(str(parent_path))

def _source_parent_is_managed(
    parent_id: str,
    directory_rows: Mapping[str, Sequence[LibraryScanEntry]],
    root_directory_id: str,
) -> bool:
    return parent_id == root_directory_id or len(directory_rows.get(parent_id, ())) == 1

def _index_path(value: str) -> str:
    return value.strip("/").replace("\\", "/")

def _valid_target_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 4096
        and value not in {".", ".."}
        and "\x00" not in value
        and "/" not in value
        and "\\" not in value
    )

def _valid_target_path(value: object) -> bool:
    """Validate a multi-segment target directory path (e.g. ``movie/Season 02``).

    ``_valid_target_name`` only validates a single segment; this validates a
    "/"-separated relative path where every segment is a safe file name.
    """

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\x00" in value
        or "\\" in value
    ):
        return False
    segments = value.split("/")
    return bool(segments) and all(_valid_target_name(segment) for segment in segments)

def _replacement_from_execution(
    value: object,
) -> tuple[str | None, str | None, str | None]:
    if value is None:
        return None, None, None
    if not isinstance(value, dict):
        return ("<invalid>", None, None)
    return value.get("object_id"), value.get("parent_id"), value.get("name")

async def load_executable_steps(
    session_factory: async_sessionmaker[AsyncSession],
    plan: OrganizationPlan | str,
    *,
    allow_unconfirmed: bool = False,
    expected_plan_revision: int | None = None,
) -> tuple[OrganizationPlanExecutionStep, ...] | None:
    """Load executable steps only after revalidating durable plan state."""

    plan_id = plan if isinstance(plan, str) else getattr(plan, "id", None)
    if not _safe_identity(plan_id):
        return None
    async with session_factory() as session:
        stored = await session.get(OrganizationPlan, plan_id)
        if stored is None or (
            not allow_unconfirmed
            and stored.status != OrganizationPlanStatus.PLANNED.value
        ):
            return None
        if (
            expected_plan_revision is not None
            and stored.revision != expected_plan_revision
        ):
            return None
        library = await session.get(MediaLibrary, stored.library_id)
        run = await session.get(LibraryScanRun, stored.source_scan_run_id)
        if (
            library is None
            or not library.enabled
            or not library.scope_verified
            or run is None
            or run.library_id != stored.library_id
            or run.root_directory_id != library.root_directory_id
            or run.state != ScanRunState.COMPLETED.value
            or not run.complete
            or run.snapshot_revision != stored.source_snapshot_revision
            or _utc(stored.expires_at) <= datetime.now(UTC)
        ):
            return None
        latest = await _latest_completed_scan(
            session,
            library_id=stored.library_id,
            root_directory_id=library.root_directory_id,
        )
        if (
            latest is None
            or not _scan_is_valid_for_library(library, latest)
            or not _plan_scan_binding_is_current(
                stored, library=library, run=run, latest=latest
            )
        ):
            return None
        source_snapshot = _load_source_snapshot(
            stored.source_snapshot_json, require_name=True
        )
        actions = _load_json_list(stored.actions_json)
        preconditions = _load_json_object(stored.preconditions_json)
        precondition_items = preconditions.get("items")
        try:
            target_directory_id = preconditions.get("target_directory_id")
            if target_directory_id is not None:
                _validate_identity(target_directory_id, "invalid_target_directory")
            target_directories = _validate_target_directories(
                preconditions.get("target_directories", {})
            )
        except OrganizationPlanError:
            return None
        if (
            source_snapshot is None
            or not actions
            or preconditions.get("library") != _library_snapshot(library)
            or not isinstance(precondition_items, list)
            or len(precondition_items) != len(actions)
        ):
            return None
        checkpoint = await session.get(LibraryScanCheckpoint, run.id)
        all_entries = list(
            (
                await session.scalars(
                    select(LibraryScanEntry).where(
                        LibraryScanEntry.scan_run_id == run.id
                    )
                )
            ).all()
        )
        try:
            validate_complete_scan_evidence(
                run,
                checkpoint,
                all_entries,
                root_directory_id=library.root_directory_id,
                require_tree=True,
            )
        except LibraryIndexError:
            return None
        steps = _parse_executable_steps(stored, allow_unconfirmed=allow_unconfirmed)
        if steps is None:
            return None
        rows = {
            (row.object_type, row.object_id): row
            for row in all_entries
        }
        if not _validate_persisted_execution(
            stored,
            library=library,
            run=run,
            source_snapshot=source_snapshot,
            actions=actions,
            preconditions=precondition_items,
            steps=steps,
            rows=rows,
            target_directory_id=target_directory_id,
            target_directories=target_directories,
        ):
            return None
        canonical = {
            "library_id": stored.library_id,
            "library_snapshot": _library_snapshot(library),
            "source_snapshot": source_snapshot,
            "target_root": stored.target_root,
            "target_directory_id": target_directory_id,
            "target_directories": target_directories,
            "actions": actions,
            "preconditions": preconditions,
            "rule_version": stored.rule_version,
            "parser_version": stored.parser_version,
            "matcher_version": stored.matcher_version,
        }
        if stored.plan_hash != _canonical_hash(canonical):
            return None
        return steps

def _parse_executable_steps(
    plan: OrganizationPlan,
    *,
    allow_unconfirmed: bool = False,
) -> tuple[OrganizationPlanExecutionStep, ...] | None:
    """Parse only the complete ``move`` payload used by a future executor.

    Review actions (``kind != "move"``, e.g. unrecognized junk files or
    low-confidence matches still waiting for human attention) are skipped
    instead of making the whole plan un-executable: execution covers the move
    actions only, while review actions remain in the plan for the user to
    handle.  ``execution.order`` is the global action index (0..N-1), so a
    step's order matches its action's position in ``actions`` even when
    review actions are skipped in between.

    Old previews remain readable, but their actions intentionally return
    ``None`` here because they do not carry the stable directory and member
    identities.
    """

    if not allow_unconfirmed and plan.status != OrganizationPlanStatus.PLANNED.value:
        return None
    actions = _load_json_list(plan.actions_json)
    if not actions:
        return None
    steps: list[OrganizationPlanExecutionStep] = []
    seen_members: set[tuple[str, str]] = set()
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            return None
        if action.get("kind") != "move":
            continue
        execution = action.get("execution")
        if not isinstance(execution, dict):
            return None
        order = execution.get("order")
        kind = execution.get("kind")
        scope = execution.get("scope_directory_ids")
        members = execution.get("members")
        if (
            isinstance(order, bool)
            or not isinstance(order, int)
            or order != index
            or kind != "move"
            or not isinstance(scope, list)
            or not scope
            or not all(_safe_identity(value) for value in scope)
            or len(scope) != len(set(scope))
            or scope != sorted(scope)
            or not isinstance(members, list)
            or not members
        ):
            return None
        replacement = execution.get("replacement")
        replacement_object_id = replacement_parent_id = replacement_name = None
        if replacement is not None:
            if not isinstance(replacement, dict):
                return None
            replacement_object_id = replacement.get("object_id")
            replacement_parent_id = replacement.get("parent_id")
            replacement_name = replacement.get("name")
            if (
                not _safe_identity(replacement_object_id)
                or not _safe_identity(replacement_parent_id)
                or not _valid_target_name(replacement_name)
                or replacement_parent_id != members[0].get("target_parent_id")
                or replacement_name != members[0].get("target_name")
            ):
                return None
        parsed_members: list[OrganizationPlanExecutionMember] = []
        for member in members:
            if not isinstance(member, dict):
                return None
            values = (
                member.get("object_type"),
                member.get("object_id"),
                member.get("source_parent_id"),
                member.get("source_path"),
                member.get("source_name"),
                member.get("source_version"),
                member.get("target_parent_id"),
                member.get("target_name"),
                member.get("target_directory_path"),
            )
            (
                object_type,
                object_id,
                source_parent,
                source_path,
                source_name,
                version,
                target_parent,
                target_name,
                target_directory_path,
            ) = values
            if (
                not _safe_identity(object_type)
                or not _safe_identity(object_id)
                or not _safe_identity(source_parent)
                or not _valid_source_path(source_path)
                or not _valid_target_name(source_name)
                or not _valid_source_version(version)
                or not _safe_identity(target_parent)
                or not _valid_target_name(target_name)
                or (
                    target_directory_path is not None
                    and not _valid_target_path(target_directory_path)
                )
            ):
                return None
            key = (object_type, object_id)
            if key in seen_members:
                return None
            seen_members.add(key)
            parsed_members.append(
                OrganizationPlanExecutionMember(
                    object_type=object_type,
                    object_id=object_id,
                    source_parent_id=source_parent,
                    source_path=source_path,
                    source_name=source_name,
                    source_version=version,
                    target_parent_id=target_parent,
                    target_name=target_name,
                    target_directory_path=target_directory_path,
                )
            )
        if (
            action.get("order") != index
            or action.get("object_type") != parsed_members[0].object_type
            or action.get("object_id") != parsed_members[0].object_id
            or not set(scope).issuperset(
                {member.source_parent_id for member in parsed_members}
                | {member.target_parent_id for member in parsed_members}
            )
        ):
            return None
        steps.append(
            OrganizationPlanExecutionStep(
                order=order,
                kind=kind,
                scope_directory_ids=tuple(scope),
                members=tuple(parsed_members),
                replacement_object_id=replacement_object_id,
                replacement_parent_id=replacement_parent_id,
                replacement_name=replacement_name,
            )
        )
    return tuple(steps)

def _validate_persisted_execution(
    plan: OrganizationPlan,
    *,
    library: MediaLibrary,
    run: LibraryScanRun,
    source_snapshot: list[dict[str, object]],
    actions: list[object],
    preconditions: list[object],
    steps: tuple[OrganizationPlanExecutionStep, ...],
    rows: Mapping[tuple[str, str], LibraryScanEntry],
    target_directory_id: str | None,
    target_directories: Mapping[str, str],
) -> bool:
    if len(source_snapshot) != len(actions) or len(steps) > len(actions):
        return False
    source_by_key = {
        (item["object_type"], item["object_id"]): item for item in source_snapshot
    }
    if len(source_by_key) != len(source_snapshot):
        return False
    move_orders = {
        index
        for index, action in enumerate(actions)
        if isinstance(action, dict) and action.get("kind") == "move"
    }
    # Every move action yields exactly one step (review actions are skipped),
    # and ``execution.order`` is the action's position in ``actions``.
    if len(steps) != len(move_orders) or {step.order for step in steps} != move_orders:
        return False
    steps_by_order = {step.order: step for step in steps}
    directory_rows: dict[str, list[LibraryScanEntry]] = {}
    for row in rows.values():
        if row.is_directory:
            directory_rows.setdefault(row.object_id, []).append(row)
    managed_directory_ids = {library.root_directory_id}
    managed_directory_ids.update(
        object_id for object_id, matches in directory_rows.items() if len(matches) == 1
    )
    try:
        _validate_relative_path(plan.target_root, allow_empty=True)
        _validate_version(plan.rule_version)
        _validate_version(plan.parser_version)
        _validate_version(plan.matcher_version)
        stored_policy = OrganizationConflictPolicy.from_mapping(
            _load_json_object(plan.preconditions_json).get("organization_policy")
        ).to_dict()
    except OrganizationPlanError:
        return False
    except ValueError:
        return False
    for index, (action, precondition) in enumerate(
        zip(actions, preconditions, strict=True)
    ):
        if not isinstance(action, dict) or not isinstance(precondition, dict):
            return False
        object_key = (action.get("object_type"), action.get("object_id"))
        snapshot = source_by_key.get(object_key)
        current_row = rows.get(object_key)
        execution = action.get("execution")
        replacement = execution.get("replacement") if isinstance(execution, dict) else None
        if (
            snapshot is None
            or precondition.get("source_index") != index
            or precondition.get("object_type") != action.get("object_type")
            or precondition.get("object_id") != action.get("object_id")
            or precondition.get("parent_id") != snapshot.get("parent_id")
            or precondition.get("source_path") != snapshot.get("path")
            or precondition.get("remote_version") != snapshot.get("remote_version")
            or action.get("source_parent_id") != snapshot.get("parent_id")
            or action.get("source_path") != snapshot.get("path")
            or action.get("source_name") != snapshot.get("name")
            or action.get("source_version") != snapshot.get("remote_version")
            or precondition.get("source_snapshot_revision")
            != plan.source_snapshot_revision
            or precondition.get("target") != action.get("target")
            or precondition.get("parent_id") != action.get("source_parent_id")
            or precondition.get("source_path") != action.get("source_path")
            or precondition.get("source_name") != action.get("source_name")
            or precondition.get("remote_version") != action.get("source_version")
            or precondition.get("target_parent_id") != action.get("target_parent_id")
            or precondition.get("target_name") != action.get("target_name")
            or precondition.get("rule_version") != plan.rule_version
            or precondition.get("parser_version") != plan.parser_version
            or precondition.get("matcher_version") != plan.matcher_version
            or precondition.get("organization_policy") != stored_policy
            or precondition.get("execution") != execution
            or action.get("order") != index
            or current_row is None
            or snapshot.get("name") != current_row.name
            or current_row.parent_id != snapshot.get("parent_id")
            or current_row.path != snapshot.get("path")
            or not _source_version_matches(snapshot.get("remote_version"), current_row)
            or current_row.is_directory is not False
        ):
            return False
        step = steps_by_order.get(index)
        if step is None:
            # Review action: nothing is executed for it, so it does not block
            # the remaining move actions (it stays in the plan for the user).
            continue
        if (
            precondition.get("target_conflict") is not False
            or action.get("kind") != "move"
            or not isinstance(execution, dict)
            or step.order != index
            or _replacement_from_execution(replacement)
            != (
                step.replacement_object_id,
                step.replacement_parent_id,
                step.replacement_name,
            )
        ):
            return False
        if not _validate_persisted_step(
            action,
            step=step,
            library=library,
            managed_directory_ids=managed_directory_ids,
            directory_rows=directory_rows,
            rows=rows,
            snapshot=snapshot,
            target_directory_id=target_directory_id,
            target_directories=target_directories,
        ):
            return False
    return True

def _validate_persisted_step(
    action: dict[str, object],
    *,
    step: OrganizationPlanExecutionStep,
    library: MediaLibrary,
    managed_directory_ids: set[str],
    directory_rows: Mapping[str, Sequence[LibraryScanEntry]],
    rows: Mapping[tuple[str, str], LibraryScanEntry],
    snapshot: dict[str, object],
    target_directory_id: str | None,
    target_directories: Mapping[str, str],
) -> bool:
    target = action.get("target")
    if not isinstance(target, str):
        return False
    try:
        _validate_relative_path(target)
    except OrganizationPlanError:
        return False
    if not step.members:
        return False
    primary = step.members[0]
    if (
        primary.object_type != action.get("object_type")
        or primary.object_id != action.get("object_id")
        or primary.source_parent_id != action.get("source_parent_id")
        or primary.source_path != action.get("source_path")
        or primary.source_name != action.get("source_name")
        or primary.source_version != action.get("source_version")
        or primary.target_parent_id != action.get("target_parent_id")
        or primary.target_name != action.get("target_name")
        or primary.target_name != PurePosixPath(target).name
        or primary.target_directory_path != action.get("target_directory_path")
        or not _target_directory_matches(
            target,
            target_parent_id=primary.target_parent_id,
            directory_rows=directory_rows,
            root_directory_id=library.root_directory_id,
            target_directory_id=target_directory_id,
            target_directories=target_directories,
        )
    ):
        return False
    companion_snapshot = snapshot.get("companions", [])
    if not isinstance(companion_snapshot, list):
        return False
    snapshot_members = [snapshot, *companion_snapshot]
    if len(snapshot_members) != len(step.members):
        return False
    if any(
        not isinstance(item, dict)
        or item.get("object_type") != member.object_type
        or item.get("object_id") != member.object_id
        or item.get("parent_id") != member.source_parent_id
        or item.get("path") != member.source_path
        or item.get("name") != member.source_name
        or item.get("remote_version") != member.source_version
        or item.get("is_directory") is not False
        for item, member in zip(snapshot_members, step.members, strict=True)
    ):
        return False
    expected_scope = {
        library.root_directory_id,
        *(member.source_parent_id for member in step.members),
        *(member.target_parent_id for member in step.members),
    }
    if target_directory_id is not None:
        expected_scope.add(target_directory_id)
    if (
        set(step.scope_directory_ids) != expected_scope
        or not expected_scope <= managed_directory_ids
    ):
        return False
    for member in step.members:
        row = rows.get((member.object_type, member.object_id))
        if (
            row is None
            or row.is_directory
            or row.parent_id != member.source_parent_id
            or row.path != member.source_path
            or row.name != member.source_name
            or not _source_version_matches(member.source_version, row)
            or not _source_parent_is_managed(
                member.source_parent_id, directory_rows, library.root_directory_id
            )
            or member.target_parent_id != primary.target_parent_id
            or member.target_directory_path != primary.target_directory_path
            or not _valid_target_name(member.target_name)
        ):
            return False
    return True

def _library_snapshot(library: MediaLibrary) -> dict[str, object]:
    return {
        "library_id": library.id,
        "revision": library.revision,
        "enabled": library.enabled,
        "scope_verified": library.scope_verified,
        "root_directory_id": library.root_directory_id,
    }

async def _latest_completed_scan(
    session: AsyncSession, *, library_id: str, root_directory_id: str
) -> LibraryScanRun | None:
    latest = await session.scalar(
        select(LibraryScanRun)
        .where(
            LibraryScanRun.library_id == library_id,
            LibraryScanRun.root_directory_id == root_directory_id,
            LibraryScanRun.complete.is_(True),
            LibraryScanRun.state == ScanRunState.COMPLETED.value,
            LibraryScanRun.snapshot_revision.is_not(None),
        )
        .order_by(LibraryScanRun.snapshot_revision.desc(), LibraryScanRun.id.desc())
        .limit(1)
    )
    if latest is None or latest.snapshot_revision is None:
        return None
    if not await source_snapshot_is_current(
        session,
        library_id=library_id,
        source_scan_run_id=latest.id,
        source_snapshot_revision=latest.snapshot_revision,
    ):
        return None
    return latest

def _plan_scan_binding_is_current(
    plan: OrganizationPlan,
    *,
    library: MediaLibrary,
    run: LibraryScanRun,
    latest: LibraryScanRun,
) -> bool:
    return (
        _scan_is_valid_for_library(library, run)
        and _scan_is_valid_for_library(library, latest)
        and run.id == plan.source_scan_run_id
        and run.snapshot_revision == plan.source_snapshot_revision
        and latest.id == run.id
        and latest.snapshot_revision == run.snapshot_revision
    )

def _scan_is_valid_for_library(library: MediaLibrary, run: LibraryScanRun) -> bool:
    return (
        run.library_id == library.id
        and run.root_directory_id == library.root_directory_id
        and run.state == ScanRunState.COMPLETED.value
        and run.complete
        and run.snapshot_revision is not None
    )

def _source_observation_matches(
    snapshot: Mapping[str, object], row: LibraryScanEntry
) -> bool:
    return (
        snapshot.get("object_type") == row.object_type
        and snapshot.get("object_id") == row.object_id
        and snapshot.get("parent_id") == row.parent_id
        and snapshot.get("path") == row.path
        and snapshot.get("name") == row.name
        and _source_version_matches(snapshot.get("remote_version"), row)
        and row.is_directory is False
    )

def _source_version_matches(value: object, row: LibraryScanEntry) -> bool:
    """Verify the exact generated fingerprint for the current scan row."""

    return _is_remote_version_hash(value) and value == _entry_remote_version(row)

def _resolve_source_index(
    source_snapshot: Sequence[Mapping[str, object]],
    *,
    source_object_id: str | None,
    source_index: int | None,
) -> int | None:
    if source_index is not None and source_index >= len(source_snapshot):
        raise OrganizationPlanError("invalid_source_index")
    if source_object_id is not None:
        matching = next(
            (
                index
                for index, source in enumerate(source_snapshot)
                if source.get("object_id") == source_object_id
            ),
            None,
        )
        if matching is None or (
            source_index is not None and source_index != matching
        ):
            raise OrganizationPlanError("invalid_source_object")
        return matching
    if source_index is not None:
        return source_index
    return 0 if len(source_snapshot) == 1 else None

def _basis_for_source(
    basis: list[object], source_index: int, source: Mapping[str, object]
) -> dict[str, object] | None:
    for evidence in basis:
        if not isinstance(evidence, dict):
            continue
        if evidence.get("source_index") == source_index or evidence.get(
            "source_object_id"
        ) == source.get("object_id"):
            return evidence
    return None

def _merge_candidate_evidence(
    evidence: dict[str, object], candidates: Sequence[TmdbCandidate]
) -> bool:
    existing = evidence.get("candidates")
    values = list(existing) if isinstance(existing, list) else []
    before = _json(values)
    seen: set[tuple[object, object]] = set()
    merged: list[dict[str, object]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        key = (value.get("tmdb_id"), value.get("media_type"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(value)
    for candidate in candidates:
        payload = _candidate_payload(candidate)
        key = (payload["tmdb_id"], payload["media_type"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(payload)
    evidence["candidates"] = merged[:20]
    return before != _json(merged[:20])

def _candidate_payload(candidate: TmdbCandidate) -> dict[str, object]:
    return {
        "tmdb_id": candidate.tmdb_id,
        "title": candidate.title,
        "media_type": candidate.media_type.value,
        "release_year": candidate.release_year,
        "original_title": candidate.original_title,
        "origin_countries": list(candidate.origin_countries),
        "kind": candidate.kind.value,
        "special_kind": candidate.special_kind.value,
        "seasons": [
            {
                "season_number": season.season_number,
                "episode_count": season.episode_count,
                "episode_numbers": list(season.episode_numbers),
            }
            for season in candidate.seasons
        ],
    }

def _validate_candidate_query(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OrganizationPlanError("invalid_candidate_query")
    value = value.strip()
    if (
        not value
        or len(value) > 200
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise OrganizationPlanError("invalid_candidate_query")
    return value

def _reconcile_target_conflicts(
    actions: list[object], preconditions: list[object]
) -> None:
    counts: dict[str, int] = {}
    for action in actions:
        if not isinstance(action, dict) or not isinstance(action.get("target"), str):
            continue
        target = _normalize_target(action["target"])
        counts[target] = counts.get(target, 0) + 1
    for index, action in enumerate(actions):
        if not isinstance(action, dict) or not isinstance(action.get("target"), str):
            continue
        precondition = preconditions[index] if index < len(preconditions) else None
        previous_conflict = isinstance(precondition, dict) and bool(
            precondition.get("target_conflict")
        )
        is_conflict = (
            counts.get(_normalize_target(action["target"]), 0) > 1
            or previous_conflict
        )
        action["kind"] = "review" if is_conflict else action.get("kind", "review")
        if isinstance(precondition, dict):
            precondition["target_conflict"] = is_conflict

def _load_json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}

def _load_source_snapshot(
    value: object, *, require_name: bool = False
) -> list[dict[str, object]] | None:
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    for item in parsed:
        if not isinstance(item, dict):
            return None
        object_type = item.get("object_type")
        object_id = item.get("object_id")
        parent_id = item.get("parent_id")
        path = item.get("path")
        remote_version = item.get("remote_version")
        if (
            not _safe_identity(object_type)
            or not _safe_identity(object_id)
            or not _safe_identity(parent_id)
            or not _valid_source_path(path)
            or not _valid_source_version(remote_version)
            or item.get("is_directory") is not False
            or (require_name and not _valid_target_name(item.get("name")))
        ):
            return None
        companions = item.get("companions", [])
        if not isinstance(companions, list):
            return None
        seen_companions: set[tuple[object, object]] = set()
        for companion in companions:
            if not isinstance(companion, dict):
                return None
            companion_key = (
                companion.get("object_type"),
                companion.get("object_id"),
            )
            if (
                not _safe_identity(companion_key[0])
                or not _safe_identity(companion_key[1])
                or companion_key in seen_companions
                or companion_key == (object_type, object_id)
                or not _safe_identity(companion.get("parent_id"))
                or not _valid_source_path(companion.get("path"))
                or not _valid_target_name(companion.get("name"))
                or not _valid_source_version(companion.get("remote_version"))
                or companion.get("is_directory") is not False
            ):
                return None
            seen_companions.add(companion_key)
    return parsed

def _load_json_list(value: object) -> list[object]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []

def _target_path(root: str, path: str | None) -> str | None:
    if path is None:
        return None
    normalized = _validate_relative_path(path)
    if not root:
        return normalized
    return _validate_relative_path(f"{root}/{normalized}")

def _validate_items(
    items: Sequence[OrganizationPlanItem],
) -> tuple[OrganizationPlanItem, ...]:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)) or not items:
        raise OrganizationPlanError("invalid_plan_items")
    result = tuple(items)
    keys: set[tuple[str, str]] = set()
    for item in result:
        if not isinstance(item, OrganizationPlanItem):
            raise OrganizationPlanError("invalid_plan_items")
        source = item.source
        _validate_identity(source.object_type, "invalid_source_identity")
        _validate_identity(source.object_id, "invalid_source_identity")
        _validate_identity(source.parent_id, "invalid_source_parent")
        if (
            not isinstance(source.path, str)
            or not source.path
            or "\x00" in source.path
            or len(source.path) > 4096
            or not isinstance(source.remote_version, str)
            or not source.remote_version
            or len(source.remote_version) > 128
            or not _valid_source_version(source.remote_version)
            or not isinstance(source.is_directory, bool)
        ):
            raise OrganizationPlanError("invalid_source_snapshot")
        if source.is_directory:
            raise OrganizationPlanError("source_not_file")
        key = (source.object_type, source.object_id)
        if key in keys:
            raise OrganizationPlanError("duplicate_source")
        keys.add(key)
        if not isinstance(item.naming_plan, NamingPlan) or not isinstance(
            item.decision, MatchDecision
        ):
            raise OrganizationPlanError("invalid_plan_input")
        if not isinstance(item.companions, Sequence) or isinstance(
            item.companions, (str, bytes)
        ):
            raise OrganizationPlanError("invalid_plan_input")
        if any(
            not isinstance(companion, OrganizationPlanCompanion)
            for companion in item.companions
        ):
            raise OrganizationPlanError("invalid_plan_input")
        if item.policy_decision is not None and not isinstance(
            item.policy_decision, VersionDecision
        ):
            raise OrganizationPlanError("invalid_plan_input")
        if item.policy_evidence is not None and not isinstance(
            item.policy_evidence, VersionEvidence
        ):
            raise OrganizationPlanError("invalid_plan_input")
        if item.existing_evidence is not None and not isinstance(
            item.existing_evidence, VersionEvidence
        ):
            raise OrganizationPlanError("invalid_plan_input")
        if item.target_directory_path is not None and not _valid_target_path(
            item.target_directory_path
        ):
            raise OrganizationPlanError("invalid_plan_input")
        replacement_values = (
            item.replacement_object_id,
            item.replacement_parent_id,
            item.replacement_name,
        )
        if any(value is not None for value in replacement_values) and (
            not _safe_identity(item.replacement_object_id)
            or not _safe_identity(item.replacement_parent_id)
            or not _valid_target_name(item.replacement_name)
            or item.policy_decision is None
            or item.policy_decision.outcome != "candidate"
        ):
            raise OrganizationPlanError("invalid_plan_input")
    return tuple(
        sorted(
            result, key=lambda item: (item.source.object_type, item.source.object_id)
        )
    )

def _validate_identity(value: str, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or "://" in value
    ):
        raise OrganizationPlanError(code)
    return value

def _validate_target_directories(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise OrganizationPlanError("invalid_target_directories")
    normalized: dict[str, str] = {}
    for path, directory_id in value.items():
        if not isinstance(path, str) or not isinstance(directory_id, str):
            raise OrganizationPlanError("invalid_target_directories")
        normalized_path = _validate_relative_path(path, allow_empty=True)
        _validate_identity(directory_id, "invalid_target_directory")
        existing = normalized.get(normalized_path)
        if existing is not None and existing != directory_id:
            raise OrganizationPlanError("target_directory_path_conflict")
        normalized[normalized_path] = directory_id
    return dict(sorted(normalized.items()))

def _is_remote_version_hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None

def _valid_source_version(value: object) -> bool:
    return _is_remote_version_hash(value)

def _valid_source_path(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\x00" in value
        or "\\" in value
        or "://" in value
    ):
        return False
    parts = value.split("/")
    if value.startswith("/"):
        parts = parts[1:]
    return bool(parts) and all(part not in {"", ".", ".."} for part in parts)

def _validate_alias(value: str) -> str:
    if not isinstance(value, str):
        raise OrganizationPlanError("invalid_alias")
    value = value.strip()
    lowered = value.casefold()
    if (
        not value
        or len(value) > 64
        or not any(character.isalpha() for character in value)
        or any(character.isspace() and character not in {" "} for character in value)
        or any(character in value for character in ("/", "\\", "\x00", ":"))
        or "://" in value
        or any(
            marker in lowered
            for marker in ("cookie", "pickcode", "token", "password", "secret")
        )
        or value in {".", ".."}
    ):
        raise OrganizationPlanError("invalid_alias")
    return value

def _validate_version(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or "\x00" in value
        or any(character.isspace() for character in value)
        or not re.fullmatch(r"[A-Za-z0-9._-]+", value)
    ):
        raise OrganizationPlanError("invalid_version")
    return value

def _validate_relative_path(value: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > 4096:
        raise OrganizationPlanError("invalid_target_path")
    if not value and allow_empty:
        return ""
    if (
        not value
        or value.startswith(("/", "\\", "//"))
        or re.match(r"^[A-Za-z]:", value)
        or "://" in value
        or "\\" in value
    ):
        raise OrganizationPlanError("invalid_target_path")
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise OrganizationPlanError("invalid_target_path")
    return "/".join(parts)

def _source_key(value: Mapping[str, object]) -> tuple[str, str]:
    return str(value["object_type"]), str(value["object_id"])

def _directory_id_for_path(directories: Mapping[str, object], path: str) -> str | None:
    normalized = path.strip().strip("/").casefold()
    for key, value in directories.items():
        if isinstance(key, str) and key.strip().strip("/").casefold() == normalized:
            return value if isinstance(value, str) and value else None
    return None

def _entry_remote_version(entry: LibraryScanEntry) -> str:
    modified = entry.modified_at.astimezone(UTC).isoformat() if entry.modified_at else None
    payload = {
        "object_type": entry.object_type,
        "object_id": entry.object_id,
        "parent_id": entry.parent_id,
        "path": entry.path,
        "name": entry.name,
        "size_bytes": entry.size_bytes,
        "modified_at": modified,
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()

def _normalize_target(value: str) -> str:
    return value.casefold()

def _safe_reason(value: object) -> str:
    text = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", text):
        return "reason_redacted"
    return text

def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

def _canonical_hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()

def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)

def _strict_plan_versions(
    source_snapshot: Sequence[object],
    actions: Sequence[object],
    precondition_items: object,
) -> bool:
    if not isinstance(precondition_items, list):
        return False
    for snapshot in source_snapshot:
        if not isinstance(snapshot, dict) or not _is_remote_version_hash(
            snapshot.get("remote_version")
        ):
            return False
        companions = snapshot.get("companions", [])
        if not isinstance(companions, list):
            return False
        if any(
            not isinstance(companion, dict)
            or not _is_remote_version_hash(companion.get("remote_version"))
            for companion in companions
        ):
            return False
    for action in actions:
        if not isinstance(action, dict) or not _is_remote_version_hash(
            action.get("source_version")
        ):
            return False
        execution = action.get("execution")
        if isinstance(execution, dict) and not _strict_execution_versions(execution):
            return False
    for precondition in precondition_items:
        if not isinstance(precondition, dict) or not _is_remote_version_hash(
            precondition.get("remote_version")
        ):
            return False
        execution = precondition.get("execution")
        if isinstance(execution, dict) and not _strict_execution_versions(execution):
            return False
    return True

def _strict_execution_versions(execution: Mapping[str, object]) -> bool:
    members = execution.get("members")
    return isinstance(members, list) and bool(members) and all(
        isinstance(member, dict)
        and _is_remote_version_hash(member.get("source_version"))
        for member in members
    )

def _execution_blockers(
    *,
    status: OrganizationPlanStatus,
    expires_at: datetime,
    now: datetime,
    action_count: int,
    executable_action_count: int,
    review_action_count: int,
    complete_preconditions: bool,
    strict_source_versions: bool,
    source_snapshot_changed: bool,
) -> tuple[OrganizationExecutionBlocker, ...]:
    blockers: list[OrganizationExecutionBlocker] = []
    if expires_at <= now:
        blockers.append(
            OrganizationExecutionBlocker(
                kind="expired",
                code="plan_expired",
                message_zh="整理计划已过期，不能执行。",
                next_step_zh="重新扫描并生成新的整理计划。",
            )
        )
    if status not in {
        OrganizationPlanStatus.NEEDS_REVIEW,
        OrganizationPlanStatus.PLANNED,
    }:
        blockers.append(
            OrganizationExecutionBlocker(
                kind="status",
                code=f"plan_status_{status.value}",
                message_zh="整理计划当前状态不允许执行。",
                next_step_zh="重新扫描并生成新的整理计划。",
            )
        )
    if source_snapshot_changed or not strict_source_versions:
        blockers.append(
            OrganizationExecutionBlocker(
                kind="snapshot_changed",
                code="source_snapshot_changed",
                message_zh="来源快照已变化或无法核对，原计划不能执行。",
                next_step_zh="重新扫描并生成新的整理计划。",
            )
        )
    if not complete_preconditions:
        blockers.append(
            OrganizationExecutionBlocker(
                kind="prerequisite",
                code="plan_prerequisites_incomplete",
                message_zh="整理前置条件尚未全部满足。",
                next_step_zh="完成范围验证、目标目录检查和最新扫描后重新生成计划。",
            )
        )
    if action_count == 0 or executable_action_count == 0:
        blockers.append(
            OrganizationExecutionBlocker(
                kind="review_only",
                code="plan_has_review_actions",
                message_zh="计划中没有可自动执行的动作。",
                next_step_zh="逐项复核计划内容，处理待确认动作后再执行。",
            )
        )
    return tuple(blockers)

async def _source_snapshot_changed(
    session: AsyncSession,
    plan: OrganizationPlan,
    source_snapshot: list[dict[str, object]] | None,
) -> bool:
    """Compare the persisted source observation with the current completed scan."""

    if source_snapshot is None:
        return True
    library = await session.get(MediaLibrary, plan.library_id)
    run = await session.get(LibraryScanRun, plan.source_scan_run_id)
    if library is None or run is None:
        return True
    latest = await _latest_completed_scan(
        session,
        library_id=plan.library_id,
        root_directory_id=library.root_directory_id,
    )
    if (
        latest is None
        or not _plan_scan_binding_is_current(
            plan, library=library, run=run, latest=latest
        )
    ):
        return True
    rows = {
        (row.object_type, row.object_id): row
        for row in await session.scalars(
            select(LibraryScanEntry).where(LibraryScanEntry.scan_run_id == run.id)
        )
    }
    for snapshot in source_snapshot:
        if not isinstance(snapshot, dict):
            return True
        object_type = snapshot.get("object_type")
        object_id = snapshot.get("object_id")
        if not isinstance(object_type, str) or not isinstance(object_id, str):
            return True
        row = rows.get((object_type, object_id))
        if row is None or not _source_observation_matches(snapshot, row):
            return True
    return False

def _view(
    plan: OrganizationPlan,
    *,
    source_snapshot_changed: bool = False,
    high_risk_action_threshold: int = 10,
) -> OrganizationPlanView:
    source_snapshot_payload = _load_source_snapshot(plan.source_snapshot_json)
    source_snapshot = source_snapshot_payload or []
    preconditions = _load_json_object(plan.preconditions_json)
    actions = _load_json_list(plan.actions_json)
    basis = _load_json_list(plan.basis_json)
    candidates: list[dict[str, object]] = []
    for evidence in basis:
        if not isinstance(evidence, dict) or not isinstance(evidence.get("candidates"), list):
            continue
        source_object_id = evidence.get("source_object_id")
        if not isinstance(source_object_id, str):
            source_index = evidence.get("source_index")
            source = (
                source_snapshot[source_index]
                if isinstance(source_index, int) and 0 <= source_index < len(source_snapshot)
                else None
            )
            source_object_id = source.get("object_id") if isinstance(source, dict) else None
        if not isinstance(source_object_id, str):
            continue
        for candidate in evidence["candidates"]:
            if not isinstance(candidate, dict):
                continue
            public = {
                key: candidate.get(key)
                for key in ("tmdb_id", "title", "media_type", "release_year")
            }
            if (
                isinstance(public["tmdb_id"], int)
                and isinstance(public["title"], str)
                and public["media_type"] in {"movie", "tv"}
            ):
                candidates.append({"source_object_id": source_object_id, **public})
    precondition_items = (
        preconditions.get("items") if isinstance(preconditions, dict) else None
    )
    precondition_count = (
        len(precondition_items) if isinstance(precondition_items, list) else 0
    )
    executable_action_count = sum(
        1
        for action in actions
        if isinstance(action, dict)
        and action.get("kind") == "move"
        and isinstance(action.get("execution"), dict)
    )
    review_action_count = max(0, len(actions) - executable_action_count)
    # 待处理文件名:每个 action(含 review 与 move)的 source_name,
    # 去重、保留顺序、最多 8 个,列表行直接展示。
    source_names: list[str] = []
    seen_source_names: set[str] = set()
    for action in actions:
        if not isinstance(action, dict):
            continue
        name = action.get("source_name")
        if isinstance(name, str) and name and name not in seen_source_names:
            seen_source_names.add(name)
            source_names.append(name)
    move_preconditions = [
        (item, precondition)
        for item, precondition in zip(actions, precondition_items or ())
        if isinstance(item, dict) and item.get("kind") == "move"
    ]
    complete_preconditions = (
        bool(actions)
        and precondition_count == len(actions)
        and isinstance(precondition_items, list)
        and all(
            isinstance(item, dict)
            and isinstance(precondition, dict)
            and precondition.get("target_conflict") is False
            and isinstance(precondition.get("execution"), dict)
            for item, precondition in move_preconditions
        )
    )
    strict_source_versions = (
        source_snapshot_payload is not None
        and _strict_plan_versions(source_snapshot, actions, precondition_items)
    )
    expires_at = _utc(plan.expires_at)
    blockers = _execution_blockers(
        status=OrganizationPlanStatus(plan.status),
        expires_at=expires_at,
        now=datetime.now(UTC),
        action_count=len(actions),
        executable_action_count=executable_action_count,
        review_action_count=review_action_count,
        complete_preconditions=complete_preconditions,
        strict_source_versions=strict_source_versions,
        source_snapshot_changed=source_snapshot_changed,
    )
    return OrganizationPlanView(
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        status=OrganizationPlanStatus(plan.status),
        revision=plan.revision,
        expires_at=expires_at,
        source_count=len(source_snapshot),
        action_count=len(actions),
        precondition_count=precondition_count,
        executable_action_count=executable_action_count,
        review_action_count=review_action_count,
        can_execute=not blockers,
        requires_web_approval=len(actions) > high_risk_action_threshold,
        high_risk_action_threshold=high_risk_action_threshold,
        execution_blockers=blockers,
        source_names=tuple(source_names),
        alias=plan.alias,
        candidates=tuple(candidates),
    )

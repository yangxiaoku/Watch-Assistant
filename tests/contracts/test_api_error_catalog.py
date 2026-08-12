import ast
import json
import re
from pathlib import Path

from watch_assistant.services.api_errors import (
    build_error_payload,
    catalog_codes,
    error_status,
)

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{1,99}$")


def _frontend_catalog_codes() -> set[str]:
    """Read both the fallback list and explicit descriptor keys from the Vue catalog."""

    path = Path(__file__).parents[2] / "frontend" / "src" / "errorCatalog.ts"
    text = path.read_text(encoding="utf-8")
    additional = text.split("const ADDITIONAL_CODES = [", 1)[1].split("] as const;", 1)[0]
    codes = {
        value
        for value in re.findall(r'"([a-z][a-z0-9_.-]{1,99})"', additional)
        if _SAFE_CODE.fullmatch(value)
    }
    catalog = text.split("const CATALOG: Record<string, Omit<UiErrorDescriptor, \"code\">> = {", 1)[1]
    catalog = catalog.split("};\n\nexport const UI_ERROR_CODES", 1)[0]
    codes.update(
        value
        for value in re.findall(r'^\s{2}"?([a-z][a-z0-9_.-]{1,99})"?\s*:', catalog, re.MULTILINE)
        if _SAFE_CODE.fullmatch(value)
    )
    return codes


def _literal_http_error_codes() -> set[str]:
    api_root = Path(__file__).parents[2] / "src" / "watch_assistant" / "api"
    codes: set[str] = set()
    for path in api_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "HTTPException":
                continue
            detail = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "detail"),
                None,
            )
            if isinstance(detail, ast.Constant) and isinstance(detail.value, str):
                codes.add(detail.value)
            elif isinstance(detail, ast.Dict):
                for key, value in zip(detail.keys, detail.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "code"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                    ):
                        codes.add(value.value)
    return codes


def test_literal_http_error_codes_are_in_central_catalog():
    assert _literal_http_error_codes() <= catalog_codes()


def test_backend_error_catalog_is_registered_in_frontend_catalog():
    missing = catalog_codes() - _frontend_catalog_codes()
    assert not missing, f"frontend error catalog is missing: {sorted(missing)}"


def test_domain_codes_used_by_http_error_mappers_are_in_central_catalog():
    domain_codes = {
        "backup_requires_file_database",
        "backup_failed",
        "backup_not_found",
        "backup_manifest_invalid",
        "backup_database_missing",
        "invalid_backup_id",
        "database_not_found",
        "confirmation_required",
        "resource_conflict",
        "invalid_resource",
        "media_mismatch",
        "invalid_magnet",
        "unsupported_url",
        "invalid_share_url",
        "unsupported_share_domain",
        "season_not_found",
        "season_metadata_unavailable",
        "season_metadata_cache_invalid",
        "invalid_series_id",
        "invalid_season_number",
        "invalid_language",
        "invalid_filename",
        "subscription_exists",
        "subscription_conflict",
        "subscription_not_pauseable",
        "subscription_cancelled",
        "subscription_not_active",
        "subscription_check_failed",
        "notification_preference_conflict",
        "quality_profile_conflict",
        "invalid_quality_rules",
        "unknown_quality_rule",
        "invalid_min_resolution",
        "plan_not_found",
        "invalid_plan",
        "invalid_pagination",
        "invalid_revision",
        "invalid_alias",
        "stale_revision",
        "plan_not_reviewable",
        "operation_not_found",
        "operation_unavailable",
        "invalid_plan_id",
        "invalid_idempotency_key",
        "invalid_operation_id",
        "plan_is_not_planned",
        "plan_prerequisites_changed",
        "plan_revision_changed",
        "idempotency_key_conflict",
        "plan_already_has_operation",
        "operation_plan_conflict",
        "operation_creation_conflict",
        "operation_revision_changed",
        "operation_is_not_cancellable",
        "uncertain_requires_verification",
        "operation_is_not_claimable",
        "lease_is_active",
        "lease_claim_lost",
        "lease_is_not_owned",
        "operation_is_not_retryable",
        "invalid_terminal_status",
        "directory_scope_required",
        "strm_library_operation_conflict",
        "strm_operation_in_progress",
        "strm_operation_lease_lost",
    }
    assert domain_codes <= catalog_codes()


def test_h9_registered_error_codes_have_specific_chinese_copy():
    """H9 审计:此前直接上抛的非 catalog 码现在都有特定中文文案,而非通用回退。"""
    h9_codes = {
        # STRM operations
        "strm_operation_claim_conflict",
        "strm_operation_creation_conflict",
        "strm_operation_lease_required",
        "strm_operation_not_reconcilable",
        "strm_operation_not_running",
        "strm_full_failed",
        "strm_incremental_failed",
        "strm_cleanup_failed",
        "invalid_cursor",
        "invalid_limit",
        # task / webhook / device
        "task_lease_active",
        "webhook_endpoint_disabled",
        "device_unavailable",
        "invalid_device_code",
        "invalid_device_name",
        # organization preview / settings validation
        "invalid_small_file_threshold",
        "target_file_invalid",
        "invalid_poll_interval",
        "invalid_conflict_mode",
        "invalid_scan_interval_minutes",
        # target catalog
        "target_directory_id_invalid",
        "target_directory_limit_invalid",
        "target_file_limit_invalid",
        "target_directory_incomplete",
        "target_file_limit_exceeded",
        "target_file_name_invalid",
        "target_file_identity_conflict",
        "target_directory_name_invalid",
        "target_directory_path_conflict",
        "target_directory_identity_conflict",
        "target_directory_limit_exceeded",
        "target_directory_pagination_unverified",
        "target_entry_type_unverified",
        "target_entry_scope_unverified",
        "target_entry_identity_unverified",
        "target_directory_read_failed",
    }
    generic_title = "操作暂时无法完成"
    for code in sorted(h9_codes):
        payload = build_error_payload(
            code,
            error_status(code),
            request_id="request-test",
            correlation_id="correlation-test",
        )
        assert payload["code"] == code, code
        assert payload["title_zh"] not in {generic_title, "输入内容有误"}, code
        assert payload["message_zh"], code
        assert payload["suggestion_zh"], code


def test_every_catalog_entry_builds_a_safe_chinese_payload():
    for code in catalog_codes():
        payload = build_error_payload(
            code,
            422,
            request_id="request-test",
            correlation_id="correlation-test",
        )
        assert payload["code"] == code
        assert payload["title_zh"]
        assert payload["message_zh"]
        assert payload["suggestion_zh"]


def test_strm_fencing_error_payloads_are_chinese_and_do_not_expose_lease_data():
    for code in (
        "strm_library_operation_conflict",
        "strm_operation_in_progress",
        "strm_operation_lease_lost",
    ):
        payload = build_error_payload(
            code,
            409,
            request_id="request-test",
            correlation_id="correlation-test",
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        assert payload["title_zh"]
        assert payload["message_zh"]
        assert payload["suggestion_zh"]
        assert "lease_owner" not in serialized
        assert "lease_token" not in serialized
        assert "token" not in serialized.lower()

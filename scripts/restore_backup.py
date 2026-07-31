#!/usr/bin/env python3
"""Restore a verified SQLite backup while the application is stopped.

This command intentionally performs no service management. The operator must
stop the service independently and pass both explicit confirmation flags.
Output is a redacted JSON result and never includes filesystem paths.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from watch_assistant.services.api_errors import build_error_payload
from watch_assistant.services.backups import BackupService, BackupServiceError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup-directory", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--backup-id")
    source.add_argument("--encrypted-manifest", type=Path)
    parser.add_argument("--approval-id")
    parser.add_argument("--recovery-key-stdin", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--service-stopped", action="store_true")
    return parser


def _error_result(code: str) -> dict[str, object]:
    payload = build_error_payload(
        code,
        422,
        request_id="offline-restore",
        correlation_id="offline-restore",
    )
    return {
        "status": "failed",
        "error_code": code,
        "title_zh": payload["title_zh"],
        "message_zh": payload["message_zh"],
        "suggestion_zh": payload["suggestion_zh"],
    }


async def _run(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    service = BackupService(
        str(args.database),
        args.backup_directory,
    )
    try:
        if args.encrypted_manifest is not None:
            if not args.recovery_key_stdin:
                return 2, _error_result("encrypted_backup_key_invalid")
            recovery_key = sys.stdin.read().strip()
            if not recovery_key:
                return 2, _error_result("encrypted_backup_key_invalid")
            result = await service.restore_encrypted_to(
                args.encrypted_manifest,
                args.database,
                recovery_key=recovery_key,
                confirmed=args.confirm,
                service_stopped=args.service_stopped,
                approval_id=args.approval_id,
            )
        else:
            result = await service.restore_to(
                args.backup_id,
                args.database,
                confirmed=args.confirm,
                service_stopped=args.service_stopped,
                approval_id=args.approval_id,
            )
    except BackupServiceError as exc:
        return 2, _error_result(exc.code)
    except Exception:  # noqa: BLE001 - maintenance output must stay redacted
        return 2, _error_result("restore_failed")
    return 0, {
        "status": result.status,
        "backup_id": result.backup_id,
        "pre_restore_backup_id": result.pre_restore_backup_id,
        "integrity_ok": result.integrity_ok,
        "business_consistency_ok": result.business_consistency_ok,
        "restart_required": result.restart_required,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    exit_code, result = asyncio.run(_run(args))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

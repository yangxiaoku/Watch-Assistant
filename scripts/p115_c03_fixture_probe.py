"""Run the C03 lifecycle against an explicitly selected offline fake.

This CLI never loads credentials, creates a P115 client, or performs network
I/O.  A live caller must invoke the injected boundary in
``p115_c03_fixture_probe`` and satisfy the separate live environment gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence

from watch_assistant.adapters.p115_c03_fixture_probe import (
    FakeP115C03Transport,
    run_p115_c03_fixture_probe,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the offline P115 C03 fixture")
    parser.add_argument("--parent-id", required=True)
    parser.add_argument(
        "--offline-fixture",
        choices=("success", "write-failed", "timeout", "cancelled", "read-failed"),
        required=True,
    )
    args = parser.parse_args(argv)
    report = asyncio.run(
        run_p115_c03_fixture_probe(
            transport=FakeP115C03Transport(_failure_for_fixture(args.offline_fixture)),
            parent_id=args.parent_id,
        )
    )
    print(json.dumps(report.to_public_dict(), ensure_ascii=True, sort_keys=True))
    return 0 if report.status == "success" else 1


def _failure_for_fixture(fixture: str) -> str | None:
    return {
        "success": None,
        "write-failed": "move",
        "timeout": "timeout:move",
        "cancelled": "cancel:move",
        "read-failed": "read",
    }[fixture]


if __name__ == "__main__":
    raise SystemExit(main())

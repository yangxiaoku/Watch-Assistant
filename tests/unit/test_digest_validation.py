"""Digest/hash 字段必须是 ASCII hex64,避免 hmac.compare_digest 抛 TypeError。"""

import pytest
from pydantic import ValidationError

from watch_assistant.schemas import (
    EmptyDirectoryCleanupPlanApplyRequest,
    OrganizationOperationBatchItem,
    OrganizationOperationQueueRequest,
    OrganizationPlanMutationRequest,
    StrmCleanupPlanApplyRequest,
)

HEX64 = "a" * 64


@pytest.mark.parametrize(
    "model_factory",
    [
        lambda digest: StrmCleanupPlanApplyRequest(
            expected_revision=1,
            digest=digest,
            confirm=True,
            idempotency_key="k",
        ),
        lambda digest: EmptyDirectoryCleanupPlanApplyRequest(
            expected_revision=1,
            digest=digest,
            confirm=True,
            idempotency_key="k",
        ),
        lambda digest: OrganizationPlanMutationRequest(
            expected_revision=1, plan_hash=digest
        ),
        lambda digest: OrganizationOperationQueueRequest(
            expected_revision=1,
            idempotency_key="k",
            digest=digest,
            confirm=True,
        ),
        lambda digest: OrganizationOperationBatchItem(
            plan_id="plan",
            expected_revision=1,
            idempotency_key="k",
            digest=digest,
            confirm=True,
        ),
    ],
)
def test_digest_fields_accept_hex64(model_factory):
    model = model_factory(HEX64)
    assert model is not None


@pytest.mark.parametrize(
    "model_factory",
    [
        lambda digest: StrmCleanupPlanApplyRequest(
            expected_revision=1,
            digest=digest,
            confirm=True,
            idempotency_key="k",
        ),
        lambda digest: EmptyDirectoryCleanupPlanApplyRequest(
            expected_revision=1,
            digest=digest,
            confirm=True,
            idempotency_key="k",
        ),
        lambda digest: OrganizationPlanMutationRequest(
            expected_revision=1, plan_hash=digest
        ),
        lambda digest: OrganizationOperationQueueRequest(
            expected_revision=1,
            idempotency_key="k",
            digest=digest,
            confirm=True,
        ),
        lambda digest: OrganizationOperationBatchItem(
            plan_id="plan",
            expected_revision=1,
            idempotency_key="k",
            digest=digest,
            confirm=True,
        ),
    ],
)
def test_digest_fields_reject_non_ascii(model_factory):
    with pytest.raises(ValidationError):
        model_factory("你" * 64)

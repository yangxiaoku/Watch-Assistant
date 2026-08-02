from datetime import UTC, datetime, timedelta

from watch_assistant.models import TaskAction
from watch_assistant.services.tasks import TaskLease


def test_task_lease_repr_redacts_fencing_identity_and_submission_snapshots():
    lease = TaskLease(
        task_id="task-fixture-id",
        lease_owner="worker-fixture-id",
        lease_token="lease-fixture-token",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        resource_id="resource-fixture-id",
        workflow_id="workflow-fixture-id",
        target_directory_id="directory-fixture-id",
        action=TaskAction.OFFLINE_DOWNLOAD,
        encrypted_url_snapshot="encrypted-url-fixture",
        encrypted_password_snapshot="encrypted-password-fixture",
        remote_ref="remote-fixture-id",
    )

    rendered = repr(lease)

    for sensitive_value in (
        "task-fixture-id",
        "worker-fixture-id",
        "lease-fixture-token",
        "encrypted-url-fixture",
        "encrypted-password-fixture",
        "remote-fixture-id",
    ):
        assert sensitive_value not in rendered
    assert "<redacted>" in rendered

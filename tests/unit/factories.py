from datetime import UTC, datetime, timedelta

from watch_assistant.models import Task, TaskState


def make_task(
    *,
    state: TaskState = TaskState.QUEUED,
    age_hours: int = 0,
    resource_id: str = "res_test",
) -> Task:
    created_at = datetime.now(UTC) - timedelta(hours=age_hours)
    return Task(
        id=f"task_{state.value}_{age_hours}",
        resource_id=resource_id,
        action="offline_download",
        encrypted_url_snapshot="encrypted",
        state=state,
        created_at=created_at,
        updated_at=created_at,
    )

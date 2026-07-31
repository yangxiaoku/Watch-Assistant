import ast
from pathlib import Path

from watch_assistant.services.event_catalog import EVENT_CATALOG
from watch_assistant.services.notifications import (
    _NOTIFIABLE_EVENTS,
    REQUIRED_NOTIFICATION_EVENTS,
)


def _literal_event_calls() -> set[str]:
    source_root = Path(__file__).parents[2] / "src" / "watch_assistant"
    events: set[str] = set()
    for path in source_root.rglob("*.py"):
        if path.name == "event_catalog.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else None
            )
            argument_index = 1 if name == "_emit_security_event" else 0
            if name not in {"emit_event", "log_event", "_audit", "_emit_security_event"}:
                continue
            if len(node.args) <= argument_index:
                continue
            argument = node.args[argument_index]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                events.add(argument.value)
    return events


def test_literal_business_events_are_registered():
    assert _literal_event_calls() <= set(EVENT_CATALOG)


def test_required_business_events_have_notification_policy():
    assert REQUIRED_NOTIFICATION_EVENTS <= _NOTIFIABLE_EVENTS
    assert _NOTIFIABLE_EVENTS <= set(EVENT_CATALOG)

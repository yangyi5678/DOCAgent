"""Parse user-facing runtime control commands before normal planning."""

from __future__ import annotations

from dataclasses import dataclass

from task_control import TASK_CANCEL_USER


RUNTIME_COMMAND_CANCEL_TASK = "cancel_task"

CANCEL_WORDS = {
    "cancel",
    "stop",
    "abort",
    "别做了",
    "不用了",
    "取消",
    "停止",
    "停",
}


@dataclass(frozen=True)
class RuntimeCommand:
    command_type: str
    task_id: str
    reason: str
    message: str | None = None


def parse_runtime_command(
    content: str,
    *,
    active_task_id: str | None,
) -> RuntimeCommand | None:
    """Return a runtime command when user input should bypass goal parsing."""
    normalized = content.strip().lower()
    if normalized in CANCEL_WORDS and active_task_id:
        return RuntimeCommand(
            command_type=RUNTIME_COMMAND_CANCEL_TASK,
            task_id=active_task_id,
            reason=TASK_CANCEL_USER,
            message="用户通过自然语言取消任务。",
        )
    return None


def is_cancel_phrase(content: str) -> bool:
    return content.strip().lower() in CANCEL_WORDS

"""Sandbox permission policy defaults."""

from __future__ import annotations

from typing import Any


DEFAULT_PERMISSION_POLICY = {
    "require_approval": False,
    "approval_reason": None,
}

SHELL_ALLOWED_COMMANDS = {
    "cat",
    "find",
    "git",
    "head",
    "ls",
    "pwd",
    "rg",
    "sed",
    "tail",
    "wc",
}
SHELL_APPROVAL_COMMANDS = [
    ["pip", "install"],
    ["uv", "sync"],
    ["npm", "install"],
    ["git", "commit"],
    ["git", "push"],
    ["sudo"],
    ["rm"],
    ["chmod"],
    ["chown"],
    ["ssh"],
    ["scp"],
    ["curl"],
    ["wget"],
    ["docker", "run", "--privileged"],
]


def _normalize_command(command: Any) -> list[str]:
    if isinstance(command, str):
        import shlex

        return shlex.split(command)
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        return command
    return []


def _matches_prefix(argv: list[str], prefix: list[str]) -> bool:
    return len(argv) >= len(prefix) and argv[: len(prefix)] == prefix


def build_permission_policy(
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build user-approval policy for one executable node."""
    if tool_name in {"run_shell_command", "run_shell_command_tool", "run_shell_command_core"}:
        tool_input = tool_input or {}
        if tool_input.get("approved") is True:
            return dict(DEFAULT_PERMISSION_POLICY)

        argv = _normalize_command(tool_input.get("command"))
        if not argv:
            return {
                **DEFAULT_PERMISSION_POLICY,
                "require_approval": True,
                "approval_reason": "shell 命令无法解析，需要审批。",
            }

        executable = argv[0]
        is_allowed = executable in SHELL_ALLOWED_COMMANDS
        needs_approval = any(_matches_prefix(argv, prefix) for prefix in SHELL_APPROVAL_COMMANDS)
        if needs_approval or not is_allowed:
            return {
                **DEFAULT_PERMISSION_POLICY,
                "require_approval": True,
                "approval_reason": "shell 命令需要审批后执行。",
            }

    return dict(DEFAULT_PERMISSION_POLICY)

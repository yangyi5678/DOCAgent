"""Process sandbox policy defaults."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any


DEFAULT_PROCESS_POLICY = {
    "allow_subprocess": False,
    "allowed_commands": [],
    "approval_commands": [],
    "max_child_processes": 0,
}

SHELL_ALLOWED_COMMANDS = ["cat", "find", "git", "head", "ls", "pwd", "rg", "sed", "tail", "wc"]
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

TOOL_PROCESS_POLICIES: dict[str, dict[str, Any]] = {
    "run_shell_command": {
        "allow_subprocess": True,
        "allowed_commands": SHELL_ALLOWED_COMMANDS,
        "approval_commands": SHELL_APPROVAL_COMMANDS,
        "max_child_processes": 1,
    },
    "run_shell_command_tool": {
        "allow_subprocess": True,
        "allowed_commands": SHELL_ALLOWED_COMMANDS,
        "approval_commands": SHELL_APPROVAL_COMMANDS,
        "max_child_processes": 1,
    },
    "run_shell_command_core": {
        "allow_subprocess": True,
        "allowed_commands": SHELL_ALLOWED_COMMANDS,
        "approval_commands": SHELL_APPROVAL_COMMANDS,
        "max_child_processes": 1,
    },
}


def build_process_policy(
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build subprocess policy for one executable node."""
    return {
        **DEFAULT_PROCESS_POLICY,
        **TOOL_PROCESS_POLICIES.get(tool_name or "", {}),
    }


def validate_process_access_for_node(node: dict[str, Any]) -> None:
    """Validate subprocess access for one executable DAG node before dispatch."""
    tool_name = node.get("tool")
    if tool_name not in TOOL_PROCESS_POLICIES:
        return

    tool_input = node.get("input") or {}
    if not isinstance(tool_input, dict):
        raise TypeError("节点 input 必须是 dict。")

    sandbox = node.get("sandbox") or {}
    policy = sandbox.get("process") or build_process_policy(tool_name, tool_input)
    ensure_process_command_allowed(tool_input.get("command"), policy, approved=tool_input.get("approved") is True)


def ensure_process_command_allowed(
    command: str | list[str] | None,
    policy: dict[str, Any] | None = None,
    *,
    approved: bool = False,
) -> None:
    """Raise PermissionError if a command cannot run under process policy."""
    policy = policy or DEFAULT_PROCESS_POLICY
    argv = _normalize_command(command)
    if not argv:
        raise ValueError("command 不能为空。")

    if not policy.get("allow_subprocess", False):
        raise PermissionError("当前 process policy 未允许启动子进程。")

    allowed_commands = set(policy.get("allowed_commands") or [])
    approval_commands = policy.get("approval_commands") or []
    executable = Path(argv[0]).name
    is_allowed = executable in allowed_commands
    needs_approval = any(_matches_prefix(argv, prefix) for prefix in approval_commands)

    if needs_approval and not approved:
        raise PermissionError("命令需要审批后执行。")
    if not is_allowed and not approved:
        raise PermissionError("命令不在直接允许列表内，需要审批后执行。")


def _normalize_command(command: str | list[str] | None) -> list[str]:
    if isinstance(command, str):
        return shlex.split(command.strip())
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        return command
    return []


def _matches_prefix(argv: list[str], prefix: list[str]) -> bool:
    return len(argv) >= len(prefix) and argv[: len(prefix)] == prefix

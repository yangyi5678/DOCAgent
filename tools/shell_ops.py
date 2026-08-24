from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

try:
    from langchain.tools import tool
except ImportError:  # pragma: no cover
    tool = None


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWED_ROOTS = [ROOT_DIR]
# 默认允许的安全读取/检查类命令。这里应该保持收敛：
# 这些命令主要用于本地项目探索，不用于任意系统修改。
DEFAULT_ALLOWED_COMMANDS = {
    "cat",  # 打印文件内容。
    "find",  # 按名称/路径规则查找文件和目录。
    "git",  # 查看仓库状态、历史、diff 和元数据。
    "head",  # 打印文件或输出流的前几行。
    "ls",  # 列出目录内容。
    "pwd",  # 打印当前工作目录。
    "rg",  # 使用 ripgrep 快速搜索文本/文件。
    "sed",  # 按文本范围/模式打印或转换内容。
    "tail",  # 打印文件或输出流的最后几行。
    "wc",  # 统计行数、单词数、字节数或字符数。
}
DEFAULT_APPROVAL_COMMANDS = [
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
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_OUTPUT_BYTES = 256 * 1024


def _resolve_allowed_roots(allowed_roots: list[str | Path] | None = None) -> list[Path]:
    roots = allowed_roots or DEFAULT_ALLOWED_ROOTS
    return [Path(root).expanduser().resolve() for root in roots]


def _ensure_cwd_allowed(
    cwd: str | Path | None,
    *,
    allowed_roots: list[str | Path] | None = None,
) -> Path:
    target = Path(cwd).expanduser().resolve() if cwd else ROOT_DIR
    roots = _resolve_allowed_roots(allowed_roots)

    for root in roots:
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue

    allowed_text = ", ".join(str(root) for root in roots)
    raise PermissionError(f"工作目录不在允许范围内: {target}。允许目录: {allowed_text}")


def _normalize_command(command: str | list[str]) -> list[str]:
    if isinstance(command, str):
        command = command.strip()
        if not command:
            raise ValueError("command 不能为空。")
        return shlex.split(command)

    if not isinstance(command, list) or not command:
        raise TypeError("command 必须是非空字符串或字符串列表。")
    if not all(isinstance(part, str) and part for part in command):
        raise TypeError("command 列表中的每一项都必须是非空字符串。")
    return command


def _matches_command_prefix(argv: list[str], prefix: list[str]) -> bool:
    return len(argv) >= len(prefix) and argv[: len(prefix)] == prefix


def _is_allowed_command(
    argv: list[str],
    *,
    allowed_commands: list[str] | set[str] | None = None,
) -> bool:
    executable = Path(argv[0]).name
    allowed = set(allowed_commands) if allowed_commands is not None else DEFAULT_ALLOWED_COMMANDS
    return executable in allowed


def _requires_approval_command(
    argv: list[str],
    *,
    approval_commands: list[list[str]] | None = None,
) -> bool:
    prefixes = approval_commands or DEFAULT_APPROVAL_COMMANDS
    return any(_matches_command_prefix(argv, prefix) for prefix in prefixes)


def _resolve_process_policy_commands(
    process_policy: dict[str, Any] | None,
    *,
    allowed_commands: list[str] | None = None,
    approval_commands: list[list[str]] | None = None,
) -> tuple[list[str] | None, list[list[str]] | None]:
    if process_policy is None:
        return allowed_commands, approval_commands

    policy_allowed = process_policy.get("allowed_commands")
    policy_approval = process_policy.get("approval_commands")
    return (
        allowed_commands if allowed_commands is not None else policy_allowed,
        approval_commands if approval_commands is not None else policy_approval,
    )


def _approval_required_result(argv: list[str], cwd: Path, reason: str) -> dict[str, Any]:
    return {
        "status": "approval_required",
        "action": "run_shell_command",
        "command": argv,
        "cwd": str(cwd),
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "elapsed_ms": 0,
        "approval_required": True,
        "approval_reason": reason,
    }


def _normalize_env(env: dict[str, str] | None) -> dict[str, str] | None:
    if env is None:
        return None
    if not isinstance(env, dict):
        raise TypeError("env 必须是 dict。")
    return {str(key): str(value) for key, value in env.items()}


def _truncate_text(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text, False
    truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
    return truncated, True


def run_shell_command_core(
    command: str | list[str],
    *,
    cwd: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    env: dict[str, str] | None = None,
    allowed_commands: list[str] | None = None,
    approval_commands: list[list[str]] | None = None,
    process_policy: dict[str, Any] | None = None,
    approved: bool = False,
    allowed_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Run one local command without invoking a shell."""
    argv = _normalize_command(command)
    safe_cwd = _ensure_cwd_allowed(cwd, allowed_roots=allowed_roots)
    if process_policy is not None and not process_policy.get("allow_subprocess", False):
        return _approval_required_result(argv, safe_cwd, "当前 process policy 未允许启动子进程，需要审批后执行。")

    allowed_commands, approval_commands = _resolve_process_policy_commands(
        process_policy,
        allowed_commands=allowed_commands,
        approval_commands=approval_commands,
    )

    is_allowed = _is_allowed_command(argv, allowed_commands=allowed_commands)
    needs_approval = _requires_approval_command(
        argv,
        approval_commands=approval_commands,
    )
    if (needs_approval or not is_allowed) and not approved:
        reason = "命令需要审批后执行。"
        if not is_allowed:
            reason = "命令不在直接允许列表内，需要审批后执行。"
        return _approval_required_result(argv, safe_cwd, reason)

    if not safe_cwd.exists():
        raise FileNotFoundError(f"工作目录不存在: {safe_cwd}")
    if not safe_cwd.is_dir():
        raise NotADirectoryError(f"工作目录不是目录: {safe_cwd}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须大于 0。")
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes 必须大于 0。")

    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            argv,
            cwd=safe_cwd,
            env={**os.environ, **(_normalize_env(env) or {})},
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = None
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")

    stdout, stdout_truncated = _truncate_text(stdout, max_output_bytes)
    stderr, stderr_truncated = _truncate_text(stderr, max_output_bytes)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    return {
        "status": "success" if returncode == 0 and not timed_out else "failed",
        "action": "run_shell_command",
        "command": argv,
        "cwd": str(safe_cwd),
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "elapsed_ms": elapsed_ms,
        "approval_required": False,
    }


def _to_tool_result(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)


def run_shell_command_tool_payload(
    command: str | list[str],
    cwd: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    approved: bool = False,
) -> str:
    return _to_tool_result(
        run_shell_command_core(
            command=command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            approved=approved,
        )
    )


if tool is not None:

    @tool
    def run_shell_command_tool(
        command: str,
        cwd: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        approved: bool = False,
    ) -> str:
        """执行一个允许列表内的本地 shell 命令，并返回 stdout/stderr/exit code。"""
        return run_shell_command_tool_payload(
            command=command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            approved=approved,
        )

else:  # pragma: no cover
    run_shell_command_tool = run_shell_command_tool_payload

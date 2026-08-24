"""Per-node resource limit helpers for sandboxed worker tool execution."""

from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import os
import signal
import time
from dataclasses import dataclass
from typing import Any


DEFAULT_LIMITS = {
    "timeout_seconds": 60,
    "max_memory_mb": 512,
    "max_output_size": 1024 * 1024,
    "max_retries": 2,
    "max_cpu_seconds": None,
}


TOOL_RESOURCE_LIMITS: dict[str, dict[str, Any]] = {
    "select_rows": {
        "timeout_seconds": 10,
        "max_output_size": 256 * 1024,
    },
    "select_rows_tool": {
        "timeout_seconds": 10,
        "max_output_size": 256 * 1024,
    },
    "select_rows_core": {
        "timeout_seconds": 10,
        "max_output_size": 256 * 1024,
    },
    "sqlite_select_rows": {
        "timeout_seconds": 10,
        "max_output_size": 256 * 1024,
    },
    "minimax_tts": {
        "timeout_seconds": 200,
        "max_output_size": 10 * 1024 * 1024,
    },
    "minimax_tts_tool": {
        "timeout_seconds": 200,
        "max_output_size": 10 * 1024 * 1024,
    },
    "minimax_tts_core": {
        "timeout_seconds": 200,
        "max_output_size": 10 * 1024 * 1024,
    },
    "minimax_tts_http": {
        "timeout_seconds": 200,
        "max_output_size": 10 * 1024 * 1024,
    },
    "minimax_generate_image": {
        "timeout_seconds": 330,
        "max_output_size": 20 * 1024 * 1024,
    },
    "minimax_generate_image_tool": {
        "timeout_seconds": 330,
        "max_output_size": 20 * 1024 * 1024,
    },
    "minimax_generate_image_core": {
        "timeout_seconds": 330,
        "max_output_size": 20 * 1024 * 1024,
    },
    "minimax_image_http": {
        "timeout_seconds": 330,
        "max_output_size": 20 * 1024 * 1024,
    },
    "run_shell_command": {
        "timeout_seconds": 30,
        "max_output_size": 256 * 1024,
        "max_retries": 0,
        "max_cpu_seconds": 30,
    },
    "run_shell_command_tool": {
        "timeout_seconds": 30,
        "max_output_size": 256 * 1024,
        "max_retries": 0,
        "max_cpu_seconds": 30,
    },
    "run_shell_command_core": {
        "timeout_seconds": 30,
        "max_output_size": 256 * 1024,
        "max_retries": 0,
        "max_cpu_seconds": 30,
    },
}


@dataclass(frozen=True)
class NodeResourceLimits:
    timeout_seconds: float | None = DEFAULT_LIMITS["timeout_seconds"]
    max_memory_mb: int | None = DEFAULT_LIMITS["max_memory_mb"]
    max_output_size: int | None = DEFAULT_LIMITS["max_output_size"]
    max_retries: int = DEFAULT_LIMITS["max_retries"]
    max_cpu_seconds: int | None = None

    @classmethod
    def from_node(cls, node: dict[str, Any]) -> "NodeResourceLimits":
        raw = resolve_resource_limits_for_node(node)
        for key in ("timeout_seconds", "max_memory_mb", "max_output_size", "max_retries", "max_cpu_seconds"):
            if key in node and node[key] is not None:
                raw[key] = node[key]
        return cls(
            timeout_seconds=_optional_float(raw.get("timeout_seconds")),
            max_memory_mb=_optional_int(raw.get("max_memory_mb")),
            max_output_size=_optional_int(raw.get("max_output_size")),
            max_retries=int(raw.get("max_retries", DEFAULT_LIMITS["max_retries"])),
            max_cpu_seconds=_optional_int(raw.get("max_cpu_seconds")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_memory_mb": self.max_memory_mb,
            "max_output_size": self.max_output_size,
            "max_retries": self.max_retries,
            "max_cpu_seconds": self.max_cpu_seconds,
        }


def build_resource_limits(
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build default resource limits for one executable node."""
    return {
        **DEFAULT_LIMITS,
        **TOOL_RESOURCE_LIMITS.get(tool_name or "", {}),
        **(overrides or {}),
    }


def resolve_resource_limits_for_node(node: dict[str, Any]) -> dict[str, Any]:
    """Resolve resource limits from defaults, tool policy, legacy limits, and sandbox config."""
    sandbox = node.get("sandbox") or {}
    sandbox_limits = sandbox.get("resource_limits") or {}
    return build_resource_limits(
        tool_name=node.get("tool"),
        tool_input=node.get("input") or {},
        overrides={
            **(node.get("limits") or {}),
            **sandbox_limits,
        },
    )


def run_with_limits(dispatcher: Any, node: dict[str, Any]) -> dict[str, Any]:
    """Run one dispatcher call in a child process and enforce node resource limits."""
    limits = NodeResourceLimits.from_node(node)
    started = time.monotonic()
    queue: mp.Queue = mp.Queue(maxsize=1)
    process = mp.Process(target=_child_main, args=(dispatcher, node, limits.as_dict(), queue))
    process.start()
    process.join(limits.timeout_seconds)

    if process.is_alive():
        _kill_process(process)
        return _limit_failure(node, "timeout", f"节点执行超过 {limits.timeout_seconds} 秒。", limits, started)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    if process.exitcode and process.exitcode != 0:
        return _limit_failure(
            node,
            "process_exit",
            f"工具子进程异常退出，exitcode={process.exitcode}。",
            limits,
            started,
            extra={"exitcode": process.exitcode, "elapsed_ms": elapsed_ms},
        )

    try:
        result = queue.get_nowait()
    except Exception:
        return _limit_failure(node, "missing_result", "工具子进程没有写回执行结果。", limits, started)

    result = _enforce_output_size(result, node, limits, started)
    result.setdefault("resource_limits", limits.as_dict())
    result.setdefault("metrics", {})["elapsed_ms"] = elapsed_ms
    return result


def _child_main(dispatcher: Any, node: dict[str, Any], raw_limits: dict[str, Any], queue: mp.Queue) -> None:
    _apply_process_limits(raw_limits)
    try:
        if hasattr(dispatcher, "dispatch_async"):
            result = asyncio.run(dispatcher.dispatch_async(node))
        else:
            result = dispatcher.dispatch(node)
        queue.put(result)
    except BaseException as exc:  # noqa: BLE001 - child must always report structured failure
        queue.put({
            "status": "failed",
            "node_id": node.get("id"),
            "tool": node.get("tool"),
            "error": str(exc),
            "error_type": type(exc).__name__,
        })


def _apply_process_limits(raw_limits: dict[str, Any]) -> None:
    try:
        import resource
    except ImportError:
        return

    memory_mb = raw_limits.get("max_memory_mb")
    if memory_mb is not None:
        memory_bytes = int(memory_mb) * 1024 * 1024
        _set_limit(resource.RLIMIT_AS, memory_bytes)

    cpu_seconds = raw_limits.get("max_cpu_seconds")
    if cpu_seconds is not None:
        _set_limit(resource.RLIMIT_CPU, int(cpu_seconds))


def _set_limit(limit_name: int, value: int) -> None:
    import resource

    try:
        soft, hard = value, value
        current_soft, current_hard = resource.getrlimit(limit_name)  # type: ignore[name-defined]
        if current_hard not in {-1, resource.RLIM_INFINITY}:  # type: ignore[name-defined]
            hard = min(value, int(current_hard))
            soft = min(value, hard)
        resource.setrlimit(limit_name, (soft, hard))  # type: ignore[name-defined]
    except Exception:
        pass


def _enforce_output_size(
    result: dict[str, Any],
    node: dict[str, Any],
    limits: NodeResourceLimits,
    started: float,
) -> dict[str, Any]:
    if limits.max_output_size is None:
        return result
    try:
        output_size = len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return _limit_failure(node, "output_serialization", "工具输出无法 JSON 序列化。", limits, started)
    if output_size <= limits.max_output_size:
        result.setdefault("metrics", {})["output_size_bytes"] = output_size
        return result
    return _limit_failure(
        node,
        "output_size",
        f"工具输出 {output_size} bytes 超过限制 {limits.max_output_size} bytes。",
        limits,
        started,
        extra={"output_size_bytes": output_size},
    )


def _kill_process(process: mp.Process) -> None:
    if process.pid is not None:
        try:
            os.kill(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.join(timeout=1)


def _limit_failure(
    node: dict[str, Any],
    reason: str,
    error: str,
    limits: NodeResourceLimits,
    started: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metrics = {"elapsed_ms": int((time.monotonic() - started) * 1000)}
    if extra:
        metrics.update(extra)
    return {
        "status": "failed",
        "node_id": node.get("id"),
        "tool": node.get("tool"),
        "error": error,
        "error_type": "ResourceLimitExceeded",
        "failure_reason": reason,
        "resource_limits": limits.as_dict(),
        "metrics": metrics,
    }


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)

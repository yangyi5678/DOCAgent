"""Rules for deciding when a DAG result should trigger automatic replanning."""

from __future__ import annotations

from typing import Any


REPLANABLE_ERROR_TYPES = {
    "FileNotFoundError",
    "ConnectionError",
    "TimeoutError",
    "ToolUnavailableError",
    "PermissionError",
}

REPLANABLE_ERROR_STAGES = {
    "tool_dispatch",
    "tool_execution",
    "network_policy",
    "filesystem_policy",
    "process_policy",
}


def analyze_result_for_replan(
    *,
    runtime_state: dict[str, Any],
    node_id: str,
    node: dict[str, Any],
    result: dict[str, Any],
    retry_exhausted: bool = False,
) -> dict[str, Any] | None:
    """Return a replan request when a node result invalidates the current DAG."""
    explicit = _explicit_replan_request(node_id, node, result)
    if explicit:
        return explicit

    if result.get("status") != "failed" or not retry_exhausted:
        return None

    if result.get("replanable") is False:
        return None

    error_type = str(result.get("error_type") or "")
    error_stage = str(result.get("error_stage") or "")
    retryable = result.get("retryable")

    should_replan = (
        result.get("replanable") is True
        or retryable is not False
        or error_type in REPLANABLE_ERROR_TYPES
        or error_stage in REPLANABLE_ERROR_STAGES
    )
    if not should_replan:
        return None

    return {
        "reason": "tool_failed_after_retries",
        "node_id": node_id,
        "tool": result.get("tool") or node.get("tool"),
        "capability": node.get("capability"),
        "error": result.get("error"),
        "error_type": result.get("error_type"),
        "error_stage": result.get("error_stage"),
        "retryable": retryable,
        "suggested_action": result.get("suggested_action") or "try_alternative_plan",
        "completed": list(runtime_state.get("completed", [])),
        "failed": list(runtime_state.get("failed", [])),
    }


def _explicit_replan_request(
    node_id: str,
    node: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any] | None:
    result_payload = result.get("result")
    if not isinstance(result_payload, dict):
        result_payload = {}

    needs_replan = (
        result.get("needs_replan") is True
        or result.get("replanable") is True
        or result_payload.get("needs_replan") is True
        or result_payload.get("replanable") is True
    )
    if not needs_replan:
        return None

    return {
        "reason": (
            result_payload.get("reason")
            or result.get("replan_reason")
            or "tool_requested_replan"
        ),
        "node_id": node_id,
        "tool": result.get("tool") or node.get("tool"),
        "capability": node.get("capability"),
        "error": result.get("error"),
        "observation": result_payload.get("observation") or result_payload,
        "suggested_action": (
            result_payload.get("suggested_action")
            or result.get("suggested_action")
            or "replan_with_observation"
        ),
    }

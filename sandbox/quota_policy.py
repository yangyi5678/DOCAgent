"""Quota and cost sandbox policy defaults."""

from __future__ import annotations

from typing import Any


DEFAULT_QUOTA_POLICY = {
    "max_api_calls": None,
    "max_tokens": None,
    "max_image_generations": None,
    "max_tts_generations": None,
    "max_estimated_cost_usd": None,
}


TOOL_QUOTA_POLICIES: dict[str, dict[str, Any]] = {
    "minimax_tts": {
        "max_api_calls": 1,
        "max_tts_generations": 1,
    },
    "minimax_tts_tool": {
        "max_api_calls": 1,
        "max_tts_generations": 1,
    },
    "minimax_tts_core": {
        "max_api_calls": 1,
        "max_tts_generations": 1,
    },
    "minimax_generate_image": {
        "max_api_calls": 1,
        "max_image_generations": 1,
    },
    "minimax_generate_image_tool": {
        "max_api_calls": 1,
        "max_image_generations": 1,
    },
    "minimax_generate_image_core": {
        "max_api_calls": 1,
        "max_image_generations": 1,
    },
}


def build_quota_policy(
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build quota/cost policy for one executable node."""
    return {
        **DEFAULT_QUOTA_POLICY,
        **TOOL_QUOTA_POLICIES.get(tool_name or "", {}),
    }


def validate_quota_access_for_node(node: dict[str, Any]) -> None:
    """Validate per-node quota before dispatch."""
    tool_name = node.get("tool")
    tool_input = node.get("input") or {}
    if not isinstance(tool_input, dict):
        raise TypeError("节点 input 必须是 dict。")

    sandbox = node.get("sandbox") or {}
    policy = sandbox.get("quota") or build_quota_policy(tool_name, tool_input)
    ensure_quota_allowed(str(tool_name), policy)


def ensure_quota_allowed(
    tool_name: str,
    policy: dict[str, Any] | None = None,
    *,
    api_calls: int = 1,
    image_generations: int = 0,
    tts_generations: int = 0,
    tokens: int = 0,
    estimated_cost_usd: float = 0.0,
) -> None:
    """Raise PermissionError if one call exceeds its quota policy."""
    policy = policy or build_quota_policy(tool_name)
    _ensure_quota_value("max_api_calls", api_calls, policy)
    _ensure_quota_value("max_image_generations", image_generations, policy)
    _ensure_quota_value("max_tts_generations", tts_generations, policy)
    _ensure_quota_value("max_tokens", tokens, policy)
    _ensure_quota_value("max_estimated_cost_usd", estimated_cost_usd, policy)


def _ensure_quota_value(key: str, requested: int | float, policy: dict[str, Any]) -> None:
    limit = policy.get(key)
    if limit is not None and requested > limit:
        raise PermissionError(f"配额超过限制: {key} requested={requested} limit={limit}")

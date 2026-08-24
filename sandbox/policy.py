"""Unified sandbox policy builder for executable DAG nodes."""

from __future__ import annotations

from typing import Any

from sandbox.audit import build_audit_policy
from sandbox.database_policy import build_database_policy
from sandbox.filesystem_policy import build_filesystem_policy
from sandbox.network_policy import build_network_policy
from sandbox.permission_policy import build_permission_policy
from sandbox.process_policy import build_process_policy
from sandbox.quota_policy import build_quota_policy
from sandbox.resource_limits import build_resource_limits


def build_sandbox_config_for_node(
    capability: dict[str, Any] | None = None,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the sandbox section attached to one executable DAG node."""
    tool_input = tool_input or {}
    return {
        "policy_version": 1,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "filesystem": build_filesystem_policy(tool_name, tool_input),
        "database": build_database_policy(tool_name, tool_input),
        "network": build_network_policy(tool_name, tool_input),
        "process": build_process_policy(tool_name, tool_input),
        "resource_limits": build_resource_limits(tool_name, tool_input),
        "quota": build_quota_policy(tool_name, tool_input),
        "permission": build_permission_policy(tool_name, tool_input),
        "audit": build_audit_policy(tool_name, tool_input),
    }

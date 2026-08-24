"""Sandbox audit policy defaults."""

from __future__ import annotations

from typing import Any


DEFAULT_AUDIT_POLICY = {
    "enabled": True,
}


def build_audit_policy(
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build audit/logging policy for one executable node."""
    return dict(DEFAULT_AUDIT_POLICY)

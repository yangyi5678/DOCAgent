"""Candidate source lookup for capability acquisition.

This first version keeps source discovery deterministic and side-effect free.
Network/package/plugin installation can be added behind these functions later.
"""

from __future__ import annotations

import re

from capability_acquisition.types import AcquisitionPlan, CapabilityGap, ToolCandidate


def find_candidates(gap: CapabilityGap, plan: AcquisitionPlan) -> list[ToolCandidate]:
    """Return candidate tools for the selected acquisition plan."""
    if plan.strategy == "known_plugin":
        return search_known_plugins(gap)
    if plan.strategy == "local_existing_tool":
        return search_available_tools_snapshot(gap)
    if plan.strategy == "generate_code":
        return build_generated_code_candidate(gap)
    if plan.strategy == "external_download":
        return search_external_tool_candidates(gap)
    return []


def search_known_plugins(gap: CapabilityGap) -> list[ToolCandidate]:
    """Return plugin candidates from a small built-in domain map."""
    domain = gap.missing_capability.split(".", 1)[0]
    plugin_name = {
        "calendar": "calendar_plugin",
        "email": "email_plugin",
        "drive": "drive_plugin",
        "figma": "figma_plugin",
        "github": "github_plugin",
        "notion": "notion_plugin",
        "slack": "slack_plugin",
    }.get(domain)
    if plugin_name is None:
        return []
    return [
        ToolCandidate(
            name=plugin_name,
            description=f"Install or connect a known plugin for {domain} capabilities.",
            source_type="plugin",
            capabilities=[gap.missing_capability],
            input_schema=gap.required_io.input_schema,
            output_schema=gap.required_io.output_schema,
            risk="medium",
            confidence=0.7,
            metadata={"domain": domain, "requires_external_connection": True},
        )
    ]


def search_available_tools_snapshot(gap: CapabilityGap) -> list[ToolCandidate]:
    """Wrap a matching already-visible tool snapshot as a candidate."""
    candidates: list[ToolCandidate] = []
    for tool in gap.available_tools_snapshot:
        capabilities = tool.get("capabilities") or []
        if gap.missing_capability not in capabilities:
            continue
        name = str(tool.get("name") or "existing_tool")
        candidates.append(
            ToolCandidate(
                name=name,
                description=str(tool.get("description") or "Existing tool from snapshot."),
                source_type="local_existing_tool",
                capabilities=list(capabilities),
                input_schema=dict(tool.get("input_schema") or gap.required_io.input_schema),
                output_schema=dict(tool.get("output_schema") or gap.required_io.output_schema),
                risk="low",
                confidence=0.85,
                metadata={"snapshot": tool},
            )
        )
    return candidates


def build_generated_code_candidate(gap: CapabilityGap) -> list[ToolCandidate]:
    """Prepare a generated-code candidate manifest without writing code yet."""
    safe_name = _safe_tool_name(gap.missing_capability)
    return [
        ToolCandidate(
            name=safe_name,
            description=f"Generated local Python tool for {gap.missing_capability}.",
            source_type="generated_code",
            capabilities=[gap.missing_capability],
            input_schema=gap.required_io.input_schema,
            output_schema=gap.required_io.output_schema,
            artifact_path=f"generated_tools/{safe_name}.py",
            entrypoint=f"{safe_name}_core",
            risk="medium",
            confidence=0.45,
            metadata={
                "generation_status": "manifest_only",
                "step_objective": gap.step.get("objective"),
            },
        )
    ]


def search_external_tool_candidates(gap: CapabilityGap) -> list[ToolCandidate]:
    """Return a placeholder external candidate requiring approval and review."""
    safe_name = _safe_tool_name(gap.missing_capability)
    return [
        ToolCandidate(
            name=f"{safe_name}_external",
            description=f"External package or MCP candidate for {gap.missing_capability}.",
            source_type="external_package",
            capabilities=[gap.missing_capability],
            input_schema=gap.required_io.input_schema,
            output_schema=gap.required_io.output_schema,
            install_method={"type": "requires_discovery"},
            risk="high",
            confidence=0.25,
            metadata={"requires_network": True, "requires_package_review": True},
        )
    ]


def _safe_tool_name(capability: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", capability).strip("_").lower()
    if not normalized:
        return "generated_capability_tool"
    if normalized[0].isdigit():
        normalized = f"tool_{normalized}"
    return normalized

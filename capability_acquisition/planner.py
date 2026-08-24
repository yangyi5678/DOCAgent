"""Strategy selection for capability acquisition."""

from __future__ import annotations

from capability_acquisition.types import AcquisitionPlan, CapabilityGap


PLUGIN_CAPABILITY_PREFIXES = (
    "calendar.",
    "email.",
    "drive.",
    "figma.",
    "github.",
    "notion.",
    "slack.",
)


def plan_acquisition(gap: CapabilityGap) -> AcquisitionPlan:
    """Choose the simplest acquisition strategy allowed by the gap constraints."""
    capability = gap.missing_capability
    constraints = gap.constraints

    if _looks_like_plugin_capability(capability):
        return AcquisitionPlan(
            strategy="known_plugin",
            reason="capability matches a known plugin-backed domain",
            requires_approval=True,
            candidate_sources=["known_plugin_marketplace"],
        )

    if _available_snapshot_mentions_capability(gap):
        return AcquisitionPlan(
            strategy="local_existing_tool",
            reason="available tool snapshot already mentions the missing capability",
            requires_approval=False,
            candidate_sources=["available_tools_snapshot"],
        )

    if constraints.can_generate_code:
        return AcquisitionPlan(
            strategy="generate_code",
            reason="no known plugin or local tool matched; code generation is allowed",
            requires_approval=False,
            candidate_sources=["generated_code_builder"],
        )

    if constraints.network_allowed and constraints.can_install_package:
        return AcquisitionPlan(
            strategy="external_download",
            reason="code generation is not allowed, but package installation is allowed",
            requires_approval=True,
            candidate_sources=["package_registry", "mcp_registry", "web_search"],
        )

    return AcquisitionPlan(
        strategy="ask_user",
        reason="no allowed automatic acquisition strategy is available",
        requires_approval=True,
        candidate_sources=[],
    )


def _looks_like_plugin_capability(capability: str) -> bool:
    return capability.startswith(PLUGIN_CAPABILITY_PREFIXES)


def _available_snapshot_mentions_capability(gap: CapabilityGap) -> bool:
    for tool in gap.available_tools_snapshot:
        capabilities = tool.get("capabilities") or []
        if gap.missing_capability in capabilities:
            return True
    return False

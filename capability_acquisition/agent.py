"""Main entrypoint for the capability acquisition sub-agent."""

from __future__ import annotations

from typing import Any

from capability_acquisition.planner import plan_acquisition
from capability_acquisition.sources import find_candidates
from capability_acquisition.types import (
    AcquisitionResult,
    CapabilityGap,
    RegistrationRequest,
    ToolCandidate,
    ValidationReport,
)
from capability_acquisition.validators import select_best_valid_candidate


def run_capability_acquisition(
    gap: CapabilityGap | dict[str, Any],
) -> AcquisitionResult:
    """Acquire a missing capability candidate without registering it globally.

    This function is designed to become the callable behind a future
    ``capability_acquisition`` DAG node. It is side-effect free in the initial
    implementation: it only plans, finds manifest-level candidates, validates
    them, and returns a registration request when appropriate.
    """
    normalized_gap = (
        gap
        if isinstance(gap, CapabilityGap)
        else CapabilityGap.from_dict(gap)
    )
    plan = plan_acquisition(normalized_gap)
    candidates = find_candidates(normalized_gap, plan)
    candidate, validation = select_best_valid_candidate(candidates)

    if candidate is None:
        return AcquisitionResult(
            request_id=normalized_gap.request_id,
            status="failed",
            strategy=plan.strategy,
            capability=normalized_gap.missing_capability,
            validation=validation,
            reason="no candidate tool found for missing capability",
            attempted_strategies=[plan.strategy],
            suggested_next_action="ask_user_for_tool_or_permission",
            metadata={"plan": plan.to_dict()},
        )

    if validation.status != "passed":
        return AcquisitionResult(
            request_id=normalized_gap.request_id,
            status="failed",
            strategy=plan.strategy,
            capability=normalized_gap.missing_capability,
            candidate_tool=candidate,
            validation=validation,
            reason="candidate validation failed",
            attempted_strategies=[plan.strategy],
            suggested_next_action="ask_user_for_tool_or_permission",
            metadata={"plan": plan.to_dict()},
        )

    if _requires_external_approval(candidate, plan.requires_approval):
        return AcquisitionResult(
            request_id=normalized_gap.request_id,
            status="needs_approval",
            strategy=plan.strategy,
            capability=normalized_gap.missing_capability,
            candidate_tool=candidate,
            validation=validation,
            reason="candidate requires user approval before registration",
            attempted_strategies=[plan.strategy],
            suggested_next_action="request_user_approval",
            metadata={"plan": plan.to_dict()},
        )

    registration_request = _build_registration_request(
        gap=normalized_gap,
        candidate=candidate,
        validation=validation,
    )
    return AcquisitionResult(
        request_id=normalized_gap.request_id,
        status="ready_for_registration",
        strategy=plan.strategy,
        capability=normalized_gap.missing_capability,
        candidate_tool=candidate,
        validation=validation,
        registration_request=registration_request,
        attempted_strategies=[plan.strategy],
        suggested_next_action="submit_registration_request",
        metadata={"plan": plan.to_dict()},
    )


def _requires_external_approval(candidate: ToolCandidate, plan_requires_approval: bool) -> bool:
    return (
        plan_requires_approval
        or candidate.source_type in {"plugin", "external_package"}
        or candidate.risk == "high"
    )


def _build_registration_request(
    *,
    gap: CapabilityGap,
    candidate: ToolCandidate,
    validation: ValidationReport,
) -> RegistrationRequest:
    manifest = {
        "name": candidate.name,
        "description": candidate.description,
        "source_type": candidate.source_type,
        "capabilities": candidate.capabilities,
        "input_schema": candidate.input_schema,
        "output_schema": candidate.output_schema,
        "artifact_path": candidate.artifact_path,
        "entrypoint": candidate.entrypoint,
        "install_method": candidate.install_method,
        "risk": candidate.risk,
        "confidence": candidate.confidence,
        "metadata": {
            **candidate.metadata,
            "request_id": gap.request_id,
            "session_id": gap.session_id,
            "task_id": gap.task_id,
            "validation": validation.to_dict(),
        },
    }
    return RegistrationRequest(
        action="register_tool",
        lifecycle=gap.constraints.lifecycle,
        trust_level="reviewed",
        tool_manifest=manifest,
    )

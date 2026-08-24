"""Validation helpers for capability acquisition candidates."""

from __future__ import annotations

from capability_acquisition.types import ToolCandidate, ValidationReport


def validate_candidate(candidate: ToolCandidate) -> ValidationReport:
    """Run lightweight manifest-level validation for a candidate tool."""
    errors: list[str] = []
    warnings: list[str] = []
    tests_run = ["manifest_required_fields", "schema_shape", "risk_gate"]

    if not candidate.name:
        errors.append("candidate name is required")
    if not candidate.capabilities:
        errors.append("candidate must declare at least one capability")
    if not isinstance(candidate.input_schema, dict):
        errors.append("input_schema must be a dict")
    if not isinstance(candidate.output_schema, dict):
        errors.append("output_schema must be a dict")

    if candidate.source_type == "generated_code":
        if not candidate.artifact_path:
            errors.append("generated code candidate requires artifact_path")
        if not candidate.entrypoint:
            errors.append("generated code candidate requires entrypoint")
        warnings.append("generated code artifact is manifest-only until builder is implemented")

    if candidate.source_type in {"plugin", "external_package"}:
        warnings.append("candidate requires external approval before registration")

    return ValidationReport(
        status="failed" if errors else "passed",
        tests_run=tests_run,
        errors=errors,
        warnings=warnings,
    )


def select_best_valid_candidate(
    candidates: list[ToolCandidate],
) -> tuple[ToolCandidate | None, ValidationReport]:
    """Validate candidates and return the highest-confidence passing candidate."""
    best_candidate: ToolCandidate | None = None
    best_report: ValidationReport | None = None

    for candidate in sorted(candidates, key=lambda item: item.confidence, reverse=True):
        report = validate_candidate(candidate)
        if report.status == "passed":
            return candidate, report
        if best_report is None:
            best_candidate = candidate
            best_report = report

    return best_candidate, best_report or ValidationReport(
        status="failed",
        tests_run=[],
        errors=["no candidates were produced"],
    )

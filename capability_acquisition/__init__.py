"""Capability acquisition sub-agent package.

This package intentionally does not register tools by itself. It accepts a
capability gap, searches or prepares candidate tooling, validates the candidate,
and returns a registration request for the system-level dynamic registry.
"""

from capability_acquisition.agent import run_capability_acquisition
from capability_acquisition.types import (
    AcquisitionConstraints,
    AcquisitionPlan,
    AcquisitionResult,
    CapabilityGap,
    RegistrationRequest,
    RequiredIO,
    ToolCandidate,
    ValidationReport,
)

__all__ = [
    "AcquisitionConstraints",
    "AcquisitionPlan",
    "AcquisitionResult",
    "CapabilityGap",
    "RegistrationRequest",
    "RequiredIO",
    "ToolCandidate",
    "ValidationReport",
    "run_capability_acquisition",
]

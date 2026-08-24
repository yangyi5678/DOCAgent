"""Data contracts for the capability acquisition sub-agent.

The main agent should pass a small, explicit CapabilityGap into this package.
The result is an AcquisitionResult, which may include a RegistrationRequest.
Final tool registration is owned by the runtime-level dynamic registry, not by
this sub-agent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


JsonSchema = dict[str, Any]

AcquisitionStrategy = Literal[
    "known_plugin",
    "local_existing_tool",
    "generate_code",
    "external_download",
    "ask_user",
]

AcquisitionStatus = Literal[
    "ready_for_registration",
    "needs_approval",
    "failed",
]

ValidationStatus = Literal[
    "passed",
    "failed",
    "skipped",
]


@dataclass
class RequiredIO:
    """Expected input and output schemas for a missing capability."""

    input_schema: JsonSchema = field(default_factory=dict)
    output_schema: JsonSchema = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "RequiredIO":
        payload = payload or {}
        return cls(
            input_schema=dict(payload.get("input_schema") or {}),
            output_schema=dict(payload.get("output_schema") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AcquisitionConstraints:
    """Permission and lifecycle constraints for acquiring a new tool."""

    network_allowed: bool = False
    can_install_package: bool = False
    can_generate_code: bool = True
    requires_user_approval: bool = True
    lifecycle: Literal["session", "workspace", "global"] = "session"

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "AcquisitionConstraints":
        payload = payload or {}
        return cls(
            network_allowed=bool(payload.get("network_allowed", False)),
            can_install_package=bool(payload.get("can_install_package", False)),
            can_generate_code=bool(payload.get("can_generate_code", True)),
            requires_user_approval=bool(payload.get("requires_user_approval", True)),
            lifecycle=str(payload.get("lifecycle") or "session"),  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CapabilityGap:
    """Input from the main agent when planning finds no matching tool."""

    request_id: str
    missing_capability: str
    session_id: str | None = None
    task_id: str | None = None
    step: dict[str, Any] = field(default_factory=dict)
    required_io: RequiredIO = field(default_factory=RequiredIO)
    constraints: AcquisitionConstraints = field(default_factory=AcquisitionConstraints)
    available_tools_snapshot: list[dict[str, Any]] = field(default_factory=list)
    workspace_context: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CapabilityGap":
        return cls(
            request_id=str(payload["request_id"]),
            session_id=payload.get("session_id"),
            task_id=payload.get("task_id"),
            missing_capability=str(payload["missing_capability"]),
            step=dict(payload.get("step") or {}),
            required_io=RequiredIO.from_dict(payload.get("required_io")),
            constraints=AcquisitionConstraints.from_dict(payload.get("constraints")),
            available_tools_snapshot=list(payload.get("available_tools_snapshot") or []),
            workspace_context=dict(payload.get("workspace_context") or {}),
            metadata=dict(payload.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "missing_capability": self.missing_capability,
            "step": self.step,
            "required_io": self.required_io.to_dict(),
            "constraints": self.constraints.to_dict(),
            "available_tools_snapshot": self.available_tools_snapshot,
            "workspace_context": self.workspace_context,
            "metadata": self.metadata,
        }


@dataclass
class AcquisitionPlan:
    """Strategy selected by the capability acquisition sub-agent."""

    strategy: AcquisitionStrategy
    reason: str
    requires_approval: bool = False
    candidate_sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolCandidate:
    """A tool candidate discovered or prepared by the acquisition flow."""

    name: str
    description: str
    source_type: Literal[
        "plugin",
        "local_existing_tool",
        "generated_code",
        "external_package",
        "unknown",
    ]
    capabilities: list[str]
    input_schema: JsonSchema = field(default_factory=dict)
    output_schema: JsonSchema = field(default_factory=dict)
    artifact_path: str | None = None
    entrypoint: str | None = None
    install_method: dict[str, Any] = field(default_factory=dict)
    risk: Literal["low", "medium", "high"] = "medium"
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationReport:
    """Validation result for a candidate tool."""

    status: ValidationStatus
    tests_run: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RegistrationRequest:
    """Request handed to the system-level dynamic tool registry."""

    action: Literal["register_tool"]
    lifecycle: Literal["session", "workspace", "global"]
    trust_level: Literal["trusted", "reviewed", "untrusted"]
    tool_manifest: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AcquisitionResult:
    """Output returned to the main agent/runtime."""

    request_id: str
    status: AcquisitionStatus
    strategy: AcquisitionStrategy
    capability: str
    candidate_tool: ToolCandidate | None = None
    validation: ValidationReport = field(default_factory=lambda: ValidationReport("skipped"))
    registration_request: RegistrationRequest | None = None
    reason: str | None = None
    attempted_strategies: list[AcquisitionStrategy] = field(default_factory=list)
    suggested_next_action: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "strategy": self.strategy,
            "capability": self.capability,
            "candidate_tool": (
                self.candidate_tool.to_dict()
                if self.candidate_tool is not None
                else None
            ),
            "validation": self.validation.to_dict(),
            "registration_request": (
                self.registration_request.to_dict()
                if self.registration_request is not None
                else None
            ),
            "reason": self.reason,
            "attempted_strategies": list(self.attempted_strategies),
            "suggested_next_action": self.suggested_next_action,
            "metadata": self.metadata,
        }

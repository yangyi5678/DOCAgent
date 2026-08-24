from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


class LLMClient(Protocol):
    """Optional LLM adapter used by skills components.

    Input:
        A JSON-serializable payload describing the local rule result and the
        current decision target.

    Output:
        A JSON string or JSON-like Python object. Callers must treat it as a
        suggestion and validate it against registry data before using it.

    Framework role:
        This protocol lets selector/loader/router use LLM reranking or
        summarization without coupling the skills package to any provider SDK.
    """

    def __call__(self, payload: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class SkillTriggers:
    """Rules used to recall a skill before planning.

    Input source:
        Usually read from skill.yaml. It may be supplemented by SKILL.md
        frontmatter for legacy skills.

    Output/use:
        SkillSelector compares these fields with Goal IR text/intents/domains
        to produce SkillMatch records.
    """

    keywords: list[str] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillCapabilities:
    """Capability contract declared by a skill.

    provides:
        Capabilities the skill can help the planner choose, such as
        domain.logs.analyze.

    requires:
        Capabilities that must exist for the skill to be fully usable.

    Framework role:
        Skill capabilities are hints for planner decomposition. ToolRegistry is
        still the source of truth for executable capabilities.
    """

    provides: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillSpec:
    """Registry record for one domain skill.

    Input:
        Built from skill.yaml or legacy SKILL.md frontmatter by SkillRegistry.

    Output/use:
        The registry exposes SkillSpec to selector/loader/router. A SkillSpec is
        metadata only; it is not executable and must not be dispatched as a tool.

    Framework chain:
        skill.yaml -> SkillSpec -> SkillSelector -> SkillLoader ->
        ReferenceRouter -> ContextManager planner context.
    """

    name: str
    description: str
    root_dir: Path
    skill_path: Path
    reference_dir: Path | None = None
    triggers: SkillTriggers = field(default_factory=SkillTriggers)
    capabilities: SkillCapabilities = field(default_factory=SkillCapabilities)
    recommended_step_kinds: list[str] = field(default_factory=list)
    default_resources: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    manifest_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("root_dir", "skill_path", "reference_dir", "manifest_path"):
            value = data.get(key)
            data[key] = str(value) if value is not None else None
        return data


@dataclass(frozen=True)
class SkillMatch:
    """Skill selection result for one goal.

    Input:
        Produced by SkillSelector from Goal IR and SkillSpec triggers.

    Output/use:
        ContextBuilder uses matches to decide which skills to load. Planner sees
        serialized matches as evidence for why a skill context was injected.
    """

    skill_name: str
    score: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SkillContext:
    """Planner-facing summary compiled from SKILL.md.

    Input:
        SkillLoader reads markdown and optional LLM summaries.

    Output/use:
        ContextManager injects this into planner context. Planner uses
        workflow_hints and recommended_step_kinds to choose step kinds, and
        capability_hints to expand capability candidates.
    """

    skill_name: str
    description: str
    workflow_hints: list[str] = field(default_factory=list)
    term_mappings: dict[str, str] = field(default_factory=dict)
    routing_rules: list[dict[str, Any]] = field(default_factory=list)
    grep_hints: list[str] = field(default_factory=list)
    capability_hints: list[str] = field(default_factory=list)
    recommended_step_kinds: list[str] = field(default_factory=list)
    default_resources: dict[str, Any] = field(default_factory=dict)
    output_contract: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None
    llm_notes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReferenceContext:
    """Relevant reference snippets selected for a goal.

    Input:
        ReferenceRouter combines skill routing rules, keyword retrieval, and
        optional LLM reranking.

    Output/use:
        Planner/tool argument binding/final diagnosis can consume snippets,
        selected_references, and grep_hints without loading all reference files.
    """

    skill_name: str
    selected_references: list[str] = field(default_factory=list)
    snippets: list[dict[str, Any]] = field(default_factory=list)
    grep_hints: list[str] = field(default_factory=list)
    llm_notes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SkillContextBundle:
    """Complete skill payload passed into planning context.

    Input:
        Built by SkillContextBuilder from matches, skill contexts, and reference
        contexts.

    Output/use:
        ContextManager serializes this bundle into planner_context. Planner uses
        capability_hints/recommended_step_kinds/default_resources as structured
        guidance; no tool is executed by this object.
    """

    matched_skills: list[SkillMatch] = field(default_factory=list)
    skill_contexts: dict[str, SkillContext] = field(default_factory=dict)
    reference_contexts: dict[str, ReferenceContext] = field(default_factory=dict)
    capability_hints: list[str] = field(default_factory=list)
    recommended_step_kinds: list[str] = field(default_factory=list)
    default_resources: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched_skills": [match.to_dict() for match in self.matched_skills],
            "skill_contexts": {
                name: context.to_dict()
                for name, context in self.skill_contexts.items()
            },
            "reference_contexts": {
                name: context.to_dict()
                for name, context in self.reference_contexts.items()
            },
            "capability_hints": list(self.capability_hints),
            "recommended_step_kinds": list(self.recommended_step_kinds),
            "default_resources": dict(self.default_resources),
        }


JsonObject = dict[str, Any]
JsonLikeCallback = Callable[[JsonObject], Any]

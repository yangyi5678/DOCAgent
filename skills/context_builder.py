from __future__ import annotations

from typing import Any

from .loader import SkillLoader
from .reference_router import ReferenceRouter
from .registry import SkillRegistry
from .selector import SkillSelector
from .types import SkillContext, SkillContextBundle


class SkillContextBuilder:
    """Build the skill bundle injected into planner context.

    Input:
        Goal IR plus registry/selector/loader/router components.

    Output:
        SkillContextBundle containing matched skills, loaded skill contexts,
        reference contexts, merged capability hints, recommended step kinds, and
        default resources.

    Logic:
        1. Select matching skills.
        2. Load SKILL.md context for each match.
        3. Route relevant references for each context.
        4. Merge planner-facing hints.

    Framework role:
        This is the single high-level object ContextManager should call. It
        keeps skill mechanics out of ContextManager while returning a stable,
        serializable planner-context payload.
    """

    def __init__(
        self,
        *,
        registry: SkillRegistry,
        selector: SkillSelector | None = None,
        loader: SkillLoader | None = None,
        router: ReferenceRouter | None = None,
    ) -> None:
        self.registry = registry
        self.selector = selector or SkillSelector()
        self.loader = loader or SkillLoader()
        self.router = router or ReferenceRouter()

    def build_for_goal(self, goal: dict[str, Any], *, limit: int = 2) -> SkillContextBundle:
        """Build a complete skill context bundle for one goal.

        Input:
            goal: Goal IR.
            limit: Maximum number of skills to load.

        Output:
            SkillContextBundle. Empty bundle means no skill branch applies and
            the normal planner chain should proceed unchanged.
        """
        matches = self.selector.select(goal, self.registry, limit=limit)
        bundle = SkillContextBundle(matched_skills=matches)
        for match in matches:
            spec = self.registry.get(match.skill_name)
            skill_context = self.loader.load(spec, goal)
            reference_context = self.router.route(goal, spec, skill_context)
            bundle.skill_contexts[spec.name] = skill_context
            bundle.reference_contexts[spec.name] = reference_context
        bundle.capability_hints = merge_capability_hints(bundle.skill_contexts.values())
        bundle.recommended_step_kinds = merge_recommended_step_kinds(bundle.skill_contexts.values())
        bundle.default_resources = merge_default_resources(bundle.skill_contexts.values())
        return bundle


def merge_capability_hints(contexts: Any) -> list[str]:
    """Merge capability hints from loaded skill contexts."""
    result: list[str] = []
    for context in contexts:
        result.extend(getattr(context, "capability_hints", []))
    return _dedupe(result)


def merge_recommended_step_kinds(contexts: Any) -> list[str]:
    """Merge step-kind hints from loaded skill contexts."""
    result: list[str] = []
    for context in contexts:
        result.extend(getattr(context, "recommended_step_kinds", []))
    return _dedupe(result)


def merge_default_resources(contexts: Any) -> dict[str, Any]:
    """Merge default resources for argument binding.

    Later skill contexts override earlier keys, which lets more specific skills
    refine generic defaults.
    """
    merged: dict[str, Any] = {}
    for context in contexts:
        merged.update(getattr(context, "default_resources", {}) or {})
    return merged


def serialize_skill_context_bundle(bundle: SkillContextBundle) -> dict[str, Any]:
    """Return a JSON-ready representation for planner_context."""
    return bundle.to_dict()


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result

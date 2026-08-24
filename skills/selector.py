from __future__ import annotations

import json
import re
from typing import Any

from .registry import SkillRegistry
from .types import LLMClient, SkillMatch, SkillSpec


class SkillSelector:
    """Select relevant skills for a Goal IR.

    Input:
        Goal IR dictionary and SkillRegistry.

    Output:
        Ranked SkillMatch records.

    Logic:
        1. Rule scoring by trigger keywords, intents, domains, and description.
        2. Optional LLM rerank over rule-recalled candidates.
        3. Registry names remain authoritative; LLM cannot invent skills.

    Framework role:
        Runs before planner context assembly. It decides which skill contexts
        should be loaded for the current goal.
    """

    def __init__(self, llm_client: LLMClient | None = None, *, min_score: float = 0.2) -> None:
        self.llm_client = llm_client
        self.min_score = min_score

    def select(
        self,
        goal: dict[str, Any],
        registry: SkillRegistry,
        *,
        limit: int = 3,
    ) -> list[SkillMatch]:
        """Return top matching skills.

        Input:
            goal: Goal IR from goalparser/planner.
            registry: Loaded SkillRegistry.
            limit: Maximum number of skills to return.

        Output:
            Sorted SkillMatch list. Empty means normal non-skill planning.
        """
        rule_matches = [
            match
            for spec in registry.iter_specs()
            if (match := score_skill(goal, spec)).score >= self.min_score
        ]
        rule_matches.sort(key=lambda item: item.score, reverse=True)
        if self.llm_client and rule_matches:
            return self._llm_rerank(goal, registry, rule_matches, limit=limit)
        return rule_matches[:limit]

    def _llm_rerank(
        self,
        goal: dict[str, Any],
        registry: SkillRegistry,
        matches: list[SkillMatch],
        *,
        limit: int,
    ) -> list[SkillMatch]:
        payload = {
            "task": "rerank_skill_matches",
            "goal": goal,
            "rule_matches": [match.to_dict() for match in matches],
            "candidate_skills": [
                {
                    "name": registry.get(match.skill_name).name,
                    "description": registry.get(match.skill_name).description,
                    "triggers": {
                        "keywords": registry.get(match.skill_name).triggers.keywords,
                        "intents": registry.get(match.skill_name).triggers.intents,
                        "domains": registry.get(match.skill_name).triggers.domains,
                    },
                }
                for match in matches
            ],
            "instructions": (
                "Return JSON with matches=[{skill_name, score, reasons}]. "
                "Only use candidate skill_name values. Do not invent skills."
            ),
        }
        try:
            raw = self.llm_client(payload)
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return matches[:limit]
        allowed = {match.skill_name for match in matches}
        reranked: list[SkillMatch] = []
        for item in (parsed or {}).get("matches", []):
            name = str(item.get("skill_name") or "")
            if name not in allowed:
                continue
            score = _as_float(item.get("score"), default=0.0)
            reasons = [str(reason) for reason in item.get("reasons", []) if str(reason).strip()]
            reranked.append(SkillMatch(skill_name=name, score=score, reasons=reasons or ["llm_rerank"]))
        if not reranked:
            return matches[:limit]
        reranked.sort(key=lambda item: item.score, reverse=True)
        return reranked[:limit]


def score_skill(goal: dict[str, Any], spec: SkillSpec) -> SkillMatch:
    """Score one skill against one goal using deterministic rules.

    Input:
        goal: Goal IR.
        spec: Skill metadata.

    Output:
        SkillMatch with score and evidence reasons.

    Logic:
        Keyword matches are the strongest signal; intent/domain matches support
        broader routing. Description overlap is a weak fallback.
    """
    text = extract_goal_text(goal)
    lowered = text.lower()
    goal_intents = {str(intent) for intent in goal.get("intents", [])}
    goal_domains = {str(domain) for domain in goal.get("domains", [])}
    reasons: list[str] = []
    score = 0.0

    for keyword in spec.triggers.keywords:
        if keyword and keyword.lower() in lowered:
            score += 0.4
            reasons.append(f"keyword:{keyword}")
    for intent in spec.triggers.intents:
        if intent in goal_intents:
            score += 0.3
            reasons.append(f"intent:{intent}")
    for domain in spec.triggers.domains:
        if domain in goal_domains or domain.lower() in lowered:
            score += 0.2
            reasons.append(f"domain:{domain}")

    desc_tokens = _tokens(spec.description)
    text_tokens = set(_tokens(text))
    overlap = [token for token in desc_tokens if token in text_tokens]
    if overlap:
        score += min(0.1, 0.02 * len(overlap))
        reasons.append("description_overlap:" + ",".join(overlap[:5]))

    return SkillMatch(skill_name=spec.name, score=min(score, 1.0), reasons=reasons)


def extract_goal_text(goal: dict[str, Any]) -> str:
    """Extract human text from Goal IR."""
    for key in ("text", "objective", "user_input", "query"):
        value = goal.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return " ".join(str(value) for value in goal.values() if isinstance(value, str))


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text.lower())


def _as_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

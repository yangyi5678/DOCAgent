from __future__ import annotations

import json
import re
from typing import Any

from .registry import parse_frontmatter
from .selector import extract_goal_text
from .types import LLMClient, SkillContext, SkillSpec


class SkillLoader:
    """Compile SKILL.md into planner-facing SkillContext.

    Input:
        SkillSpec and Goal IR.

    Output:
        SkillContext with workflow hints, routing rules, grep hints, capability
        hints, recommended step kinds, and default resources.

    Logic:
        1. Read markdown and frontmatter.
        2. Extract structured hints with markdown/table/regex rules.
        3. Optionally ask an LLM to summarize or enrich non-contract fields.
        4. Keep manifest-declared capabilities and step kinds authoritative.

    Framework role:
        Converts human-oriented skill documentation into a context object that
        ContextManager can inject into planner context.
    """

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self.llm_client = llm_client

    def load(self, spec: SkillSpec, goal: dict[str, Any] | None = None) -> SkillContext:
        """Load and compile one skill.

        Input:
            spec: SkillSpec from SkillRegistry.
            goal: Optional Goal IR for goal-aware extraction and LLM summaries.

        Output:
            SkillContext. If SKILL.md is missing, returns manifest-only context.
        """
        markdown = read_skill_markdown(spec)
        frontmatter, body = parse_frontmatter(markdown)
        description = spec.description or str(frontmatter.get("description") or "")
        context = SkillContext(
            skill_name=spec.name,
            description=description,
            workflow_hints=extract_workflow_hints(body),
            term_mappings=extract_term_mappings(body),
            routing_rules=extract_routing_rules(body),
            grep_hints=extract_grep_hints(body),
            capability_hints=[
                *spec.capabilities.provides,
                *spec.capabilities.requires,
            ],
            recommended_step_kinds=list(spec.recommended_step_kinds),
            default_resources=dict(spec.default_resources),
            output_contract=extract_output_contract(body),
            source_path=str(spec.skill_path),
        )
        if self.llm_client:
            context.llm_notes = self._llm_enrich(spec, goal or {}, body, context)
            _merge_llm_notes(context, context.llm_notes)
        return context

    def _llm_enrich(
        self,
        spec: SkillSpec,
        goal: dict[str, Any],
        body: str,
        context: SkillContext,
    ) -> dict[str, Any]:
        payload = {
            "task": "summarize_skill_context",
            "goal_text": extract_goal_text(goal),
            "skill": spec.to_dict(),
            "rule_context": context.to_dict(),
            "skill_markdown_excerpt": body[:12000],
            "instructions": (
                "Return JSON with optional workflow_hints, term_mappings, "
                "routing_rules, grep_hints, output_contract, notes. Do not "
                "invent capabilities, tool names, paths, or step kinds."
            ),
        }
        try:
            raw = self.llm_client(payload)
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}


def read_skill_markdown(spec: SkillSpec) -> str:
    """Read SKILL.md.

    Input:
        SkillSpec.skill_path.

    Output:
        Markdown text, or an empty string when missing.
    """
    if not spec.skill_path.exists():
        return ""
    return spec.skill_path.read_text(encoding="utf-8", errors="ignore")


def extract_workflow_hints(markdown: str) -> list[str]:
    """Extract step/checklist style workflow hints from markdown."""
    hints: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.search(r"\bStep\s*\d+|步骤|执行步骤|检查清单|先|再|然后", stripped, re.I):
            hints.append(_clean_markdown(stripped))
        elif stripped.startswith("- [ ]"):
            hints.append(_clean_markdown(stripped[5:].strip()))
    return _dedupe(hints)[:80]


def extract_term_mappings(markdown: str) -> dict[str, str]:
    """Extract domain term mappings from natural-language skill docs."""
    mappings: dict[str, str] = {}
    patterns = [
        r"[\"“](?P<term>[^\"”]+)[\"”]\s*/\s*[\"“][^\"”]+[\"”]\s*=\s*(?P<value>[^，。；\n]+)",
        r"[\"“](?P<term>[^\"”]+)[\"”]\s*=\s*(?P<value>[^，。；\n]+)",
        r"`(?P<term>[^`]+)`\s*=\s*(?P<value>[^，。；\n]+)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, markdown):
            term = _clean_markdown(match.group("term")).strip()
            value = _clean_markdown(match.group("value")).strip()
            if term and value:
                mappings[term] = value
    return mappings


def extract_routing_rules(markdown: str) -> list[dict[str, Any]]:
    """Extract symptom -> module -> reference rules from markdown tables."""
    rules: list[dict[str, Any]] = []
    for table in _extract_markdown_tables(markdown):
        header = table[0]
        joined_header = " ".join(header)
        if not any(token in joined_header for token in ("症状", "日志特征", "reference", "模块")):
            continue
        for row in table[2:]:
            if len(row) < 2:
                continue
            references = re.findall(r"`([^`]+\.md)`", " ".join(row))
            rules.append(
                {
                    "condition": _clean_markdown(row[0]),
                    "module": _clean_markdown(row[1]) if len(row) > 1 else "",
                    "references": references,
                    "raw": [_clean_markdown(cell) for cell in row],
                }
            )
    return rules


def extract_grep_hints(markdown: str) -> list[str]:
    """Extract grep/search hints from markdown."""
    hints: list[str] = []
    for line in markdown.splitlines():
        if "grep" in line.lower():
            hints.append(_clean_markdown(line.strip()))
        hints.extend(re.findall(r"`([^`]*(?:state|mode|transition|result turns|EVT_|Is[A-Z][^`]*)[^`]*)`", line))
    return _dedupe([hint.strip() for hint in hints if hint.strip()])[:120]


def extract_output_contract(markdown: str) -> dict[str, Any]:
    """Extract expected report/output sections from skill markdown."""
    sections: list[str] = []
    capture = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if "报告输出" in stripped or "报告模板" in stripped:
            capture = True
            continue
        if capture and stripped.startswith("## "):
            break
        if capture and stripped.startswith("##"):
            sections.append(_clean_markdown(stripped))
        elif capture and stripped.startswith("- "):
            sections.append(_clean_markdown(stripped[2:]))
    return {"sections": _dedupe(sections[:30])}


def _extract_markdown_tables(markdown: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in markdown.splitlines():
        if line.strip().startswith("|") and line.strip().endswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            current.append(cells)
        else:
            if current:
                tables.append(current)
                current = []
    if current:
        tables.append(current)
    return tables


def _merge_llm_notes(context: SkillContext, notes: dict[str, Any]) -> None:
    """Merge only safe, non-contract LLM suggestions into context."""
    for key in ("workflow_hints", "grep_hints"):
        values = notes.get(key)
        if isinstance(values, list):
            merged = _dedupe([*getattr(context, key), *(str(item) for item in values)])
            setattr(context, key, merged[:120])
    mappings = notes.get("term_mappings")
    if isinstance(mappings, dict):
        context.term_mappings.update({str(k): str(v) for k, v in mappings.items()})
    rules = notes.get("routing_rules")
    if isinstance(rules, list):
        context.routing_rules.extend([rule for rule in rules if isinstance(rule, dict)])
    output_contract = notes.get("output_contract")
    if isinstance(output_contract, dict):
        context.output_contract.update(output_contract)


def _clean_markdown(text: str) -> str:
    return re.sub(r"[*_#>`]+", "", text).strip()


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result

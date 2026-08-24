from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .selector import extract_goal_text
from .types import LLMClient, ReferenceContext, SkillContext, SkillSpec


class ReferenceRouter:
    """Select relevant reference files and snippets for a goal.

    Input:
        Goal IR, SkillSpec, and SkillContext.

    Output:
        ReferenceContext with selected reference paths, snippets, and grep hints.

    Logic:
        1. Rule route by SkillContext.routing_rules.
        2. Keyword/BM25-like retrieval over markdown sections.
        3. Optional LLM rerank over candidate snippets.

    Framework role:
        Produces compact reference_context for planner/tools instead of injecting
        all reference documents into prompts.
    """

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self.llm_client = llm_client

    def route(
        self,
        goal: dict[str, Any],
        spec: SkillSpec,
        skill_context: SkillContext,
        *,
        max_snippets: int = 12,
    ) -> ReferenceContext:
        """Route references for one goal.

        Input:
            goal: Goal IR.
            spec: SkillSpec.
            skill_context: Loaded context.
            max_snippets: Snippet budget.

        Output:
            ReferenceContext for planner context assembly.
        """
        files = list_reference_files(spec)
        selected = select_references(goal, skill_context, files)
        if not selected:
            selected = files
        snippets = retrieve_reference_snippets(goal, selected, max_snippets=max_snippets * 2)
        if self.llm_client and snippets:
            snippets, notes = self._llm_rerank(goal, skill_context, snippets, max_snippets=max_snippets)
        else:
            snippets, notes = snippets[:max_snippets], {}
        return ReferenceContext(
            skill_name=spec.name,
            selected_references=[str(path) for path in selected],
            snippets=snippets[:max_snippets],
            grep_hints=extract_reference_grep_hints(snippets),
            llm_notes=notes,
        )

    def _llm_rerank(
        self,
        goal: dict[str, Any],
        skill_context: SkillContext,
        snippets: list[dict[str, Any]],
        *,
        max_snippets: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        payload = {
            "task": "rerank_reference_snippets",
            "goal_text": extract_goal_text(goal),
            "skill_context": skill_context.to_dict(),
            "candidate_snippets": snippets[:30],
            "instructions": (
                "Return JSON with snippet_ids=[...] and optional notes. "
                "Only use candidate snippet id values. Do not invent sources."
            ),
        }
        try:
            raw = self.llm_client(payload)
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return snippets[:max_snippets], {}
        id_to_snippet = {snippet["id"]: snippet for snippet in snippets}
        ranked = [
            id_to_snippet[item]
            for item in (parsed or {}).get("snippet_ids", [])
            if item in id_to_snippet
        ]
        if not ranked:
            return snippets[:max_snippets], {}
        return ranked[:max_snippets], {"llm_rerank": parsed}


def list_reference_files(spec: SkillSpec) -> list[Path]:
    """List markdown reference files under spec.reference_dir."""
    if not spec.reference_dir or not spec.reference_dir.exists():
        return []
    return sorted(path for path in spec.reference_dir.glob("*.md") if path.is_file())


def select_references(
    goal: dict[str, Any],
    skill_context: SkillContext,
    files: list[Path],
) -> list[Path]:
    """Select reference files by routing rules and goal keywords."""
    text = extract_goal_text(goal).lower()
    by_name = {path.name: path for path in files}
    selected: list[Path] = []
    for rule in skill_context.routing_rules:
        condition = " ".join(str(value) for value in rule.get("raw", [])).lower()
        if _has_overlap(text, condition):
            for ref in rule.get("references", []):
                if ref in by_name:
                    selected.append(by_name[ref])
    for path in files:
        stem = path.stem.lower()
        if stem in text or _has_overlap(text, stem.replace("_", " ")):
            selected.append(path)
    return _dedupe_paths(selected)


def split_markdown_sections(text: str) -> list[dict[str, Any]]:
    """Split markdown into heading-based sections."""
    sections: list[dict[str, Any]] = []
    current_heading = "<root>"
    current_lines: list[str] = []
    current_level = 0
    for line in text.splitlines():
        match = re.match(r"^(?P<marks>#{1,6})\s+(?P<title>.+)$", line)
        if match:
            if current_lines:
                sections.append(
                    {
                        "heading": current_heading,
                        "level": current_level,
                        "content": "\n".join(current_lines).strip(),
                    }
                )
            current_heading = match.group("title").strip()
            current_level = len(match.group("marks"))
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        sections.append({"heading": current_heading, "level": current_level, "content": "\n".join(current_lines).strip()})
    return sections


def retrieve_reference_snippets(
    goal: dict[str, Any],
    paths: list[Path],
    *,
    max_snippets: int,
) -> list[dict[str, Any]]:
    """Retrieve relevant markdown sections with simple lexical scoring."""
    query_tokens = _tokens(extract_goal_text(goal))
    snippets: list[dict[str, Any]] = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for index, section in enumerate(split_markdown_sections(text), start=1):
            content = section.get("content", "")
            score = _score_text(query_tokens, content)
            if score <= 0 and index > 1:
                continue
            snippets.append(
                {
                    "id": f"{path.name}#{index}",
                    "path": str(path),
                    "heading": section.get("heading"),
                    "score": score,
                    "content": content[:2400],
                }
            )
    snippets.sort(key=lambda item: item["score"], reverse=True)
    return snippets[:max_snippets]


def extract_reference_grep_hints(snippets: list[dict[str, Any]]) -> list[str]:
    """Extract grep/search commands and keywords from reference snippets."""
    hints: list[str] = []
    for snippet in snippets:
        content = str(snippet.get("content") or "")
        for line in content.splitlines():
            if "grep" in line.lower() or "rg " in line:
                hints.append(_clean(line))
        hints.extend(re.findall(r"`([^`]*(?:state|mode|transition|result turns|EVT_|Is[A-Z][^`]*)[^`]*)`", content))
    return _dedupe([hint for hint in hints if hint])[:120]


def _score_text(query_tokens: list[str], text: str) -> float:
    lowered = text.lower()
    score = 0.0
    for token in query_tokens:
        if token in lowered:
            score += 1.0
    if "grep" in lowered:
        score += 0.2
    return score


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text.lower())


def _has_overlap(text: str, candidate: str) -> bool:
    tokens = _tokens(candidate)
    return any(token in text for token in tokens)


def _clean(text: str) -> str:
    return re.sub(r"[*_#>`]+", "", text).strip()


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(path)
    return result

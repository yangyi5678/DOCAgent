from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .types import SkillCapabilities, SkillSpec, SkillTriggers


SKILL_MANIFEST_NAMES = ("skill.yaml", "skill.yml", "skill.json")


class SkillRegistry:
    """Scans and stores SkillSpec metadata.

    Input:
        One or more root paths. Each root may contain skill.yaml files or legacy
        SKILL.md files with frontmatter.

    Output:
        A name-indexed registry of SkillSpec objects.

    Core logic:
        scan() discovers manifests, loads specs, validates duplicate names, and
        builds lightweight keyword/capability indexes.

    Framework role:
        SkillRegistry is the knowledge-registry counterpart to tool_registry.
        It does not execute anything. ContextManager/SkillSelector use it before
        planner construction.
    """

    def __init__(self, roots: list[str | Path] | None = None) -> None:
        self.roots = [Path(root).expanduser().resolve() for root in roots or []]
        self._specs: dict[str, SkillSpec] = {}
        self._keyword_index: dict[str, list[str]] = {}
        self._capability_index: dict[str, list[str]] = {}

    def scan(self) -> None:
        """Discover and register all skill manifests under roots.

        Input:
            self.roots configured at construction time.

        Output:
            Mutates the registry indexes. Raises ValueError on duplicate skill
            names, because planner context must be deterministic.
        """
        self._specs.clear()
        for manifest in discover_skill_manifests(self.roots):
            self.register(load_skill_spec(manifest))
        self._rebuild_indexes()

    def register(self, spec: SkillSpec) -> None:
        """Register a SkillSpec explicitly.

        Input:
            A fully resolved SkillSpec.

        Output:
            Adds it to the registry; duplicate names are rejected.
        """
        if spec.name in self._specs:
            raise ValueError(f"重复 skill name: {spec.name}")
        self._specs[spec.name] = spec
        self._rebuild_indexes()

    def get(self, name: str) -> SkillSpec:
        """Return one registered skill by name."""
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(f"未找到 skill spec: {name}") from exc

    def iter_specs(self) -> list[SkillSpec]:
        """Return all registered specs in stable name order."""
        return [self._specs[name] for name in sorted(self._specs)]

    def find_by_keyword(self, text: str) -> list[SkillSpec]:
        """Recall skills whose trigger keyword appears in text."""
        matched_names: list[str] = []
        lowered = text.lower()
        for keyword, names in self._keyword_index.items():
            if keyword.lower() in lowered:
                matched_names.extend(names)
        return [self._specs[name] for name in _dedupe(matched_names)]

    def find_by_capability(self, capability: str) -> list[SkillSpec]:
        """Return skills that declare a given provided/required capability."""
        return [self._specs[name] for name in self._capability_index.get(capability, [])]

    def validate(
        self,
        *,
        supported_capabilities: set[str] | None = None,
        supported_step_kinds: set[str] | None = None,
    ) -> list[str]:
        """Validate all registered specs against framework contracts.

        Input:
            Optional capability and step-kind allowlists from tool_registry and
            planner.STEP_KIND_DEFINITIONS.

        Output:
            List of human-readable validation issues. Empty means OK.
        """
        from .validators import validate_skill_spec

        issues: list[str] = []
        for spec in self.iter_specs():
            issues.extend(
                validate_skill_spec(
                    spec,
                    supported_capabilities=supported_capabilities,
                    supported_step_kinds=supported_step_kinds,
                )
            )
        return issues

    def _rebuild_indexes(self) -> None:
        self._keyword_index = {}
        self._capability_index = {}
        for spec in self._specs.values():
            for keyword in spec.triggers.keywords:
                self._keyword_index.setdefault(keyword, []).append(spec.name)
            for capability in [*spec.capabilities.provides, *spec.capabilities.requires]:
                self._capability_index.setdefault(capability, []).append(spec.name)


def discover_skill_manifests(roots: list[Path]) -> list[Path]:
    """Find skill manifests below roots.

    Input:
        Directory roots to scan.

    Output:
        Paths to skill.yaml/skill.yml/skill.json. If a directory has SKILL.md but
        no manifest, the SKILL.md path is returned as a legacy manifest source.

    Logic:
        Prefer explicit manifests. Legacy SKILL.md support keeps current local
        skills discoverable while long-term skills migrate to skill.yaml.
    """
    manifests: list[Path] = []
    for root in roots:
        if root.is_file() and root.name in (*SKILL_MANIFEST_NAMES, "SKILL.md"):
            manifests.append(root)
            continue
        if not root.exists():
            continue
        explicit = []
        for name in SKILL_MANIFEST_NAMES:
            explicit.extend(root.rglob(name))
        manifests.extend(explicit)
        explicit_dirs = {path.parent.resolve() for path in explicit}
        for skill_md in root.rglob("SKILL.md"):
            if skill_md.parent.resolve() not in explicit_dirs:
                manifests.append(skill_md)
    return sorted(set(manifests))


def load_skill_spec(path: str | Path) -> SkillSpec:
    """Load one SkillSpec from skill.yaml, skill.json, or SKILL.md frontmatter."""
    source = Path(path).expanduser().resolve()
    if source.name == "SKILL.md":
        data = _load_skill_md_frontmatter(source)
        root_dir = source.parent
    else:
        data = _load_manifest_mapping(source)
        root_dir = source.parent

    name = str(data.get("name") or root_dir.name)
    description = str(data.get("description") or "")
    skill_path = _resolve_relative(root_dir, data.get("skill_path") or "SKILL.md")
    reference_dir_raw = data.get("reference_dir") or (data.get("references") or {}).get("root") or "references"
    reference_dir = _resolve_relative(root_dir, reference_dir_raw)

    triggers_data = data.get("triggers") or {}
    if not triggers_data and data.get("keywords"):
        triggers_data = {"keywords": data.get("keywords")}
    capabilities_data = data.get("capabilities") or {}

    return SkillSpec(
        name=name,
        description=description,
        root_dir=root_dir,
        skill_path=skill_path,
        reference_dir=reference_dir if reference_dir.exists() or reference_dir_raw else None,
        triggers=SkillTriggers(
            keywords=_as_str_list(triggers_data.get("keywords")),
            intents=_as_str_list(triggers_data.get("intents")),
            domains=_as_str_list(triggers_data.get("domains")),
        ),
        capabilities=SkillCapabilities(
            provides=_as_str_list(capabilities_data.get("provides")),
            requires=_as_str_list(capabilities_data.get("requires")),
        ),
        recommended_step_kinds=_as_str_list(data.get("recommended_step_kinds")),
        default_resources=dict(data.get("default_resources") or {}),
        metadata={
            key: value
            for key, value in data.items()
            if key
            not in {
                "name",
                "description",
                "skill_path",
                "reference_dir",
                "references",
                "triggers",
                "keywords",
                "capabilities",
                "recommended_step_kinds",
                "default_resources",
            }
        },
        manifest_path=source,
    )


def _load_manifest_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return dict(json.loads(text))
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(text) or {}
        if isinstance(loaded, dict):
            return loaded
    except Exception:
        pass
    return _parse_tiny_yaml(text)


def _load_skill_md_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    frontmatter, _ = parse_frontmatter(text)
    if frontmatter:
        data = dict(frontmatter)
    else:
        data = {"name": path.parent.name}
    data.setdefault("skill_path", path.name)
    data.setdefault("reference_dir", "references")
    data.setdefault("metadata", {})
    if not data.get("triggers"):
        keywords = _extract_legacy_keywords(text, str(data.get("description") or ""))
        if keywords:
            data["triggers"] = {"keywords": keywords}
    return data


def parse_frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    """Parse YAML-like markdown frontmatter without requiring PyYAML."""
    if not markdown.startswith("---"):
        return {}, markdown
    match = re.match(r"\A---\s*\n(?P<body>.*?)\n---\s*\n?(?P<rest>.*)\Z", markdown, re.S)
    if not match:
        return {}, markdown
    return _parse_tiny_yaml(match.group("body")), match.group("rest")


def _parse_tiny_yaml(text: str) -> dict[str, Any]:
    """Parse the subset of YAML used by skill manifests.

    Supports:
        top-level scalars, one-level nested mappings, and simple lists.

    This fallback is intentionally small. If PyYAML is installed, the registry
    uses it first.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any, str | None]] = [(-1, root, None)]
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if line.startswith("- "):
            value = _parse_scalar(line[2:].strip())
            if isinstance(parent, list):
                parent.append(value)
            continue
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value:
            value = _parse_scalar(raw_value)
            if isinstance(parent, dict):
                parent[key] = value
            continue
        container: dict[str, Any] | list[Any]
        next_is_list = _next_content_line_starts_list(text.splitlines(), raw_line)
        container = [] if next_is_list else {}
        if isinstance(parent, dict):
            parent[key] = container
            stack.append((indent, container, key))
    return root


def _next_content_line_starts_list(lines: list[str], current_line: str) -> bool:
    try:
        start = lines.index(current_line) + 1
    except ValueError:
        return False
    current_indent = len(current_line) - len(current_line.lstrip(" "))
    for line in lines[start:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        return indent > current_indent and line.strip().startswith("- ")
    return False


def _parse_scalar(value: str) -> Any:
    value = value.strip().strip("'\"")
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = json.loads(value.replace("'", '"'))
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
    return value


def _resolve_relative(root: Path, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _extract_legacy_keywords(markdown: str, description: str) -> list[str]:
    """Extract trigger keywords from legacy SKILL.md prose.

    Long-term skills should use skill.yaml triggers. This helper keeps current
    frontmatter-only skills selectable while preserving manifest-first design.
    """
    text = "\n".join([description, markdown[:4000]])
    matches = re.findall(r"关键词[：:]\s*([^。\n]+)", text)
    values: list[str] = []
    for match in matches:
        values.extend(re.split(r"[/,，、\s]+", match))
    for match in re.finditer(r"[\"“](?P<term>[^\"”]+)[\"”]\s*(?:/\s*[\"“][^\"”]+[\"”]\s*)?=", markdown[:8000]):
        term = match.group("term").strip()
        values.extend(re.split(r"[/,，、\s]+", term))
    return _dedupe([value.strip("。；; ") for value in values if value.strip()])


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result

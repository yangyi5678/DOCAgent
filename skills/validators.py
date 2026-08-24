from __future__ import annotations

from .types import SkillSpec


def validate_skill_spec(
    spec: SkillSpec,
    *,
    supported_capabilities: set[str] | None = None,
    supported_step_kinds: set[str] | None = None,
) -> list[str]:
    """Validate one SkillSpec.

    Input:
        spec plus optional framework allowlists.

    Output:
        Human-readable issue list.

    Logic:
        Delegates to focused validators for fields, paths, capabilities, step
        kinds, and references.

    Framework role:
        Registry can call this after scan; CI can call it to ensure skills and
        tools remain aligned.
    """
    issues: list[str] = []
    issues.extend(validate_skill_fields(spec))
    issues.extend(validate_skill_paths(spec))
    issues.extend(validate_skill_references(spec))
    if supported_capabilities is not None:
        issues.extend(validate_skill_capabilities(spec, supported_capabilities))
    if supported_step_kinds is not None:
        issues.extend(validate_skill_step_kinds(spec, supported_step_kinds))
    return issues


def validate_skill_fields(spec: SkillSpec) -> list[str]:
    """Validate required identity fields."""
    issues: list[str] = []
    if not spec.name.strip():
        issues.append("skill name 不能为空")
    if not spec.description.strip():
        issues.append(f"{spec.name}: description 为空")
    return issues


def validate_skill_paths(spec: SkillSpec) -> list[str]:
    """Validate root and SKILL.md paths."""
    issues: list[str] = []
    if not spec.root_dir.exists():
        issues.append(f"{spec.name}: root_dir 不存在: {spec.root_dir}")
    if not spec.skill_path.exists():
        issues.append(f"{spec.name}: skill_path 不存在: {spec.skill_path}")
    return issues


def validate_skill_references(spec: SkillSpec) -> list[str]:
    """Validate reference directory and declared reference files."""
    issues: list[str] = []
    if spec.reference_dir and not spec.reference_dir.exists():
        issues.append(f"{spec.name}: reference_dir 不存在: {spec.reference_dir}")
    references = spec.metadata.get("references")
    files: list[str] = []
    if isinstance(references, dict):
        raw_files = references.get("files")
        if isinstance(raw_files, list):
            files = [str(item) for item in raw_files]
    if spec.reference_dir:
        for file_name in files:
            if not (spec.reference_dir / file_name).exists():
                issues.append(f"{spec.name}: reference 文件不存在: {file_name}")
    return issues


def validate_skill_capabilities(spec: SkillSpec, supported_capabilities: set[str]) -> list[str]:
    """Validate skill-declared capabilities against tool registry capability set."""
    issues: list[str] = []
    for capability in [*spec.capabilities.provides, *spec.capabilities.requires]:
        if capability not in supported_capabilities:
            issues.append(f"{spec.name}: capability 未被工具支持: {capability}")
    return issues


def validate_skill_step_kinds(spec: SkillSpec, supported_step_kinds: set[str]) -> list[str]:
    """Validate recommended step kinds against planner STEP_KIND_DEFINITIONS."""
    issues: list[str] = []
    for kind in spec.recommended_step_kinds:
        if kind not in supported_step_kinds:
            issues.append(f"{spec.name}: recommended_step_kind 未定义: {kind}")
    return issues

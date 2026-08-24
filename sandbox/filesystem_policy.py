"""Filesystem sandbox policy defaults."""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_READ_ROOTS = [PROJECT_ROOT]
DEFAULT_WRITE_ROOTS = [
    PROJECT_ROOT / "outputs",
    PROJECT_ROOT / "logs",
]
DEFAULT_ARTIFACT_ROOTS = [
    PROJECT_ROOT / "outputs" / "artifacts",
]
DEFAULT_DELETE_ROOTS = [
    PROJECT_ROOT / "outputs" / "tmp",
    PROJECT_ROOT / "logs" / "tmp",
]
DEFAULT_BLOCKED_READ_PATTERNS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_rsa.pub",
]


def _path_list(paths: list[Path]) -> list[str]:
    return [str(path) for path in paths]


DEFAULT_FILESYSTEM_POLICY = {
    "allow_read": True,
    "allow_write": False,
    "allow_delete": False,
    "read_roots": _path_list(DEFAULT_READ_ROOTS),
    "write_roots": _path_list(DEFAULT_WRITE_ROOTS),
    "artifact_roots": _path_list(DEFAULT_ARTIFACT_ROOTS),
    "delete_roots": _path_list(DEFAULT_DELETE_ROOTS),
    "blocked_read_patterns": DEFAULT_BLOCKED_READ_PATTERNS,
    "allowed_read_paths": _path_list(DEFAULT_READ_ROOTS),
    "allowed_write_paths": _path_list(DEFAULT_WRITE_ROOTS),
    "max_file_size": 10 * 1024 * 1024,
}


TOOL_FILESYSTEM_POLICIES: dict[str, dict[str, Any]] = {
    "ensure_directory": {
        "allow_read": True,
        "allow_write": True,
    },
    "ensure_directory_tool": {
        "allow_read": True,
        "allow_write": True,
    },
    "ensure_directory_core": {
        "allow_read": True,
        "allow_write": True,
    },
    "read_text_file": {
        "allow_read": True,
        "allow_write": False,
    },
    "read_text_file_tool": {
        "allow_read": True,
        "allow_write": False,
    },
    "read_text_file_core": {
        "allow_read": True,
        "allow_write": False,
    },
    "read_file": {
        "allow_read": True,
        "allow_write": False,
    },
    "write_text_file": {
        "allow_read": True,
        "allow_write": True,
    },
    "write_text_file_tool": {
        "allow_read": True,
        "allow_write": True,
    },
    "write_text_file_core": {
        "allow_read": True,
        "allow_write": True,
    },
    "append_text_file": {
        "allow_read": True,
        "allow_write": True,
    },
    "append_text_file_tool": {
        "allow_read": True,
        "allow_write": True,
    },
    "append_text_file_core": {
        "allow_read": True,
        "allow_write": True,
    },
    "write_file": {
        "allow_read": True,
        "allow_write": True,
    },
    "replace_in_file": {
        "allow_read": True,
        "allow_write": True,
    },
    "replace_in_file_tool": {
        "allow_read": True,
        "allow_write": True,
    },
    "replace_in_file_core": {
        "allow_read": True,
        "allow_write": True,
    },
    "delete_file": {
        "allow_read": True,
        "allow_write": False,
        "allow_delete": True,
    },
    "delete_file_tool": {
        "allow_read": True,
        "allow_write": False,
        "allow_delete": True,
    },
    "delete_file_core": {
        "allow_read": True,
        "allow_write": False,
        "allow_delete": True,
    },
    "list_directory": {
        "allow_read": True,
        "allow_write": False,
    },
    "list_directory_tool": {
        "allow_read": True,
        "allow_write": False,
    },
    "list_directory_core": {
        "allow_read": True,
        "allow_write": False,
    },
}


def _resolve_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _is_under_root(path: Path, root: str | Path) -> bool:
    try:
        path.relative_to(_resolve_path(root))
        return True
    except ValueError:
        return False


def _matches_blocked_read_pattern(path: Path, patterns: list[str]) -> bool:
    name = path.name
    text = str(path)
    return any(fnmatch(name, pattern) or fnmatch(text, pattern) for pattern in patterns)


def is_read_allowed(path: str | Path, policy: dict[str, Any] | None = None) -> bool:
    """Return whether one path can be read under the filesystem policy."""
    policy = policy or DEFAULT_FILESYSTEM_POLICY
    if not policy.get("allow_read", False):
        return False

    target = _resolve_path(path)
    patterns = policy.get("blocked_read_patterns") or []
    if _matches_blocked_read_pattern(target, patterns):
        return False

    return any(_is_under_root(target, root) for root in policy.get("read_roots") or [])


def is_write_allowed(path: str | Path, policy: dict[str, Any] | None = None) -> bool:
    """Return whether one path can be written under the filesystem policy."""
    policy = policy or DEFAULT_FILESYSTEM_POLICY
    if not policy.get("allow_write", False):
        return False

    target = _resolve_path(path)
    return any(_is_under_root(target, root) for root in policy.get("write_roots") or [])


def is_artifact_allowed(path: str | Path, policy: dict[str, Any] | None = None) -> bool:
    """Return whether one generated artifact can be saved at the path."""
    policy = policy or DEFAULT_FILESYSTEM_POLICY
    target = _resolve_path(path)
    return any(_is_under_root(target, root) for root in policy.get("artifact_roots") or [])


def is_delete_allowed(path: str | Path, policy: dict[str, Any] | None = None) -> bool:
    """Return whether one path can be deleted under the filesystem policy."""
    policy = policy or DEFAULT_FILESYSTEM_POLICY
    if not policy.get("allow_delete", False):
        return False

    target = _resolve_path(path)
    return any(_is_under_root(target, root) for root in policy.get("delete_roots") or [])


def build_filesystem_policy(
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build filesystem policy for one executable node."""
    return {
        **DEFAULT_FILESYSTEM_POLICY,
        **TOOL_FILESYSTEM_POLICIES.get(tool_name or "", {}),
    }


def validate_filesystem_access_for_node(node: dict[str, Any]) -> None:
    """Validate filesystem access for one executable DAG node before dispatch."""
    tool_name = node.get("tool")
    tool_input = node.get("input") or {}
    if not isinstance(tool_input, dict):
        raise TypeError("节点 input 必须是 dict。")

    sandbox = node.get("sandbox") or {}
    policy = sandbox.get("filesystem") or build_filesystem_policy(tool_name, tool_input)
    path = tool_input.get("path")
    if path is None:
        return

    if tool_name in {"read_text_file", "read_text_file_tool", "read_text_file_core"}:
        ensure_read_allowed(path, policy)
    elif tool_name in {"list_directory", "list_directory_tool", "list_directory_core"}:
        ensure_read_allowed(path, policy)
    elif tool_name in {
        "ensure_directory",
        "ensure_directory_tool",
        "ensure_directory_core",
        "write_text_file",
        "write_text_file_tool",
        "write_text_file_core",
        "append_text_file",
        "append_text_file_tool",
        "append_text_file_core",
        "replace_in_file",
        "replace_in_file_tool",
        "replace_in_file_core",
    }:
        ensure_write_allowed(path, policy)
    elif tool_name in {"delete_file", "delete_file_tool", "delete_file_core"}:
        ensure_delete_allowed(path, policy)


def ensure_read_allowed(path: str | Path, policy: dict[str, Any] | None = None) -> None:
    """Raise PermissionError if the path cannot be read."""
    if not is_read_allowed(path, policy):
        raise PermissionError(f"路径不允许读取: {_resolve_path(path)}")


def ensure_write_allowed(path: str | Path, policy: dict[str, Any] | None = None) -> None:
    """Raise PermissionError if the path cannot be written."""
    if not is_write_allowed(path, policy):
        raise PermissionError(f"路径不允许写入: {_resolve_path(path)}")


def ensure_artifact_allowed(path: str | Path, policy: dict[str, Any] | None = None) -> None:
    """Raise PermissionError if the artifact cannot be saved at the path."""
    if not is_artifact_allowed(path, policy):
        raise PermissionError(f"产物路径不允许写入: {_resolve_path(path)}")


def ensure_delete_allowed(path: str | Path, policy: dict[str, Any] | None = None) -> None:
    """Raise PermissionError if the path cannot be deleted."""
    if not is_delete_allowed(path, policy):
        raise PermissionError(f"路径不允许删除: {_resolve_path(path)}")

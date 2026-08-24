"""Database sandbox policy defaults and validators.

数据库策略专门管理数据库路径和数据库操作权限。它和 filesystem_policy 分开：
filesystem_policy 管普通文件读写删；database_policy 管 db_path、SQL 操作和查询规模。
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQLITE_READ_ROOTS = [
    PROJECT_ROOT / "data",
    PROJECT_ROOT / "outputs",
]
DEFAULT_SQLITE_WRITE_ROOTS = [
    PROJECT_ROOT / "data",
    PROJECT_ROOT / "outputs",
]
DEFAULT_SQLITE_ALLOWED_PATTERNS = [
    "*.db",
    "*.sqlite",
    "*.sqlite3",
]

SQLITE_TOOL_OPERATIONS = {
    "ensure_sqlite_db": "ensure",
    "ensure_sqlite_db_tool": "ensure",
    "ensure_sqlite_db_core": "ensure",
    "create_table": "create_table",
    "create_table_tool": "create_table",
    "create_table_core": "create_table",
    "insert_row": "insert",
    "insert_row_tool": "insert",
    "insert_row_core": "insert",
    "select_rows": "select",
    "select_rows_tool": "select",
    "select_rows_core": "select",
    "update_rows": "update",
    "update_rows_tool": "update",
    "update_rows_core": "update",
    "delete_rows": "delete",
    "delete_rows_tool": "delete",
    "delete_rows_core": "delete",
}


def _path_list(paths: list[Path]) -> list[str]:
    return [str(path) for path in paths]


DEFAULT_DATABASE_POLICY = {
    "allow_database": False,
    "sqlite_read_roots": _path_list(DEFAULT_SQLITE_READ_ROOTS),
    "sqlite_write_roots": _path_list(DEFAULT_SQLITE_WRITE_ROOTS),
    "sqlite_allowed_patterns": DEFAULT_SQLITE_ALLOWED_PATTERNS,
    "allow_ensure": False,
    "allow_select": False,
    "allow_create_table": False,
    "allow_insert": False,
    "allow_update": False,
    "allow_delete": False,
    "allow_raw_sql": False,
    "allow_drop": False,
    "allow_alter": False,
    "allowed_tables": [],
    "blocked_tables": [],
    "require_limit": False,
    "max_rows": 1000,
}


TOOL_DATABASE_POLICIES: dict[str, dict[str, Any]] = {
    "ensure_sqlite_db": {
        "allow_database": True,
        "allow_ensure": True,
    },
    "ensure_sqlite_db_tool": {
        "allow_database": True,
        "allow_ensure": True,
    },
    "ensure_sqlite_db_core": {
        "allow_database": True,
        "allow_ensure": True,
    },
    "create_table": {
        "allow_database": True,
        "allow_create_table": True,
    },
    "create_table_tool": {
        "allow_database": True,
        "allow_create_table": True,
    },
    "create_table_core": {
        "allow_database": True,
        "allow_create_table": True,
    },
    "insert_row": {
        "allow_database": True,
        "allow_insert": True,
    },
    "insert_row_tool": {
        "allow_database": True,
        "allow_insert": True,
    },
    "insert_row_core": {
        "allow_database": True,
        "allow_insert": True,
    },
    "select_rows": {
        "allow_database": True,
        "allow_select": True,
    },
    "select_rows_tool": {
        "allow_database": True,
        "allow_select": True,
    },
    "select_rows_core": {
        "allow_database": True,
        "allow_select": True,
    },
    "update_rows": {
        "allow_database": True,
        "allow_update": True,
    },
    "update_rows_tool": {
        "allow_database": True,
        "allow_update": True,
    },
    "update_rows_core": {
        "allow_database": True,
        "allow_update": True,
    },
    "delete_rows": {
        "allow_database": True,
        "allow_delete": True,
    },
    "delete_rows_tool": {
        "allow_database": True,
        "allow_delete": True,
    },
    "delete_rows_core": {
        "allow_database": True,
        "allow_delete": True,
    },
}


def build_database_policy(
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build database policy for one executable node."""
    return {
        **DEFAULT_DATABASE_POLICY,
        **TOOL_DATABASE_POLICIES.get(tool_name or "", {}),
    }


def ensure_database_tool_allowed(
    tool_name: str,
    tool_input: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> None:
    """Raise PermissionError/ValueError if one database tool call is not allowed."""
    tool_input = tool_input or {}
    policy = policy or build_database_policy(tool_name, tool_input)
    operation = SQLITE_TOOL_OPERATIONS.get(tool_name)
    if operation is None:
        return

    if not policy.get("allow_database", False):
        raise PermissionError(f"数据库工具不允许执行: {tool_name}")

    if not policy.get(f"allow_{operation}", False):
        raise PermissionError(f"数据库操作不允许执行: {operation}")

    db_path = tool_input.get("db_path")
    if db_path is not None:
        ensure_sqlite_path_allowed(db_path, operation=operation, policy=policy)

    table = tool_input.get("table")
    if table is not None:
        ensure_table_allowed(str(table), policy)

    if operation == "select":
        _ensure_select_limit_allowed(tool_input, policy)


def validate_database_access_for_node(node: dict[str, Any]) -> None:
    """Validate database access for one executable DAG node before dispatch."""
    tool_name = node.get("tool")
    if tool_name not in SQLITE_TOOL_OPERATIONS:
        return

    tool_input = node.get("input") or {}
    if not isinstance(tool_input, dict):
        raise TypeError("节点 input 必须是 dict。")

    sandbox = node.get("sandbox") or {}
    policy = sandbox.get("database") or build_database_policy(tool_name, tool_input)
    ensure_database_tool_allowed(str(tool_name), tool_input, policy)


def ensure_sqlite_path_allowed(
    db_path: str | Path,
    *,
    operation: str,
    policy: dict[str, Any] | None = None,
) -> Path:
    """Resolve and validate one SQLite database path."""
    policy = policy or DEFAULT_DATABASE_POLICY
    target = Path(db_path).expanduser().resolve()

    if not _matches_allowed_pattern(target, policy.get("sqlite_allowed_patterns") or []):
        raise PermissionError(f"数据库文件后缀不允许: {target}")

    roots_key = "sqlite_read_roots" if operation == "select" else "sqlite_write_roots"
    if not any(_is_under_root(target, root) for root in policy.get(roots_key) or []):
        roots_text = ", ".join(policy.get(roots_key) or [])
        raise PermissionError(f"数据库路径不在允许范围内: {target}。允许目录: {roots_text}")

    return target


def ensure_table_allowed(table: str, policy: dict[str, Any] | None = None) -> str:
    """Validate one database table name against allow/block table policy."""
    policy = policy or DEFAULT_DATABASE_POLICY
    table_name = table.strip()
    if not table_name:
        raise ValueError("table 不能为空。")

    allowed_tables = set(policy.get("allowed_tables") or [])
    blocked_tables = set(policy.get("blocked_tables") or [])

    if allowed_tables and table_name not in allowed_tables:
        raise PermissionError(f"数据库表不在允许列表内: {table_name}")
    if table_name in blocked_tables:
        raise PermissionError(f"数据库表在禁止列表内: {table_name}")

    return table_name


def _ensure_select_limit_allowed(tool_input: dict[str, Any], policy: dict[str, Any]) -> None:
    if policy.get("require_limit", True) and tool_input.get("limit") is None:
        raise ValueError("select_rows 必须提供 limit。")

    limit = int(tool_input.get("limit", policy.get("max_rows", 1000)))
    max_rows = int(policy.get("max_rows", 1000))
    if limit > max_rows:
        raise ValueError(f"select_rows limit={limit} 超过最大允许行数 {max_rows}。")


def _matches_allowed_pattern(path: Path, patterns: list[str]) -> bool:
    name = path.name
    return any(fnmatch(name, pattern) for pattern in patterns)


def _is_under_root(path: Path, root: str | Path) -> bool:
    try:
        path.relative_to(Path(root).expanduser().resolve())
        return True
    except ValueError:
        return False

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from sandbox.database_policy import ensure_database_tool_allowed, ensure_sqlite_path_allowed

try:
    from langchain.tools import tool
except ImportError:  # pragma: no cover
    tool = None


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_DIR = ROOT_DIR / "data"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "app.db"


def _resolve_db_path(db_path: str | Path = DEFAULT_DB_PATH) -> Path:
    return ensure_sqlite_path_allowed(db_path, operation="select")


def ensure_sqlite_db_core(
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    tool_input = {"db_path": db_path}
    ensure_database_tool_allowed("ensure_sqlite_db", tool_input)
    target = ensure_sqlite_path_allowed(db_path, operation="ensure")
    target.parent.mkdir(parents=True, exist_ok=True)

    created = not target.exists()
    with sqlite3.connect(target):
        pass

    return {
        "status": "success",
        "action": "ensure_sqlite_db",
        "db_path": str(target),
        "created": created,
    }


def _connect(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    operation: str = "select",
) -> tuple[sqlite3.Connection, Path]:
    target = ensure_sqlite_path_allowed(db_path, operation=operation)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    return conn, target


def _validate_identifier(name: str, *, label: str) -> str:
    clean = name.strip()
    if not clean:
        raise ValueError(f"{label} 不能为空。")

    if not all(ch.isalnum() or ch == "_" for ch in clean):
        raise ValueError(f"{label} 仅允许字母、数字和下划线: {clean}")
    return clean


def create_table_core(
    table: str,
    schema_sql: str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    ensure_database_tool_allowed(
        "create_table",
        {"table": table, "schema_sql": schema_sql, "db_path": db_path},
    )
    table_name = _validate_identifier(table, label="table")
    schema = schema_sql.strip()
    if not schema:
        raise ValueError("schema_sql 不能为空。")

    conn, target = _connect(db_path, operation="create_table")
    try:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({schema})")
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "success",
        "action": "create_table",
        "db_path": str(target),
        "table": table_name,
    }


def insert_row_core(
    table: str,
    data: dict[str, Any],
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    ensure_database_tool_allowed(
        "insert_row",
        {"table": table, "data": data, "db_path": db_path},
    )
    table_name = _validate_identifier(table, label="table")
    if not data:
        raise ValueError("data 不能为空。")

    columns = [_validate_identifier(key, label="column") for key in data.keys()]
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    values = [data[key] for key in data.keys()]

    conn, target = _connect(db_path, operation="insert")
    try:
        cursor = conn.execute(
            f"INSERT INTO {table_name} ({column_sql}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "success",
        "action": "insert_row",
        "db_path": str(target),
        "table": table_name,
        "rows_affected": 1,
        "last_row_id": cursor.lastrowid,
    }


def _build_filter_clause(filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
    if not filters:
        return "", []

    clauses: list[str] = []
    values: list[Any] = []
    for key, value in filters.items():
        column = _validate_identifier(key, label="filter column")
        clauses.append(f"{column} = ?")
        values.append(value)

    return " WHERE " + " AND ".join(clauses), values


def select_rows_core(
    table: str,
    filters: dict[str, Any] | None = None,
    limit: int = 100,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    ensure_database_tool_allowed(
        "select_rows",
        {"table": table, "filters": filters, "limit": limit, "db_path": db_path},
    )
    table_name = _validate_identifier(table, label="table")
    safe_limit = max(1, int(limit))
    where_sql, values = _build_filter_clause(filters)

    conn, target = _connect(db_path, operation="select")
    try:
        cursor = conn.execute(
            f"SELECT * FROM {table_name}{where_sql} LIMIT ?",
            [*values, safe_limit],
        )
        rows = [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

    return {
        "status": "success",
        "action": "select_rows",
        "db_path": str(target),
        "table": table_name,
        "count": len(rows),
        "rows": rows,
    }


def update_rows_core(
    table: str,
    filters: dict[str, Any],
    data: dict[str, Any],
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    ensure_database_tool_allowed(
        "update_rows",
        {"table": table, "filters": filters, "data": data, "db_path": db_path},
    )
    table_name = _validate_identifier(table, label="table")
    if not filters:
        raise ValueError("filters 不能为空。")
    if not data:
        raise ValueError("data 不能为空。")

    set_columns = [_validate_identifier(key, label="update column") for key in data.keys()]
    set_sql = ", ".join(f"{column} = ?" for column in set_columns)
    set_values = [data[key] for key in data.keys()]
    where_sql, filter_values = _build_filter_clause(filters)

    conn, target = _connect(db_path, operation="update")
    try:
        cursor = conn.execute(
            f"UPDATE {table_name} SET {set_sql}{where_sql}",
            [*set_values, *filter_values],
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "success",
        "action": "update_rows",
        "db_path": str(target),
        "table": table_name,
        "rows_affected": cursor.rowcount,
    }


def delete_rows_core(
    table: str,
    filters: dict[str, Any],
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    ensure_database_tool_allowed(
        "delete_rows",
        {"table": table, "filters": filters, "db_path": db_path},
    )
    table_name = _validate_identifier(table, label="table")
    if not filters:
        raise ValueError("filters 不能为空。")

    where_sql, filter_values = _build_filter_clause(filters)

    conn, target = _connect(db_path, operation="delete")
    try:
        cursor = conn.execute(
            f"DELETE FROM {table_name}{where_sql}",
            filter_values,
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "success",
        "action": "delete_rows",
        "db_path": str(target),
        "table": table_name,
        "rows_affected": cursor.rowcount,
    }


def _to_tool_result(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)


def ensure_sqlite_db_tool_payload(db_path: str = str(DEFAULT_DB_PATH)) -> str:
    return _to_tool_result(ensure_sqlite_db_core(db_path=db_path))


def create_table_tool_payload(
    table: str,
    schema_sql: str,
    db_path: str = str(DEFAULT_DB_PATH),
) -> str:
    return _to_tool_result(create_table_core(table=table, schema_sql=schema_sql, db_path=db_path))


def insert_row_tool_payload(
    table: str,
    data: dict[str, Any],
    db_path: str = str(DEFAULT_DB_PATH),
) -> str:
    return _to_tool_result(insert_row_core(table=table, data=data, db_path=db_path))


def select_rows_tool_payload(
    table: str,
    filters: dict[str, Any] | None = None,
    limit: int = 100,
    db_path: str = str(DEFAULT_DB_PATH),
) -> str:
    return _to_tool_result(
        select_rows_core(table=table, filters=filters, limit=limit, db_path=db_path)
    )


def update_rows_tool_payload(
    table: str,
    filters: dict[str, Any],
    data: dict[str, Any],
    db_path: str = str(DEFAULT_DB_PATH),
) -> str:
    return _to_tool_result(
        update_rows_core(table=table, filters=filters, data=data, db_path=db_path)
    )


def delete_rows_tool_payload(
    table: str,
    filters: dict[str, Any],
    db_path: str = str(DEFAULT_DB_PATH),
) -> str:
    return _to_tool_result(delete_rows_core(table=table, filters=filters, db_path=db_path))


if tool is not None:

    @tool
    def ensure_sqlite_db_tool(db_path: str = str(DEFAULT_DB_PATH)) -> str:
        """确保 SQLite 数据库文件存在。"""
        return ensure_sqlite_db_tool_payload(db_path=db_path)

    @tool
    def create_table_tool(
        table: str,
        schema_sql: str,
        db_path: str = str(DEFAULT_DB_PATH),
    ) -> str:
        """在 SQLite 数据库中创建数据表。"""
        return create_table_tool_payload(table=table, schema_sql=schema_sql, db_path=db_path)

    @tool
    def insert_row_tool(
        table: str,
        data: dict[str, Any],
        db_path: str = str(DEFAULT_DB_PATH),
    ) -> str:
        """向 SQLite 数据表插入一行记录。"""
        return insert_row_tool_payload(table=table, data=data, db_path=db_path)

    @tool
    def select_rows_tool(
        table: str,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        db_path: str = str(DEFAULT_DB_PATH),
    ) -> str:
        """从 SQLite 数据表查询记录。"""
        return select_rows_tool_payload(
            table=table,
            filters=filters,
            limit=limit,
            db_path=db_path,
        )

    @tool
    def update_rows_tool(
        table: str,
        filters: dict[str, Any],
        data: dict[str, Any],
        db_path: str = str(DEFAULT_DB_PATH),
    ) -> str:
        """按过滤条件更新 SQLite 数据表中的记录。"""
        return update_rows_tool_payload(
            table=table,
            filters=filters,
            data=data,
            db_path=db_path,
        )

    @tool
    def delete_rows_tool(
        table: str,
        filters: dict[str, Any],
        db_path: str = str(DEFAULT_DB_PATH),
    ) -> str:
        """按过滤条件删除 SQLite 数据表中的记录。"""
        return delete_rows_tool_payload(
            table=table,
            filters=filters,
            db_path=db_path,
        )

else:  # pragma: no cover
    ensure_sqlite_db_tool = ensure_sqlite_db_tool_payload
    create_table_tool = create_table_tool_payload
    insert_row_tool = insert_row_tool_payload
    select_rows_tool = select_rows_tool_payload
    update_rows_tool = update_rows_tool_payload
    delete_rows_tool = delete_rows_tool_payload

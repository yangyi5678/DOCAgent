from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sandbox.filesystem_policy import (
    build_filesystem_policy,
    ensure_delete_allowed,
    ensure_read_allowed,
    ensure_write_allowed,
)

try:
    from langchain.tools import tool
except ImportError:  # pragma: no cover
    tool = None


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWED_ROOTS = [
    ROOT_DIR / "outputs",
    ROOT_DIR / "data",
    ROOT_DIR / "logs",
]


def _resolve_allowed_roots(allowed_roots: list[str | Path] | None = None) -> list[Path]:
    roots = allowed_roots or DEFAULT_ALLOWED_ROOTS
    return [Path(root).resolve() for root in roots]


def _resolve_target_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _ensure_path_allowed(
    path: str | Path,
    *,
    allowed_roots: list[str | Path] | None = None,
) -> Path:
    target = _resolve_target_path(path)
    roots = _resolve_allowed_roots(allowed_roots)

    for root in roots:
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue

    allowed_text = ", ".join(str(root) for root in roots)
    raise PermissionError(f"路径不在允许范围内: {target}。允许目录: {allowed_text}")


def _policy_with_allowed_roots(
    tool_name: str,
    tool_input: dict[str, Any],
    allowed_roots: list[str | Path] | None,
) -> dict[str, Any]:
    policy = build_filesystem_policy(tool_name=tool_name, tool_input=tool_input)
    if allowed_roots is not None:
        roots = [str(root) for root in _resolve_allowed_roots(allowed_roots)]
        policy = {
            **policy,
            "read_roots": roots,
            "write_roots": roots,
            "delete_roots": roots,
            "artifact_roots": roots,
            "allowed_read_paths": roots,
            "allowed_write_paths": roots,
        }
    return policy


def _ensure_filesystem_operation_allowed(
    path: str | Path,
    *,
    tool_name: str,
    operation: str,
    tool_input: dict[str, Any],
    allowed_roots: list[str | Path] | None = None,
) -> Path:
    target = _resolve_target_path(path)
    policy = _policy_with_allowed_roots(tool_name, tool_input, allowed_roots)

    if operation == "read":
        ensure_read_allowed(target, policy)
    elif operation == "write":
        ensure_write_allowed(target, policy)
    elif operation == "delete":
        ensure_delete_allowed(target, policy)
    else:
        raise ValueError(f"未知文件系统操作: {operation}")

    return target


def ensure_directory_core(
    path: str,
    *,
    allowed_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    target = _ensure_filesystem_operation_allowed(
        path,
        tool_name="ensure_directory",
        operation="write",
        tool_input={"path": path},
        allowed_roots=allowed_roots,
    )
    existed = target.exists()
    target.mkdir(parents=True, exist_ok=True)
    return {
        "status": "success",
        "action": "ensure_directory",
        "path": str(target),
        "created": not existed,
    }


def write_text_file_core(
    path: str,
    content: str,
    *,
    create_dirs: bool = True,
    encoding: str = "utf-8",
    allowed_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    target = _ensure_filesystem_operation_allowed(
        path,
        tool_name="write_text_file",
        operation="write",
        tool_input={"path": path, "content": content, "create_dirs": create_dirs},
        allowed_roots=allowed_roots,
    )
    if create_dirs:
        target.parent.mkdir(parents=True, exist_ok=True)

    text = content or ""
    target.write_text(text, encoding=encoding)
    return {
        "status": "success",
        "action": "write_text_file",
        "path": str(target),
        "bytes_written": len(text.encode(encoding)),
    }


def append_text_file_core(
    path: str,
    content: str,
    *,
    create_dirs: bool = True,
    encoding: str = "utf-8",
    allowed_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    target = _ensure_filesystem_operation_allowed(
        path,
        tool_name="append_text_file",
        operation="write",
        tool_input={"path": path, "content": content, "create_dirs": create_dirs},
        allowed_roots=allowed_roots,
    )
    if create_dirs:
        target.parent.mkdir(parents=True, exist_ok=True)

    text = content or ""
    with target.open("a", encoding=encoding) as f:
        f.write(text)

    return {
        "status": "success",
        "action": "append_text_file",
        "path": str(target),
        "bytes_written": len(text.encode(encoding)),
    }


def read_text_file_core(
    path: str,
    *,
    encoding: str = "utf-8",
    allowed_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    target = _ensure_filesystem_operation_allowed(
        path,
        tool_name="read_text_file",
        operation="read",
        tool_input={"path": path},
        allowed_roots=allowed_roots,
    )
    if not target.exists():
        raise FileNotFoundError(f"找不到文件: {target}")

    content = target.read_text(encoding=encoding)
    return {
        "status": "success",
        "action": "read_text_file",
        "path": str(target),
        "content": content,
    }


def replace_in_file_core(
    path: str,
    old: str,
    new: str,
    *,
    encoding: str = "utf-8",
    allowed_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    if old == "":
        raise ValueError("old 不能为空字符串。")

    target = _ensure_filesystem_operation_allowed(
        path,
        tool_name="replace_in_file",
        operation="write",
        tool_input={"path": path, "old": old, "new": new},
        allowed_roots=allowed_roots,
    )
    if not target.exists():
        raise FileNotFoundError(f"找不到文件: {target}")

    content = target.read_text(encoding=encoding)
    replacements = content.count(old)
    updated = content.replace(old, new)
    target.write_text(updated, encoding=encoding)

    return {
        "status": "success",
        "action": "replace_in_file",
        "path": str(target),
        "replacements": replacements,
    }


def delete_file_core(
    path: str,
    *,
    missing_ok: bool = True,
    allowed_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    target = _ensure_filesystem_operation_allowed(
        path,
        tool_name="delete_file",
        operation="delete",
        tool_input={"path": path, "missing_ok": missing_ok},
        allowed_roots=allowed_roots,
    )
    if not target.exists():
        if missing_ok:
            return {
                "status": "success",
                "action": "delete_file",
                "path": str(target),
                "deleted": False,
                "missing": True,
            }
        raise FileNotFoundError(f"找不到文件: {target}")

    if target.is_dir():
        raise IsADirectoryError(f"目标是目录，不是文件: {target}")

    target.unlink()
    return {
        "status": "success",
        "action": "delete_file",
        "path": str(target),
        "deleted": True,
        "missing": False,
    }


def list_directory_core(
    path: str,
    *,
    include_hidden: bool = False,
    allowed_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    target = _ensure_filesystem_operation_allowed(
        path,
        tool_name="list_directory",
        operation="read",
        tool_input={"path": path, "include_hidden": include_hidden},
        allowed_roots=allowed_roots,
    )
    if not target.exists():
        raise FileNotFoundError(f"找不到目录: {target}")
    if not target.is_dir():
        raise NotADirectoryError(f"目标不是目录: {target}")

    entries: list[dict[str, Any]] = []
    for item in sorted(target.iterdir(), key=lambda p: p.name):
        if not include_hidden and item.name.startswith("."):
            continue
        entries.append(
            {
                "name": item.name,
                "path": str(item.resolve()),
                "is_dir": item.is_dir(),
                "is_file": item.is_file(),
            }
        )

    return {
        "status": "success",
        "action": "list_directory",
        "path": str(target),
        "count": len(entries),
        "entries": entries,
    }


def _to_tool_result(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)


def ensure_directory_tool_payload(path: str) -> str:
    return _to_tool_result(ensure_directory_core(path))


def write_text_file_tool_payload(
    path: str,
    content: str,
    create_dirs: bool = True,
) -> str:
    return _to_tool_result(
        write_text_file_core(path=path, content=content, create_dirs=create_dirs)
    )


def append_text_file_tool_payload(
    path: str,
    content: str,
    create_dirs: bool = True,
) -> str:
    return _to_tool_result(
        append_text_file_core(path=path, content=content, create_dirs=create_dirs)
    )


def read_text_file_tool_payload(path: str) -> str:
    return _to_tool_result(read_text_file_core(path))


def replace_in_file_tool_payload(path: str, old: str, new: str) -> str:
    return _to_tool_result(replace_in_file_core(path=path, old=old, new=new))


def delete_file_tool_payload(path: str, missing_ok: bool = True) -> str:
    return _to_tool_result(delete_file_core(path=path, missing_ok=missing_ok))


def list_directory_tool_payload(path: str, include_hidden: bool = False) -> str:
    return _to_tool_result(
        list_directory_core(path=path, include_hidden=include_hidden)
    )


if tool is not None:

    @tool
    def ensure_directory_tool(path: str) -> str:
        """创建目录；如果目录已存在则直接返回成功。"""
        return ensure_directory_tool_payload(path)

    @tool
    def write_text_file_tool(
        path: str,
        content: str,
        create_dirs: bool = True,
    ) -> str:
        """覆盖写入文本文件，可选自动创建父目录。"""
        return write_text_file_tool_payload(
            path=path,
            content=content,
            create_dirs=create_dirs,
        )

    @tool
    def append_text_file_tool(
        path: str,
        content: str,
        create_dirs: bool = True,
    ) -> str:
        """向文本文件末尾追加内容，可选自动创建父目录。"""
        return append_text_file_tool_payload(
            path=path,
            content=content,
            create_dirs=create_dirs,
        )

    @tool
    def read_text_file_tool(path: str) -> str:
        """读取文本文件内容。"""
        return read_text_file_tool_payload(path)

    @tool
    def replace_in_file_tool(path: str, old: str, new: str) -> str:
        """在文本文件中将指定旧内容替换为新内容。"""
        return replace_in_file_tool_payload(path=path, old=old, new=new)

    @tool
    def delete_file_tool(path: str, missing_ok: bool = True) -> str:
        """删除单个文件；默认文件不存在时不报错。"""
        return delete_file_tool_payload(path=path, missing_ok=missing_ok)

    @tool
    def list_directory_tool(path: str, include_hidden: bool = False) -> str:
        """列出目录下的文件和子目录。"""
        return list_directory_tool_payload(
            path=path,
            include_hidden=include_hidden,
        )

else:  # pragma: no cover
    ensure_directory_tool = ensure_directory_tool_payload
    write_text_file_tool = write_text_file_tool_payload
    append_text_file_tool = append_text_file_tool_payload
    read_text_file_tool = read_text_file_tool_payload
    replace_in_file_tool = replace_in_file_tool_payload
    delete_file_tool = delete_file_tool_payload
    list_directory_tool = list_directory_tool_payload

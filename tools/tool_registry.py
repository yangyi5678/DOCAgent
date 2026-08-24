from __future__ import annotations

from collections.abc import Iterable

from tools.code_analysis import (
    code_search_core,
    code_search_tool,
    diagnose_incident_core,
    diagnose_incident_tool,
    extract_code_symbols_core,
    extract_code_symbols_tool,
    parse_log_timeline_core,
    parse_log_timeline_tool,
    scan_code_tree_core,
    scan_code_tree_tool,
    summarize_code_architecture_core,
    summarize_code_architecture_tool,
    summarize_code_files_core,
    summarize_code_files_tool,
)
from tools.file_ops import (
    append_text_file_tool,
    append_text_file_core,
    delete_file_tool,
    delete_file_core,
    ensure_directory_tool,
    ensure_directory_core,
    list_directory_tool,
    list_directory_core,
    read_text_file_tool,
    read_text_file_core,
    replace_in_file_tool,
    replace_in_file_core,
    write_text_file_tool,
    write_text_file_core,
)
from tools.minimax_image_http import minimax_generate_image_core, minimax_generate_image_tool
from tools.minimax_tts_http import minimax_tts_core, minimax_tts_tool
from tools.shell_ops import run_shell_command_core, run_shell_command_tool
from tools.sqlite_ops import (
    create_table_core,
    create_table_tool,
    delete_rows_core,
    delete_rows_tool,
    ensure_sqlite_db_core,
    ensure_sqlite_db_tool,
    insert_row_core,
    insert_row_tool,
    select_rows_core,
    select_rows_tool,
    update_rows_core,
    update_rows_tool,
)
from tools.tool_type import ToolCategory, ToolSource, ToolSpec


PATH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
    },
    "required": ["path"],
}

TEXT_FILE_WRITE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
        "create_dirs": {"type": "boolean"},
    },
    "required": ["path", "content"],
}

TEXT_FILE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "action": {"type": "string"},
        "path": {"type": "string"},
    },
}

SQLITE_DB_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "db_path": {"type": "string"},
    },
}

SQLITE_TABLE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "table": {"type": "string"},
        "db_path": {"type": "string"},
    },
    "required": ["table"],
}

SQLITE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "action": {"type": "string"},
        "db_path": {"type": "string"},
        "table": {"type": "string"},
    },
}

SHELL_COMMAND_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
        },
        "cwd": {"type": "string"},
        "timeout_seconds": {"type": "number"},
        "max_output_bytes": {"type": "integer"},
        "env": {"type": "object"},
        "allowed_commands": {"type": "array", "items": {"type": "string"}},
        "approval_commands": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "string"}},
        },
        "approved": {"type": "boolean"},
        "allowed_roots": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["command"],
}

SHELL_COMMAND_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "action": {"type": "string"},
        "command": {"type": "array", "items": {"type": "string"}},
        "cwd": {"type": "string"},
        "returncode": {"type": ["integer", "null"]},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
        "timed_out": {"type": "boolean"},
        "elapsed_ms": {"type": "integer"},
        "approval_required": {"type": "boolean"},
        "approval_reason": {"type": "string"},
    },
}

CODE_PROJECT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "project_path": {"type": "string"},
        "include_ext": {"type": "array", "items": {"type": "string"}},
        "exclude_dirs": {"type": "array", "items": {"type": "string"}},
        "max_files": {"type": "integer"},
    },
    "required": ["project_path"],
}

CODE_SEARCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "project_path": {"type": "string"},
        "query": {"type": "string"},
        "include_ext": {"type": "array", "items": {"type": "string"}},
        "exclude_dirs": {"type": "array", "items": {"type": "string"}},
        "max_files": {"type": "integer"},
        "max_matches": {"type": "integer"},
        "context_lines": {"type": "integer"},
    },
    "required": ["project_path", "query"],
}

LOG_TIMELINE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "log_text": {"type": "string"},
        "log_path": {"type": "string"},
        "event_time": {"type": "string"},
        "max_events": {"type": "integer"},
    },
}

INCIDENT_DIAGNOSE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "problem_description": {"type": "string"},
        "event_time": {"type": "string"},
        "project_path": {"type": "string"},
        "log_text": {"type": "string"},
        "log_path": {"type": "string"},
        "architecture_summary": {"type": "string"},
        "search_query": {"type": "string"},
        "max_code_matches": {"type": "integer"},
    },
    "required": ["problem_description"],
}

CODE_ANALYSIS_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "action": {"type": "string"},
        "project_path": {"type": "string"},
    },
}


TOOL_REGISTRY: list[ToolSpec] = [
    ToolSpec(
        name="scan_code_tree",
        description="扫描工程目录，输出代码文件树、文件类型分布、核心目录和候选代码文件。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.TEXT,
        input_schema={
            **CODE_PROJECT_INPUT_SCHEMA,
            "properties": {
                **CODE_PROJECT_INPUT_SCHEMA["properties"],
                "max_depth": {"type": "integer"},
            },
        },
        output_schema={
            **CODE_ANALYSIS_RESULT_SCHEMA,
            "properties": {
                **CODE_ANALYSIS_RESULT_SCHEMA["properties"],
                "file_count": {"type": "integer"},
                "extension_counts": {"type": "object"},
                "top_dirs": {"type": "array"},
                "files": {"type": "array"},
            },
        },
        capabilities=["code.scan_tree", "codebase.scan", "code.tree"],
        tool_name="scan_code_tree_tool",
        core_name="scan_code_tree_core",
        tags=["code", "codebase", "scan", "architecture", "代码工程"],
        tool_obj=scan_code_tree_tool,
        core_func=scan_code_tree_core,
    ),
    ToolSpec(
        name="extract_code_symbols",
        description="提取工程中的类、函数、方法、include/import 等符号信息。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.TEXT,
        input_schema={
            **CODE_PROJECT_INPUT_SCHEMA,
            "properties": {
                **CODE_PROJECT_INPUT_SCHEMA["properties"],
                "max_symbols_per_file": {"type": "integer"},
            },
        },
        output_schema={
            **CODE_ANALYSIS_RESULT_SCHEMA,
            "properties": {
                **CODE_ANALYSIS_RESULT_SCHEMA["properties"],
                "file_count": {"type": "integer"},
                "symbol_count": {"type": "integer"},
                "files": {"type": "array"},
            },
        },
        capabilities=["code.extract_symbols", "code.symbol.extract", "code.symbols"],
        tool_name="extract_code_symbols_tool",
        core_name="extract_code_symbols_core",
        tags=["code", "symbols", "function", "class", "代码符号"],
        tool_obj=extract_code_symbols_tool,
        core_func=extract_code_symbols_core,
    ),
    ToolSpec(
        name="summarize_code_files",
        description="按文件生成代码职责、关键符号、依赖和内容预览摘要。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.TEXT,
        input_schema={
            **CODE_PROJECT_INPUT_SCHEMA,
            "properties": {
                **CODE_PROJECT_INPUT_SCHEMA["properties"],
                "max_chars_per_file": {"type": "integer"},
            },
        },
        output_schema={
            **CODE_ANALYSIS_RESULT_SCHEMA,
            "properties": {
                **CODE_ANALYSIS_RESULT_SCHEMA["properties"],
                "file_count": {"type": "integer"},
                "files": {"type": "array"},
            },
        },
        capabilities=["code.summarize_files", "code.file.summarize", "text.summarize"],
        tool_name="summarize_code_files_tool",
        core_name="summarize_code_files_core",
        tags=["code", "summary", "file", "代码摘要"],
        tool_obj=summarize_code_files_tool,
        core_func=summarize_code_files_core,
    ),
    ToolSpec(
        name="summarize_code_architecture",
        description="根据目录树和符号表生成工程模块架构摘要。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.TEXT,
        input_schema=CODE_PROJECT_INPUT_SCHEMA,
        output_schema={
            **CODE_ANALYSIS_RESULT_SCHEMA,
            "properties": {
                **CODE_ANALYSIS_RESULT_SCHEMA["properties"],
                "overview": {"type": "object"},
                "modules": {"type": "array"},
                "architecture_summary": {"type": "string"},
            },
        },
        capabilities=[
            "code.summarize_architecture",
            "code.architecture.summarize",
            "codebase.architecture",
        ],
        tool_name="summarize_code_architecture_tool",
        core_name="summarize_code_architecture_core",
        tags=["code", "architecture", "summary", "代码架构"],
        tool_obj=summarize_code_architecture_tool,
        core_func=summarize_code_architecture_core,
    ),
    ToolSpec(
        name="code_search",
        description="在工程代码和配置中搜索问题、日志关键字或符号名，并返回上下文。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.SEARCH,
        input_schema=CODE_SEARCH_INPUT_SCHEMA,
        output_schema={
            **CODE_ANALYSIS_RESULT_SCHEMA,
            "properties": {
                **CODE_ANALYSIS_RESULT_SCHEMA["properties"],
                "query": {"type": "string"},
                "count": {"type": "integer"},
                "matches": {"type": "array"},
            },
        },
        capabilities=["code.search", "code.retrieve", "search.query"],
        tool_name="code_search_tool",
        core_name="code_search_core",
        tags=["code", "search", "retrieve", "代码检索"],
        tool_obj=code_search_tool,
        core_func=code_search_core,
    ),
    ToolSpec(
        name="parse_log_timeline",
        description="解析日志文本或日志文件，提取时间线、等级统计和异常事件。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.TEXT,
        input_schema=LOG_TIMELINE_INPUT_SCHEMA,
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "action": {"type": "string"},
                "event_count": {"type": "integer"},
                "level_counts": {"type": "object"},
                "timeline": {"type": "array"},
                "anomalies": {"type": "array"},
            },
        },
        capabilities=["log.parse_timeline", "log.timeline", "log.parse"],
        tool_name="parse_log_timeline_tool",
        core_name="parse_log_timeline_core",
        tags=["log", "timeline", "incident", "日志"],
        tool_obj=parse_log_timeline_tool,
        core_func=parse_log_timeline_core,
    ),
    ToolSpec(
        name="diagnose_incident",
        description="结合问题描述、日志和代码搜索证据，输出 incident 初步诊断。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.TEXT,
        input_schema=INCIDENT_DIAGNOSE_INPUT_SCHEMA,
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "action": {"type": "string"},
                "root_cause_candidates": {"type": "array"},
                "evidence": {"type": "array"},
                "verification_steps": {"type": "array"},
                "fix_suggestions": {"type": "array"},
            },
        },
        capabilities=["incident.diagnose", "code.incident.diagnose", "root_cause.analyze"],
        tool_name="diagnose_incident_tool",
        core_name="diagnose_incident_core",
        tags=["incident", "diagnose", "log", "code", "问题诊断"],
        tool_obj=diagnose_incident_tool,
        core_func=diagnose_incident_core,
    ),
    ToolSpec(
        name="ensure_directory",
        description="创建目录；如果目录已存在则直接返回成功。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.FILE,
        input_schema=PATH_INPUT_SCHEMA,
        output_schema=TEXT_FILE_RESULT_SCHEMA,
        capabilities=["filesystem", "directory.create", "directory.ensure"],
        tool_name="ensure_directory_tool",
        core_name="ensure_directory_core",
        tags=["file", "directory", "mkdir", "文件系统"],
        tool_obj=ensure_directory_tool,
        core_func=ensure_directory_core,
    ),
    ToolSpec(
        name="write_text_file",
        description="覆盖写入文本文件，可选自动创建父目录。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.FILE,
        input_schema=TEXT_FILE_WRITE_INPUT_SCHEMA,
        output_schema=TEXT_FILE_RESULT_SCHEMA,
        capabilities=["filesystem", "file.write", "text.write"],
        tool_name="write_text_file_tool",
        core_name="write_text_file_core",
        tags=["file", "write", "text", "文件系统"],
        tool_obj=write_text_file_tool,
        core_func=write_text_file_core,
    ),
    ToolSpec(
        name="append_text_file",
        description="向文本文件末尾追加内容，可选自动创建父目录。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.FILE,
        input_schema=TEXT_FILE_WRITE_INPUT_SCHEMA,
        output_schema=TEXT_FILE_RESULT_SCHEMA,
        capabilities=["filesystem", "file.append", "text.write"],
        tool_name="append_text_file_tool",
        core_name="append_text_file_core",
        tags=["file", "append", "text", "文件系统"],
        tool_obj=append_text_file_tool,
        core_func=append_text_file_core,
    ),
    ToolSpec(
        name="read_text_file",
        description="读取文本文件内容。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.FILE,
        input_schema=PATH_INPUT_SCHEMA,
        output_schema={
            **TEXT_FILE_RESULT_SCHEMA,
            "properties": {
                **TEXT_FILE_RESULT_SCHEMA["properties"],
                "content": {"type": "string"},
            },
        },
        capabilities=["filesystem", "file.read", "text.read"],
        tool_name="read_text_file_tool",
        core_name="read_text_file_core",
        tags=["file", "read", "text", "文件系统"],
        tool_obj=read_text_file_tool,
        core_func=read_text_file_core,
    ),
    ToolSpec(
        name="replace_in_file",
        description="在文本文件中将指定旧内容替换为新内容。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.FILE,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["path", "old", "new"],
        },
        output_schema=TEXT_FILE_RESULT_SCHEMA,
        capabilities=["filesystem", "file.replace", "text.edit"],
        tool_name="replace_in_file_tool",
        core_name="replace_in_file_core",
        tags=["file", "replace", "text", "文件系统"],
        tool_obj=replace_in_file_tool,
        core_func=replace_in_file_core,
    ),
    ToolSpec(
        name="delete_file",
        description="删除单个文件；默认文件不存在时不报错。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.FILE,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "missing_ok": {"type": "boolean"},
            },
            "required": ["path"],
        },
        output_schema=TEXT_FILE_RESULT_SCHEMA,
        capabilities=["filesystem", "file.delete"],
        tool_name="delete_file_tool",
        core_name="delete_file_core",
        tags=["file", "delete", "文件系统"],
        tool_obj=delete_file_tool,
        core_func=delete_file_core,
    ),
    ToolSpec(
        name="list_directory",
        description="列出目录下的文件和子目录。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.FILE,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "include_hidden": {"type": "boolean"},
            },
            "required": ["path"],
        },
        output_schema={
            **TEXT_FILE_RESULT_SCHEMA,
            "properties": {
                **TEXT_FILE_RESULT_SCHEMA["properties"],
                "count": {"type": "integer"},
                "entries": {"type": "array"},
            },
        },
        capabilities=["filesystem", "directory.list"],
        tool_name="list_directory_tool",
        core_name="list_directory_core",
        tags=["file", "directory", "list", "文件系统"],
        tool_obj=list_directory_tool,
        core_func=list_directory_core,
    ),
    ToolSpec(
        name="run_shell_command",
        description="在受限工作目录内执行允许列表中的本地命令，返回 stdout、stderr、退出码和耗时。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.SYSTEM,
        input_schema=SHELL_COMMAND_INPUT_SCHEMA,
        output_schema=SHELL_COMMAND_RESULT_SCHEMA,
        capabilities=["process", "shell", "command.run", "subprocess"],
        tool_name="run_shell_command_tool",
        core_name="run_shell_command_core",
        tags=["shell", "command", "subprocess", "系统命令"],
        tool_obj=run_shell_command_tool,
        core_func=run_shell_command_core,
    ),
    ToolSpec(
        name="ensure_sqlite_db",
        description="确保 SQLite 数据库文件存在。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.DATABASE,
        input_schema=SQLITE_DB_INPUT_SCHEMA,
        output_schema=SQLITE_RESULT_SCHEMA,
        capabilities=["sqlite", "database.ensure"],
        tool_name="ensure_sqlite_db_tool",
        core_name="ensure_sqlite_db_core",
        tags=["sqlite", "database", "db", "数据库"],
        tool_obj=ensure_sqlite_db_tool,
        core_func=ensure_sqlite_db_core,
    ),
    ToolSpec(
        name="create_table",
        description="在 SQLite 数据库中创建数据表。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.DATABASE,
        input_schema={
            **SQLITE_TABLE_INPUT_SCHEMA,
            "properties": {
                **SQLITE_TABLE_INPUT_SCHEMA["properties"],
                "schema_sql": {"type": "string"},
            },
            "required": ["table", "schema_sql"],
        },
        output_schema=SQLITE_RESULT_SCHEMA,
        capabilities=["sqlite", "database.schema", "table.create"],
        tool_name="create_table_tool",
        core_name="create_table_core",
        tags=["sqlite", "database", "table", "数据库"],
        tool_obj=create_table_tool,
        core_func=create_table_core,
    ),
    ToolSpec(
        name="insert_row",
        description="向 SQLite 数据表插入一行记录。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.DATABASE,
        input_schema={
            **SQLITE_TABLE_INPUT_SCHEMA,
            "properties": {
                **SQLITE_TABLE_INPUT_SCHEMA["properties"],
                "data": {"type": "object"},
            },
            "required": ["table", "data"],
        },
        output_schema=SQLITE_RESULT_SCHEMA,
        capabilities=["sqlite", "database.write", "row.insert"],
        tool_name="insert_row_tool",
        core_name="insert_row_core",
        tags=["sqlite", "database", "insert", "数据库"],
        tool_obj=insert_row_tool,
        core_func=insert_row_core,
    ),
    ToolSpec(
        name="select_rows",
        description="从 SQLite 数据表查询记录。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.DATABASE,
        input_schema={
            **SQLITE_TABLE_INPUT_SCHEMA,
            "properties": {
                **SQLITE_TABLE_INPUT_SCHEMA["properties"],
                "filters": {"type": "object"},
                "limit": {"type": "integer"},
            },
        },
        output_schema={
            **SQLITE_RESULT_SCHEMA,
            "properties": {
                **SQLITE_RESULT_SCHEMA["properties"],
                "count": {"type": "integer"},
                "rows": {"type": "array"},
            },
        },
        capabilities=["sqlite", "database.read", "row.select"],
        tool_name="select_rows_tool",
        core_name="select_rows_core",
        tags=["sqlite", "database", "select", "数据库"],
        tool_obj=select_rows_tool,
        core_func=select_rows_core,
    ),
    ToolSpec(
        name="update_rows",
        description="按过滤条件更新 SQLite 数据表中的记录。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.DATABASE,
        input_schema={
            **SQLITE_TABLE_INPUT_SCHEMA,
            "properties": {
                **SQLITE_TABLE_INPUT_SCHEMA["properties"],
                "filters": {"type": "object"},
                "data": {"type": "object"},
            },
            "required": ["table", "filters", "data"],
        },
        output_schema=SQLITE_RESULT_SCHEMA,
        capabilities=["sqlite", "database.write", "row.update"],
        tool_name="update_rows_tool",
        core_name="update_rows_core",
        tags=["sqlite", "database", "update", "数据库"],
        tool_obj=update_rows_tool,
        core_func=update_rows_core,
    ),
    ToolSpec(
        name="delete_rows",
        description="按过滤条件删除 SQLite 数据表中的记录。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.DATABASE,
        input_schema={
            **SQLITE_TABLE_INPUT_SCHEMA,
            "properties": {
                **SQLITE_TABLE_INPUT_SCHEMA["properties"],
                "filters": {"type": "object"},
            },
            "required": ["table", "filters"],
        },
        output_schema=SQLITE_RESULT_SCHEMA,
        capabilities=["sqlite", "database.write", "row.delete"],
        tool_name="delete_rows_tool",
        core_name="delete_rows_core",
        tags=["sqlite", "database", "delete", "数据库"],
        tool_obj=delete_rows_tool,
        core_func=delete_rows_core,
    ),
    ToolSpec(
        name="minimax_generate_image",
        description="根据文本提示词生成图片；支持设置比例、格式，并可传入参考人物图片 URL 保持角色一致性。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.IMAGE,
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "output_dir": {"type": "string"},
                "file_prefix": {"type": "string"},
                "image_format": {"type": "string"},
                "aspect_ratio": {"type": "string"},
                "character_image_url": {"type": "string"},
            },
            "required": ["prompt"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "image_count": {"type": "integer"},
                "output_paths": {"type": "array", "items": {"type": "string"}},
            },
        },
        capabilities=["image.generate", "image.text_to_image", "minimax"],
        tool_name="minimax_generate_image_tool",
        core_name="minimax_generate_image_core",
        tags=["minimax", "image", "生成图片"],
        tool_obj=minimax_generate_image_tool,
        core_func=minimax_generate_image_core,
    ),
    ToolSpec(
        name="minimax_tts",
        description="将中文口播文本合成为 MP3 音频，支持音色、语速、音量和音调配置。",
        source=ToolSource.LOCAL_PYTHON,
        category=ToolCategory.AUDIO,
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "output_file": {"type": "string"},
                "output_dir": {"type": "string"},
                "voice_id": {"type": "string"},
                "speed": {"type": "number"},
                "volume": {"type": "number"},
                "pitch": {"type": "number"},
            },
            "required": ["text"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "output_path": {"type": "string"},
                "audio_format": {"type": "string"},
                "audio_sample_rate": {"type": "integer"},
                "audio_size": {"type": "integer"},
                "audio_length": {"type": "number"},
                "voice_id": {"type": "string"},
            },
        },
        capabilities=["audio.generate", "audio.text_to_speech", "minimax"],
        tool_name="minimax_tts_tool",
        core_name="minimax_tts_core",
        tags=["minimax", "tts", "audio", "语音合成"],
        tool_obj=minimax_tts_tool,
        core_func=minimax_tts_core,
    ),
]


def iter_tool_specs(*, enabled_only: bool = True) -> Iterable[ToolSpec]:
    """Yield registered tool specs, optionally filtering disabled tools."""
    for spec in TOOL_REGISTRY:
        if enabled_only and not spec.enabled:
            continue
        yield spec


def get_tool_spec(name: str, *, enabled_only: bool = True) -> ToolSpec:
    """Return one registered tool spec by name."""
    for spec in iter_tool_specs(enabled_only=enabled_only):
        if name in {spec.name, spec.tool_name, spec.core_name}:
            return spec
    raise KeyError(f"未找到 tool spec: {name}")


def get_tool(name: str):
    """Return the callable tool object registered under ``name``."""
    spec = get_tool_spec(name)
    if spec.tool_obj is None:
        raise ValueError(f"tool 没有关联 Python 对象: {name}")
    return spec.tool_obj


def get_enabled_tools() -> list:
    """Return callable objects for all enabled Python-backed tools."""
    return [
        spec.tool_obj
        for spec in iter_tool_specs(enabled_only=True)
        if spec.tool_obj is not None
    ]


def get_tool_specs_by_category(category: ToolCategory) -> list[ToolSpec]:
    """Return enabled tool specs in a category."""
    return [
        spec
        for spec in iter_tool_specs(enabled_only=True)
        if spec.category == category
    ]


def get_tool_specs_by_capability(capability: str) -> list[ToolSpec]:
    """Return enabled tool specs that advertise one capability."""
    return [
        spec
        for spec in iter_tool_specs(enabled_only=True)
        if capability in spec.capabilities
    ]

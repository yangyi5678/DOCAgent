from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class ToolSource(str, Enum):
    """Where a registered tool is executed from."""

    LOCAL_PYTHON = "local_python"
    LOCAL_MCP = "local_mcp"


class ToolCategory(str, Enum):
    """High-level category used for planner/tool selection."""

    AUDIO = "audio"
    DATABASE = "database"
    FILE = "file"
    IMAGE = "image"
    SEARCH = "search"
    SYSTEM = "system"
    TEXT = "text"
    VIDEO = "video"


@dataclass(frozen=True)
class ToolSpec:
    """Metadata and callable object for one registered tool."""

    name: str
    description: str
    source: ToolSource
    category: ToolCategory
    enabled: bool = True
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    tool_name: str | None = None
    core_name: str | None = None
    tool_obj: Callable[..., Any] | None = None
    core_func: Callable[..., Any] | None = None
    tags: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

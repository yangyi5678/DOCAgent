from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolSource(str, Enum):
    LOCAL_PYTHON = "local_python"
    MCP = "mcp"
    HTTP_API = "http_api"
    BUILTIN = "builtin"


class ToolCategory(str, Enum):
    WEB = "web"
    DOCUMENT = "document"
    DATA = "data"
    CODE = "code"
    AUDIO = "audio"
    IMAGE = "image"
    MEMORY = "memory"
    REPORT = "report"


@dataclass
class MainAgentToolSpec:
    name: str
    description: str
    source: ToolSource
    category: ToolCategory
    tags: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    permissions: list[str] = field(default_factory=list)
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)
    tool_obj: Any | None = None

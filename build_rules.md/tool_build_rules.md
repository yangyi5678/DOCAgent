# Skill: Standardized Tool Building

## Purpose

When building tools for agents, always follow a standardized structure.

A tool is not just a Python function.  
A tool must have:

- core implementation
- thin wrapper
- normalized spec
- optional framework decorator
- registry entry
- executor-compatible call contract

---

## Required Tool Architecture

Every local Python tool must follow this chain:

```text
core function
  ↓
thin wrapper tool function
  ↓
@tool(...) optional framework adapter
  ↓
MainAgentToolSpec
  ↓
ToolRegistry
  ↓
Executor / Agent
```

---

## File Structure

Use this structure for each capability domain:

```text
tools/
  tool_spec.py          # ToolSource, ToolCategory, MainAgentToolSpec
  tool_registry.py      # registry, selector, resolver

  <domain>_core.py      # pure implementation functions
  <domain>_tools.py     # thin wrappers and @tool-decorated functions
  <domain>_specs.py     # MainAgentToolSpec definitions
```

Example:

```text
tools/
  tool_spec.py
  tool_registry.py

  web_core.py
  web_tools.py
  web_specs.py
```

---

## Standard ToolSpec

All tools must use this normalized spec shape:

```python
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
```

---

## Field Meaning

```text
name          Stable unique tool name
description   Human / planner-readable tool description
source        How the tool is called: local_python / mcp / http_api / builtin
category      Capability domain: web / document / data / code / audio
tags          Fine-grained labels: browser / download / sql / read_only
input_schema  Tool argument contract
output_schema Tool result contract
permissions   Machine-readable safety declarations
enabled       Whether the tool is available
config        Runtime configuration
tool_obj      Executable local tool object
```

---

## Source / Category / Tags Rules

Do not mix these dimensions.

```text
source
  = where the tool comes from / how it is invoked

category
  = what capability domain it belongs to

tags
  = fine-grained searchable labels
```

Correct example:

```python
MainAgentToolSpec(
    name="sql.query",
    source=ToolSource.LOCAL_PYTHON,
    category=ToolCategory.DATA,
    tags=["database", "sql", "read_only"],
)
```

Incorrect example:

```python
source=ToolSource.DATABASE
```

`DATABASE` is not a source. It should be a tag or data category.

---

## Permission Rules

Permissions are machine-readable safety metadata.

Common permissions:

```text
network
browser
filesystem
download
shell
code_exec
secret_read
database
```

Examples:

```python
permissions=["network", "browser"]
```

```python
permissions=["network", "filesystem", "download"]
```

Do not rely on docstrings for permissions.  
Docstrings are human-readable. Permissions are executable policy metadata.

---

## Core Function Rules

Core functions contain the real implementation.

Core functions must:

- not depend on Agent runtime
- not depend on registry
- not depend on LangChain / LangGraph decorators
- not know about `MainAgentToolSpec`
- be directly unit-testable
- do one clear thing

Example:

```python
def core_classify_source(url: str) -> str:
    ...
```

---

## Thin Wrapper Rules

Thin wrapper functions are the official tool entrypoints.

They must:

- call the core function
- normalize inputs if necessary
- catch exceptions
- return a stable result shape
- be usable by internal agents
- be usable as `tool_obj`
- optionally be decorated with framework `@tool(...)`

Standard result shape:

```python
{
    "success": bool,
    "tool": str,
    "data": dict | list | str | None,
    "error": str | None,
}
```

Example:

```python
@tool("url_classify_source")
async def classify_source_tool(url: str) -> dict:
    try:
        source_type = core_classify_source(url)
        return {
            "success": True,
            "tool": "url.classify_source",
            "data": {"source_type": source_type},
            "error": None,
        }
    except Exception as e:
        return {
            "success": False,
            "tool": "url.classify_source",
            "data": None,
            "error": str(e),
        }
```

---

## Decorator Rule

`@tool(...)` is only a framework adapter.

It makes the wrapper compatible with:

```python
create_agent(tools=[...])
```

The decorator does not replace the registry.

The decorated function should be assigned to `tool_obj`.

Example:

```python
@tool("url_classify_source")
async def classify_source_tool(url: str) -> dict:
    ...
```

Then:

```python
tool_obj=classify_source_tool
```

---

## Registry Rules

The registry is the tool directory.

It must support:

- selecting tools by source
- selecting tools by category
- selecting tools by tags
- resolving executable tool objects
- excluding disabled tools

Example:

```python
TOOL_REGISTRY = [
    WEB_SEARCH_TOOL_SPEC,
    DOC_PARSE_TOOL_SPEC,
]
```

A resolver should return executable tools:

```python
def resolve_tools(specs: list[MainAgentToolSpec]) -> list[Any]:
    return [
        spec.tool_obj
        for spec in specs
        if spec.enabled and spec.tool_obj is not None
    ]
```

---

## Tool Definition Pattern

Every local Python tool should follow this pattern.

### 1. Core function

```python
def core_xxx(...):
    ...
```

### 2. Thin wrapper

```python
@tool("xxx")
async def xxx_tool(...):
    return core_xxx(...)
```

### 3. Spec

```python
XXX_TOOL_SPEC = MainAgentToolSpec(
    name="xxx",
    description="...",
    source=ToolSource.LOCAL_PYTHON,
    category=ToolCategory.WEB,
    tags=["..."],
    input_schema={...},
    output_schema={...},
    permissions=[...],
    enabled=True,
    config={},
    tool_obj=xxx_tool,
)
```

### 4. Registry

```python
TOOL_REGISTRY = [
    XXX_TOOL_SPEC,
]
```

---

## Naming Rules

Tool names must be stable and namespaced.

Recommended format:

```text
<domain>.<action>
```

Examples:

```text
search.query
browser.goto
browser.extract
browser.download
url.classify_source
doc.parse
file.inspect
sql.query
```

Framework decorator names may use framework-compatible names:

```python
@tool("browser_goto")
```

But `MainAgentToolSpec.name` should use the canonical internal name:

```python
name="browser.goto"
```

---

## Execution Rules

Agent executors should not import random functions directly.

They should execute via resolved tool specs or registry.

Execution flow:

```text
Planner outputs action
  ↓
Executor finds tool spec by name
  ↓
Executor checks enabled / permissions / source
  ↓
Executor calls tool_obj or external MCP/API
  ↓
Executor returns observation
```

---

## Local Python Tool Rule

For `ToolSource.LOCAL_PYTHON`:

```text
tool_obj must not be None
```

Usually:

```python
tool_obj=<decorated wrapper function>
```

---

## MCP Tool Rule

For `ToolSource.MCP`:

```text
tool_obj is usually None
config must contain MCP server/tool mapping
```

Example:

```python
config={
    "server": "playwright",
    "tool_name": "browser.goto",
}
```

---

## HTTP API Tool Rule

For `ToolSource.HTTP_API`:

```text
tool_obj may be None
config must contain base_url / auth / timeout info
```

Example:

```python
config={
    "base_url": "https://api.example.com",
    "api_key_env": "EXAMPLE_API_KEY",
    "timeout": 30,
}
```

---

## Forbidden Patterns

Do not:

- put core logic in specs
- put core logic in registry
- use `config={}` as a dataclass default
- hide network/filesystem behavior from permissions
- use docstring as a replacement for structured spec
- mix source/category/tags
- register duplicate tool names
- let disabled tools be exposed to agents
- let tools return inconsistent shapes
- let wrapper functions perform broad business reasoning

---

## Required Output Contract

Every tool wrapper must return:

```python
{
    "success": bool,
    "tool": str,
    "data": Any | None,
    "error": str | None,
}
```

Do not raise normal runtime errors from wrapper tools unless the error is unrecoverable.

---

## Summary

Use this rule for every reusable agent tool:

```text
core function = implementation
thin wrapper = safe callable boundary
@tool = framework adapter
MainAgentToolSpec = metadata contract
ToolRegistry = tool directory
Executor = runtime caller
```
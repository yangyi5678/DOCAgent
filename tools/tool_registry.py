from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .tool_spec import MainAgentToolSpec, ToolCategory, ToolSource


@dataclass
class ToolRegistry:
    _by_name: dict[str, MainAgentToolSpec] = field(default_factory=dict)

    def register(self, spec: MainAgentToolSpec) -> None:
        if spec.name in self._by_name:
            raise ValueError(f"duplicate tool name: {spec.name}")
        if spec.source == ToolSource.LOCAL_PYTHON and spec.tool_obj is None:
            raise ValueError(f"LOCAL_PYTHON tool requires tool_obj: {spec.name}")
        self._by_name[spec.name] = spec

    def register_many(self, specs: list[MainAgentToolSpec]) -> None:
        for s in specs:
            self.register(s)

    def get(self, name: str) -> MainAgentToolSpec:
        return self._by_name[name]

    def all_specs(self) -> list[MainAgentToolSpec]:
        return list(self._by_name.values())

    def by_source(self, source: ToolSource) -> list[MainAgentToolSpec]:
        return [s for s in self._by_name.values() if s.enabled and s.source == source]

    def by_category(self, category: ToolCategory) -> list[MainAgentToolSpec]:
        return [s for s in self._by_name.values() if s.enabled and s.category == category]

    def by_tags(self, tags: list[str]) -> list[MainAgentToolSpec]:
        tagset = set(tags)
        return [s for s in self._by_name.values() if s.enabled and tagset.issubset(set(s.tags))]

    def resolve_tools(self, specs: list[MainAgentToolSpec] | None = None) -> list[Any]:
        selected = specs if specs is not None else self.all_specs()
        return [s.tool_obj for s in selected if s.enabled and s.tool_obj is not None]

"""工具分发器。

本文件负责把 DAG 节点中的 ``tool`` 字段解析成真实可调用的工具函数，
并统一处理参数提取、调用、返回值包装和异常包装。
"""

from __future__ import annotations

import inspect
from typing import Any

from tools.tool_registry import get_tool_spec


class ToolDispatcher:
    """根据 tool registry 调用工具。

    功能：
        接收一个 DAG 节点字典，读取其中的 ``tool`` 名称，并从
        ``tools.tool_registry`` 中找到对应工具。默认优先调用 core 函数；
        如果没有 core 函数，则调用 tool 包装对象。

    输入：
        节点字典，例如：
        ``{"id": "n1", "tool": "read_text_file", "input": {"path": "..."}}``。

    输出：
        一个结构化执行结果字典，包含节点 ID、工具名、执行状态、结果或错误。
    """

    def __init__(self, prefer_core: bool = True):
        """初始化工具分发器。

        参数：
            prefer_core: 是否优先调用 ``ToolSpec.core_func``。为 ``True`` 时，
                dispatcher 会优先使用返回 dict 的 core 函数；为 ``False`` 时，
                优先使用轻封装后的 tool 对象。

        输出：
            无显式返回值。偏好配置会保存到实例中。
        """
        self.prefer_core = prefer_core

    def dispatch(self, node: dict[str, Any]) -> dict[str, Any]:
        """执行一个 DAG 节点对应的工具。

        参数：
            node: DAG 节点字典。必须包含 ``id`` 和 ``tool`` 字段。可选字段：
                ``input``: 字典，作为关键字参数传入工具。
                ``args``: 列表，作为位置参数传入工具。
                ``kwargs``: 字典，作为关键字参数传入工具。

        输入：
            一个待执行节点。工具名称可以是 registry ID、tool wrapper 名，
            或 core 函数名，例如 ``read_text_file``、``read_text_file_tool``、
            ``read_text_file_core``。

        输出：
            成功时返回：
            ``{"status": "success", "node_id": ..., "tool": ..., "result": ...}``
            失败时返回：
            ``{"status": "failed", "node_id": ..., "tool": ..., "error": ...}``

        功能：
            解析节点参数，查找工具函数，执行工具函数，并将异常转换为结构化
            失败结果，避免 worker 主循环被单个工具异常打断。
        """
        node_id = node.get("id")
        tool_name = node.get("tool")

        if not tool_name:
            return {
                "status": "failed",
                "node_id": node_id,
                "tool": None,
                "error": "节点缺少 tool 字段。",
            }

        try:
            spec = get_tool_spec(tool_name)
            func = self._select_callable(spec)
            args, kwargs = self._extract_arguments(node)
            kwargs = self._inject_sandbox_policies(func, node, kwargs)
            result = func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - worker 需要结构化返回任意工具异常
            return {
                "status": "failed",
                "node_id": node_id,
                "tool": tool_name,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }

        return {
            "status": "success",
            "node_id": node_id,
            "tool": tool_name,
            "result": result,
        }

    async def dispatch_async(self, node: dict[str, Any]) -> dict[str, Any]:
        """异步执行一个 DAG 节点对应的工具。

        功能：
            与 ``dispatch`` 的返回结构一致，但会识别 async tool 返回的
            awaitable/coroutine，并通过 ``await`` 取得真实工具结果。
            同步工具仍按普通函数执行。
        """
        node_id = node.get("id")
        tool_name = node.get("tool")

        if not tool_name:
            return {
                "status": "failed",
                "node_id": node_id,
                "tool": None,
                "error": "节点缺少 tool 字段。",
            }

        try:
            spec = get_tool_spec(tool_name)
            func = self._select_callable(spec)
            args, kwargs = self._extract_arguments(node)
            kwargs = self._inject_sandbox_policies(func, node, kwargs)
            result = func(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - worker 需要结构化返回任意工具异常
            return {
                "status": "failed",
                "node_id": node_id,
                "tool": tool_name,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }

        return {
            "status": "success",
            "node_id": node_id,
            "tool": tool_name,
            "result": result,
        }

    def _select_callable(self, spec):
        """从 ``ToolSpec`` 中选择实际调用对象。

        参数：
            spec: 从 tool registry 中查到的 ``ToolSpec``。

        输出：
            返回一个可调用对象。

        功能：
            根据 ``prefer_core`` 决定优先使用 core 函数还是 tool 包装函数。
            如果首选对象不存在，会自动回退到另一个对象。
        """
        if self.prefer_core:
            func = spec.core_func or spec.tool_obj
        else:
            func = spec.tool_obj or spec.core_func

        if func is None:
            raise ValueError(f"tool 没有关联可调用对象: {spec.name}")
        return func

    def _extract_arguments(self, node: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        """从 DAG 节点中提取工具调用参数。

        参数：
            node: DAG 节点字典。

        输入：
            支持三种参数字段：
            ``args`` 作为位置参数列表；
            ``kwargs`` 作为关键字参数字典；
            ``input`` 作为关键字参数字典的简写。

        输出：
            返回 ``(args, kwargs)``，可直接用于 ``func(*args, **kwargs)``。

        功能：
            兼容不同 planner 产出的节点格式。若同时存在 ``input`` 和
            ``kwargs``，两者会合并，``kwargs`` 的优先级更高。
        """
        args = node.get("args") or []
        if not isinstance(args, list):
            raise TypeError("节点 args 必须是 list。")

        input_payload = node.get("input") or {}
        kwargs = node.get("kwargs") or {}

        if not isinstance(input_payload, dict):
            raise TypeError("节点 input 必须是 dict。")
        if not isinstance(kwargs, dict):
            raise TypeError("节点 kwargs 必须是 dict。")

        return args, {**input_payload, **kwargs}

    def _inject_sandbox_policies(
        self,
        func,
        node: dict[str, Any],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Inject sandbox policy sections only when the target callable accepts them."""
        sandbox = node.get("sandbox") or {}
        if not isinstance(sandbox, dict):
            return kwargs

        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            return kwargs

        updated = dict(kwargs)
        if "process_policy" in signature.parameters and "process_policy" not in updated:
            process_policy = sandbox.get("process")
            if isinstance(process_policy, dict):
                updated["process_policy"] = process_policy
        if "network_policy" in signature.parameters and "network_policy" not in updated:
            network_policy = sandbox.get("network")
            if isinstance(network_policy, dict):
                updated["network_policy"] = network_policy
        if "quota_policy" in signature.parameters and "quota_policy" not in updated:
            quota_policy = sandbox.get("quota")
            if isinstance(quota_policy, dict):
                updated["quota_policy"] = quota_policy

        return updated

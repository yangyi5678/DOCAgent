"""智能体上下文管理器。

ContextManager 不是单纯的提示词拼接器，也不是只保存历史消息的记忆模块。
它负责两件事：

1. 维护会话级状态：
   多轮消息、任务摘要、工具执行轨迹、产物引用、用户偏好、长期摘要。

2. 按智能体流水线的不同阶段生成上下文视图：
   目标解析、规划器、工具绑定、执行器、最终回复看到的信息不同。

核心边界：
    session_id: 一段多轮对话。
    task_id: 某一轮用户请求触发的一次 DAG 执行。
    context view: 从同一份会话状态裁剪出来的阶段专用上下文。

当前上下文链路：
    1. start_turn 把用户输入写入 Redis session context。
    2. save 先压缩超窗旧消息，再裁剪 messages/tasks/tool_traces/artifacts。
    3. build_*_context 从同一份 session context 生成阶段专用视图。
    4. build_*_messages 把阶段视图裁剪到字符预算并组装成 LLM messages。
    5. scheduler/worker 执行结果通过 record_tool_result/record_task_result 回写。
    6. 历史过长时由 MemoryManager 生成 summary/memory_facts，下一轮再检索使用。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from memory_manager import MemoryManager, restore_message_order
from redis_infra import RedisClientFactory, RedisContextStore
from skills.context_builder import SkillContextBuilder
from skills.registry import SkillRegistry
from tools.tool_registry import iter_tool_specs
from tools.tool_type import ToolSpec


DEFAULT_MAX_MESSAGES = 20
DEFAULT_MAX_TASKS = 10
DEFAULT_MAX_TOOL_TRACES = 50
DEFAULT_MAX_ARTIFACTS = 30
DEFAULT_PROMPT_MESSAGES = 8
DEFAULT_PROMPT_TASKS = 3
DEFAULT_PROMPT_TRACES = 5
DEFAULT_CONTEXT_BUDGET_CHARS = 12_000
DEFAULT_MEMORY_SUMMARY_CHARS = 4_000
DEFAULT_MEMORY_FACTS = 50


DEFAULT_SYSTEM_PROMPTS: dict[str, str] = {
    "goal_parser": (
        "You are a Goal Parser. Convert user input into structured Goal JSON. "
        "Use conversation context only to resolve references and ambiguity."
    ),
    "planner": (
        "You are an agent planner. Build a capability DAG from the goal, "
        "available capabilities, constraints, and reusable context."
    ),
    "tool_binding": (
        "You are a tool binding module. Choose tools only from the provided "
        "candidate tools and bind arguments according to their schemas."
    ),
    "worker": (
        "You are executing one DAG node. Use only the node input, upstream "
        "results, and explicit execution context."
    ),
    "final_response": (
        "You are preparing the final user-facing response from execution "
        "results. Be concise and faithful to observed tool outputs."
    ),
}


class ContextManager:
    """基于 Redis 的智能体上下文管理器。

    公开 API 分成三组：
        1. 会话生命周期：load/save/start_turn/end_turn/clear。
        2. 阶段视图：build_goal_parser_context/build_planner_context 等。
        3. 轨迹写入：record_tool_result/record_task_result/update_summary。
    """

    def __init__(
        self,
        redis_client=None,
        *,
        postgres_store=None,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_tasks: int = DEFAULT_MAX_TASKS,
        max_tool_traces: int = DEFAULT_MAX_TOOL_TRACES,
        max_artifacts: int = DEFAULT_MAX_ARTIFACTS,
        system_prompts: dict[str, str] | None = None,
        skill_roots: list[str | Path] | None = None,
    ):
        """初始化上下文管理器。

        参数：
            redis_client: 可选 Redis client；不传则使用 RedisClientFactory 创建。
            postgres_store: 可选 PostgreSQL 存储，用于持久化摘要、事实和长期记忆。
            max_messages: Redis session 中最多保留的消息条数。
            max_tasks: Redis session 中最多保留的任务摘要条数。
            max_tool_traces: Redis session 中最多保留的工具执行轨迹条数。
            max_artifacts: Redis session 中最多保留的产物引用条数。
            system_prompts: 可覆盖默认阶段 system prompt 的字典。
            skill_roots: 可选 skill 根目录；未传时从环境变量和本地约定目录发现。

        返回：
            无。初始化 Redis 上下文存储、MemoryManager 和阶段 prompt 配置。

        功能：
            建立 ContextManager 的读写依赖和容量策略，是整个上下文链路入口。
        """
        self.redis = redis_client or RedisClientFactory().create()
        self.store = RedisContextStore(self.redis)
        self.postgres_store = postgres_store
        self.max_messages = max_messages
        self.max_tasks = max_tasks
        self.max_tool_traces = max_tool_traces
        self.max_artifacts = max_artifacts
        self.memory = MemoryManager()
        self.system_prompts = {
            **DEFAULT_SYSTEM_PROMPTS,
            **(system_prompts or {}),
        }
        self.skill_roots = normalize_skill_roots(skill_roots)

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    def load(self, session_id: str) -> dict[str, Any]:
        """读取完整会话状态；不存在时返回标准空结构。

        参数：
            session_id: 要读取的会话 ID。

        返回：
            标准化后的 session context 字典。

        功能：
            从 Redis 读取 ``context:{session_id}`` 快照，并补齐缺失字段。
        """
        context = self.store.load(session_id) or {}
        return self.normalize(session_id, context)

    def save(self, session_id: str, context: dict[str, Any]) -> int:
        """保存完整会话状态。

        参数：
            session_id: 要保存的会话 ID。
            context: 当前完整 session context。

        返回：
            Redis set 的返回值。

        功能：
            保存前先标准化、压缩超窗旧消息、硬裁剪列表字段，再写入 Redis。
        """
        normalized = self.normalize(session_id, context)
        normalized["updated_at"] = utc_now()
        self._compact_history_in_place(normalized, max_messages=self.max_messages)
        self._trim_in_place(normalized)
        return self.store.save(session_id, normalized)

    def clear(self, session_id: str) -> int:
        """清空某个会话的上下文。

        参数：
            session_id: 要清空的会话 ID。

        返回：
            Redis delete 的返回值。

        功能：
            删除 Redis 中对应的 session context 快照。
        """
        return self.store.clear(session_id)

    def start_turn(
        self,
        session_id: str,
        user_input: str,
        *,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """开启一轮用户输入，返回完整 session state。

        参数：
            session_id: 会话 ID。
            user_input: 本轮用户原始输入。
            task_id: 可选任务 ID；不传时自动生成。
            metadata: 可选元数据，会合并到 ``context["metadata"]``。

        返回：
            保存并重新读取后的完整 session context。

        功能：
            递增 turn_index，设置 current_task_id/current_user_input，并把 user
            message 追加到 ``context["messages"]``。

        这里负责更新 session 状态，不负责生成某个阶段的 prompt。阶段专用
        context 由 build_*_context 方法生成。
        """
        context = self.load(session_id)
        context["turn_index"] = int(context.get("turn_index", 0)) + 1
        context["current_task_id"] = task_id or make_task_id(
            session_id,
            context["turn_index"],
        )
        context["current_user_input"] = user_input
        context["metadata"].update(metadata or {})
        context["messages"].append({
            "role": "user",
            "content": user_input,
            "turn_index": context["turn_index"],
            "task_id": context["current_task_id"],
            "created_at": utc_now(),
        })
        self.save(session_id, context)
        return self.load(session_id)

    def end_turn(
        self,
        session_id: str,
        *,
        assistant_message: str | None = None,
        task_result: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """结束一轮对话，可同时记录最终回复和任务摘要。

        参数：
            session_id: 会话 ID。
            assistant_message: 可选助手最终回复。
            task_result: 可选任务摘要。
            task_id: 可选任务 ID；不传则使用 current_task_id。

        返回：
            保存并重新读取后的完整 session context。

        功能：
            把 assistant message 和任务摘要写回 session context，形成下一轮可用历史。
        """
        context = self.load(session_id)
        resolved_task_id = task_id or context.get("current_task_id")

        if task_result is not None and resolved_task_id:
            context = self._append_task_result(
                context,
                resolved_task_id,
                task_result,
            )

        if assistant_message:
            context["messages"].append({
                "role": "assistant",
                "content": assistant_message,
                "turn_index": context.get("turn_index", 0),
                "task_id": resolved_task_id,
                "created_at": utc_now(),
            })

        self.save(session_id, context)
        return self.load(session_id)

    def append_assistant_message(
        self,
        session_id: str,
        content: str,
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """兼容旧接口：把助手最终回复写回上下文。

        参数：
            session_id: 会话 ID。
            content: 助手回复文本。
            task_id: 可选任务 ID。

        返回：
            更新后的完整 session context。

        功能：
            委托 end_turn 追加 assistant message。
        """
        return self.end_turn(
            session_id,
            assistant_message=content,
            task_id=task_id,
        )

    def add_task_result(
        self,
        session_id: str,
        task_id: str,
        result_summary: dict[str, Any],
    ) -> dict[str, Any]:
        """兼容旧接口：记录一轮 DAG 任务的最终摘要。

        参数：
            session_id: 会话 ID。
            task_id: 任务 ID。
            result_summary: 任务摘要。

        返回：
            更新后的完整 session context。

        功能：
            委托 record_task_result 写入任务结果。
        """
        return self.record_task_result(session_id, task_id, result_summary)

    # ------------------------------------------------------------------
    # 摘要与记忆压缩
    # ------------------------------------------------------------------

    def update_summary(
        self,
        session_id: str,
        *,
        summary: str | None = None,
        max_messages_to_summarize: int | None = None,
    ) -> dict[str, Any]:
        """更新长期 summary。

        参数：
            session_id: 会话 ID。
            summary: 可选外部摘要；传入时直接覆盖 context["summary"]。
            max_messages_to_summarize: 未传 summary 时，参与摘要的消息数量上限。

        返回：
            更新后的完整 session context。

        功能：
            手动刷新滚动摘要；有外部 LLM 摘要时直接写入，否则调用 MemoryManager
            基于当前消息、任务和产物生成摘要。

        不在这里强依赖 LLM。外部如果有 LLM 总结结果，可以通过 ``summary``
        传入；否则使用确定性摘要，把旧 summary、消息和任务摘要合并。
        """
        context = self.load(session_id)
        if summary is not None:
            context["summary"] = summary
        else:
            messages = context.get("messages", [])
            selected = messages
            if max_messages_to_summarize is not None:
                selected = messages[:max_messages_to_summarize]
            context["summary"] = self.build_conversation_summary(
                context,
                messages=selected,
            )
        self.save(session_id, context)
        return self.load(session_id)

    def maybe_compact_history(
        self,
        session_id: str,
        *,
        max_messages: int | None = None,
        keep_recent: int | None = None,
    ) -> dict[str, Any]:
        """当历史消息过长时，把旧消息压缩进 summary。

        参数：
            session_id: 会话 ID。
            max_messages: 可选消息窗口阈值；默认使用 self.max_messages。
            keep_recent: 触发压缩时必保留的最近消息数。

        返回：
            更新后的完整 session context；未超过阈值时返回原 context。

        功能：
            主动触发历史压缩：保留最近消息和相关旧消息，把其他旧消息压进
            summary/memory_facts。

        返回更新后的完整 session state。这个方法适合在一轮结束后主动调用；
        ``save`` 也会做一次轻量保护，避免历史被直接截断。
        """
        context = self.load(session_id)
        threshold = max_messages or self.max_messages
        keep = keep_recent or max(1, threshold // 2)
        messages = list(context.get("messages", []))
        if len(messages) <= threshold:
            return context

        self._compact_messages_in_context(
            context,
            max_messages=threshold,
            keep_recent=keep,
        )
        self.save(session_id, context)
        return self.load(session_id)

    def build_conversation_summary(
        self,
        context: dict[str, Any],
        *,
        messages: list[dict[str, Any]] | None = None,
        max_chars: int = 1200,
    ) -> str:
        """根据会话状态生成对话摘要。

        参数：
            context: 当前 session context。
            messages: 可选参与摘要的消息列表；不传则使用 context["messages"]。
            max_chars: 摘要最大字符数。

        返回：
            摘要字符串。

        功能：
            委托 MemoryManager 基于旧 summary、消息、任务、产物和 memory_facts
            生成滚动会话摘要。
        """
        return self.memory.build_session_summary(
            context.get("summary", ""),
            messages if messages is not None else context.get("messages", []),
            context.get("tasks", []),
            context.get("artifacts", []),
            context.get("memory_facts", []),
            max_chars=max_chars,
        )

    def build_memory_context(
        self,
        session_id: str,
        *,
        query: str = "",
        max_messages: int = DEFAULT_PROMPT_MESSAGES,
    ) -> dict[str, Any]:
        """构建当前请求可用的记忆切片。

        参数：
            session_id: 会话 ID。
            query: 当前请求或阶段查询文本。
            max_messages: memory_context 中保留的最近消息数量。

        返回：
            包含 session_summary、recent_messages、relevant_facts、
            long_term_memory 的记忆视图。

        功能：
            从 Redis session 读取 summary/memory_facts/long_term_memory，并交给
            MemoryManager 做相关事实检索。

        这里仍然由 ContextManager 读取会话状态，但具体的事实检索和记忆负载
        形状交给 MemoryManager。
        """
        context = self.load(session_id)
        return self._build_memory_view(context, query=query, max_messages=max_messages)

    # ------------------------------------------------------------------
    # 阶段专用上下文视图
    # ------------------------------------------------------------------

    def build_goal_parser_context(
        self,
        session_id: str,
        user_input: str | None = None,
        *,
        max_messages: int = DEFAULT_PROMPT_MESSAGES,
    ) -> dict[str, Any]:
        """给目标解析阶段使用的上下文。

        参数：
            session_id: 会话 ID。
            user_input: 当前用户输入；不传则使用 context["current_user_input"]。
            max_messages: 最近消息窗口大小，默认 8。

        返回：
            goal_parser 阶段 context view。

        功能：
            从完整 session context 中裁出“理解当前用户意图”所需信息，包括摘要、
            记忆切片、最近消息、最近任务、澄清历史和用户偏好。

        只包含理解当前用户输入所需的信息：最近对话、长期摘要、最近任务结果、
        澄清历史和用户偏好。不包含完整工具结构，避免干扰意图识别。
        """
        context = self.load(session_id)
        query = user_input or context.get("current_user_input", "")
        return {
            "stage": "goal_parser",
            "system_prompt": self.system_prompts["goal_parser"],
            "session": self._session_header(context),
            "user_input": query,
            "conversation_summary": context.get("summary", ""),
            "memory_context": self._build_memory_view(context, query=query, max_messages=max_messages),
            "recent_messages": self._recent_messages(context, max_messages),
            "recent_tasks": self._recent_tasks(context),
            "clarification_history": context.get("clarification_history", []),
            "user_preferences": context.get("metadata", {}).get("user_preferences", {}),
        }

    def build_planner_context(
        self,
        session_id: str,
        goal: dict[str, Any] | None = None,
        *,
        capability_candidates: list[dict[str, Any]] | None = None,
        include_tools: bool = False,
    ) -> dict[str, Any]:
        """给规划器和 DAG 推理使用的上下文。

        参数：
            session_id: 会话 ID。
            goal: Goal IR，来自 goal_parser。
            capability_candidates: 候选 capability 列表。
            include_tools: 是否附带 capability -> tools 的工具能力索引。

        返回：
            planner 阶段 context view。

        功能：
            为规划器提供结构化目标、候选能力、摘要、相关记忆、最近任务、可复用
            产物和用户约束。

        规划器需要结构化目标、历史任务摘要、可复用产物，以及可选的工具能力
        索引。完整工具结构默认不放入，只有需要时打开。
        """
        context = self.load(session_id)
        query = str((goal or {}).get("text", ""))
        planner_context = {
            "stage": "planner",
            "system_prompt": self.system_prompts["planner"],
            "session": self._session_header(context),
            "goal": goal or {},
            "capability_candidates": capability_candidates or [],
            "conversation_summary": context.get("summary", ""),
            "memory_context": self._build_memory_view(context, query=query),
            "recent_tasks": self._recent_tasks(context),
            "reusable_artifacts": self._recent_artifacts(context),
            "user_constraints": self._extract_user_constraints(goal or {}, context),
        }
        skill_bundle = self.build_skill_context_bundle(goal or {}, context=context)
        if skill_bundle.get("matched_skills"):
            planner_context["skill_context_bundle"] = skill_bundle
        if include_tools:
            planner_context["tool_capability_index"] = self.build_tool_capability_index()
        return planner_context

    def build_tool_binding_context(
        self,
        session_id: str,
        *,
        goal: dict[str, Any],
        capability: str,
        candidate_tools: list[ToolSpec] | list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """给 tool binding / rerank 使用的上下文。

        参数：
            session_id: 会话 ID。
            goal: Goal IR。
            capability: 当前要绑定工具的 capability。
            candidate_tools: 可选候选工具列表；不传则按 capability 从 registry 查。

        返回：
            tool_binding 阶段 context view。

        功能：
            只给 LLM 暴露当前 capability 的候选工具、最近任务和可复用产物，避免
            从完整工具表中幻觉工具。

        这个阶段需要 tool registry，但只应该看到当前 capability 的候选工具。
        """
        context = self.load(session_id)
        tools = candidate_tools
        if tools is None:
            tools = self.find_tools_for_capability(capability)

        return {
            "stage": "tool_binding",
            "system_prompt": self.system_prompts["tool_binding"],
            "session": self._session_header(context),
            "goal": goal,
            "capability": capability,
            "candidate_tools": self.serialize_tools(tools),
            "skill_context_bundle": self.build_skill_context_bundle(goal, context=context),
            "recent_tasks": self._recent_tasks(context),
            "reusable_artifacts": self._recent_artifacts(context),
        }

    def build_argument_binding_context(
        self,
        session_id: str,
        *,
        goal: dict[str, Any],
        capability: str,
        tool: ToolSpec | dict[str, Any],
    ) -> dict[str, Any]:
        """给参数绑定使用的上下文。

        参数：
            session_id: 会话 ID。
            goal: Goal IR。
            capability: 当前 capability。
            tool: 已选中的工具定义或序列化工具字典。

        返回：
            argument_binding 阶段 context view。

        功能：
            给参数绑定阶段提供单个工具 schema、goal 约束、最近任务和可复用产物。

        它比 tool binding 更窄：只包含一个已选 tool 的 schema、goal 约束、
        可复用 artifact 和最近任务摘要。
        """
        context = self.load(session_id)
        return {
            "stage": "argument_binding",
            "system_prompt": self.system_prompts["tool_binding"],
            "session": self._session_header(context),
            "goal": goal,
            "capability": capability,
            "tool": self.serialize_tool(tool),
            "skill_context_bundle": self.build_skill_context_bundle(goal, context=context),
            "goal_constraints": goal.get("constraints", {}),
            "reusable_artifacts": self._recent_artifacts(context),
            "recent_tasks": self._recent_tasks(context),
        }

    def build_skill_context_bundle(
        self,
        goal: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
        limit: int = 2,
    ) -> dict[str, Any]:
        """选择并加载当前 goal 相关的 skill context。

        参数：
            goal: Goal IR。
            context: 可选完整 session context，用于读取 metadata.skill_roots。
            limit: 最多加载的 skill 数。

        返回：
            可 JSON 序列化的 skill bundle；没有匹配 skill 时返回空结构。

        功能：
            这是 skill 链路进入框架的 ContextManager 入口。它只返回上下文和
            planner hints，不执行工具，不直接生成 DAG。
        """
        roots = self._resolve_skill_roots(context or {})
        if not roots:
            return empty_skill_context_bundle()
        try:
            registry = SkillRegistry(roots)
            registry.scan()
            bundle = SkillContextBuilder(registry=registry).build_for_goal(
                goal,
                limit=limit,
            )
            return bundle.to_dict()
        except Exception as exc:  # noqa: BLE001 - skill 不应阻断基础 planner
            return {
                **empty_skill_context_bundle(),
                "skill_context_error": str(exc),
            }

    def _resolve_skill_roots(self, context: dict[str, Any]) -> list[Path]:
        metadata = context.get("metadata") or {}
        metadata_roots = metadata.get("skill_roots") or metadata.get("skill_root")
        roots = [
            *self.skill_roots,
            *normalize_skill_roots(metadata_roots),
        ]
        return dedupe_paths([root for root in roots if root.exists()])

    def build_worker_context(
        self,
        session_id: str,
        *,
        task_id: str,
        node: dict[str, Any],
        upstream_results: dict[str, Any] | None = None,
        retry_state: dict[str, Any] | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        """给 worker / tool 执行阶段使用的上下文。

        参数：
            session_id: 会话 ID。
            task_id: 当前 DAG 任务 ID。
            node: 当前待执行节点。
            upstream_results: 上游节点结果。
            retry_state: 当前节点重试状态。
            worker_id: 可选 worker 标识。

        返回：
            worker 阶段 context view。

        功能：
            只提供执行当前节点需要的信息，不附带完整消息历史。

        Worker 通常不用 LLM。这里不放完整消息历史和 system prompt，只保留执行
        当前 node 必要的信息。
        """
        context = self.load(session_id)
        return {
            "stage": "worker",
            "session": self._session_header(context),
            "task_id": task_id,
            "worker_id": worker_id,
            "node": self._compact_node(node),
            "upstream_results": upstream_results or {},
            "retry_state": retry_state or {},
            "artifacts": self._recent_artifacts(context),
        }

    def build_final_response_context(
        self,
        session_id: str,
        *,
        task_id: str | None = None,
        runtime_state: dict[str, Any] | None = None,
        final_results: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """给最终回答生成阶段使用的上下文。

        参数：
            session_id: 会话 ID。
            task_id: 可选任务 ID；不传则使用 current_task_id。
            runtime_state: Scheduler 运行时状态。
            final_results: 最终执行结果。

        返回：
            final_response 阶段 context view。

        功能：
            汇总用户输入、摘要、记忆切片、最近消息、任务摘要、tool traces 和最终
            结果，供最终回复生成使用。
        """
        context = self.load(session_id)
        resolved_task_id = task_id or context.get("current_task_id")
        query = context.get("current_user_input", "")
        return {
            "stage": "final_response",
            "system_prompt": self.system_prompts["final_response"],
            "session": self._session_header(context),
            "user_input": context.get("current_user_input", ""),
            "conversation_summary": context.get("summary", ""),
            "memory_context": self._build_memory_view(context, query=query),
            "recent_messages": self._recent_messages(context, DEFAULT_PROMPT_MESSAGES),
            "task_id": resolved_task_id,
            "task": self._find_task(context, resolved_task_id),
            "tool_traces": self._recent_tool_traces(context, task_id=resolved_task_id),
            "runtime_state": self._compact_runtime_state(runtime_state or {}),
            "final_results": final_results or {},
        }

    def build_prompt_context(self, session_id: str, *, max_messages: int = 8) -> dict[str, Any]:
        """兼容旧接口：返回适合普通大模型提示词的轻量上下文。

        参数：
            session_id: 会话 ID。
            max_messages: 最近消息数量。

        返回：
            轻量 prompt context 字典。

        功能：
            为旧调用方提供 summary、recent_messages、recent_tasks 和 metadata。
        """
        context = self.load(session_id)
        return {
            "session_id": context["session_id"],
            "turn_index": context["turn_index"],
            "summary": context.get("summary", ""),
            "recent_messages": self._recent_messages(context, max_messages),
            "recent_tasks": self._recent_tasks(context),
            "metadata": context.get("metadata", {}),
        }

    def build_stage_messages(
        self,
        stage_context: dict[str, Any],
        *,
        max_chars: int | None = None,
    ) -> list[dict[str, str]]:
        """按阶段把 context view 拼装成 LLM messages。

        作用：
            统一入口，根据 ``stage_context["stage"]`` 分发到具体阶段的
            prompt assembler，避免 goalparser/planner 到处手写 system prompt
            和 JSON payload。

        参数：
            stage_context: ``build_goal_parser_context``、
                ``build_planner_context`` 等函数生成的阶段上下文。
            max_chars: 可选字符预算；传入时会在拼装前裁剪 payload。

        返回：
            OpenAI-compatible messages：
            ``[{"role": "system", ...}, {"role": "user", ...}]``。

        核心计算逻辑：
            读取 ``stage`` 字段，选择对应 ``build_*_messages`` 方法；未知阶段
            回退到通用 ``build_prompt_messages``。
        """
        stage = stage_context.get("stage")
        if stage == "goal_parser":
            return self.build_goal_parser_messages(stage_context, max_chars=max_chars)
        if stage == "planner":
            return self.build_planner_messages(stage_context, max_chars=max_chars)
        if stage == "tool_binding":
            return self.build_tool_binding_messages(stage_context, max_chars=max_chars)
        if stage == "argument_binding":
            return self.build_argument_binding_messages(stage_context, max_chars=max_chars)
        if stage == "final_response":
            return self.build_final_response_messages(stage_context, max_chars=max_chars)
        return self.build_prompt_messages(stage_context, max_chars=max_chars)

    def build_goal_parser_messages(
        self,
        context: dict[str, Any],
        *,
        max_chars: int | None = 6_000,
    ) -> list[dict[str, str]]:
        """构造 Goal Parser 阶段的 LLM messages。

        作用：
            把用户当前输入和少量多轮上下文拼成 prompt，让 LLM 只做 Goal IR
            解析，不做任务规划、不编造工具。

        参数：
            context: ``build_goal_parser_context`` 的返回值，通常包含
                ``user_input``、``conversation_summary``、``recent_messages``、
                ``recent_tasks``、``clarification_history``、``user_preferences``。
            max_chars: payload 字符预算，默认 6000。

        返回：
            LLM messages，要求模型返回符合 ``output_shape`` 的 JSON。

        核心计算逻辑：
            组装 goal parser 专用 system prompt；payload 只保留意图解析所需字段，
            并附带输出形状，最后交给 ``_build_json_messages`` 做预算裁剪和 JSON
            序列化。
        """
        system_prompt = (
            "You are a Goal Parser. Convert user input into structured Goal JSON. "
            "Do not plan execution steps. Do not hallucinate tools. "
            "Use conversation context only to resolve references and ambiguity. "
            "Return valid JSON only."
        )
        payload = {
            "stage": "goal_parser",
            "session": context.get("session", {}),
            "user_input": context.get("user_input", ""),
            "conversation_summary": context.get("conversation_summary", ""),
            "memory_context": context.get("memory_context", {}),
            "recent_messages": context.get("recent_messages", []),
            "recent_tasks": context.get("recent_tasks", []),
            "clarification_history": context.get("clarification_history", []),
            "user_preferences": context.get("user_preferences", {}),
            "output_shape": {
                "intents": ["qa"],
                "entities": [],
                "constraints": {},
                "completion_criteria": [],
                "ambiguity": {
                    "is_ambiguous": False,
                    "reasons": [],
                    "clarification_questions": [],
                },
                "requires_clarification": False,
                "intent_scores": {"qa": 0.9},
            },
        }
        return self._build_json_messages(system_prompt, payload, max_chars=max_chars)

    def build_planner_messages(
        self,
        context: dict[str, Any],
        *,
        max_chars: int | None = 12_000,
    ) -> list[dict[str, str]]:
        """构造 Planner / LLM DAG reasoning 阶段的 LLM messages。

        作用：
            让 LLM 根据 Goal IR、候选 capability、历史任务和 artifact 推理
            Capability DAG。

        参数：
            context: ``build_planner_context`` 的返回值，通常包含 ``goal``、
                ``capability_candidates``、``recent_tasks``、
                ``reusable_artifacts``、``tool_capability_index``。
            max_chars: payload 字符预算，默认 12000。

        返回：
            LLM messages，要求模型返回 capability DAG JSON。

        核心计算逻辑：
            system prompt 强约束“只能使用候选 capability id”；payload 加入
            ``output_shape``，明确需要 ``nodes``、``edges``、``parallel_groups``
            和 ``reason``。
        """
        system_prompt = (
            "You are a capability DAG reasoning module for an agent planner. "
            "Use only provided capability candidate ids and capabilities. "
            "Infer dependency edges; do not simply sort unless dependencies are sequential. "
            "Do not invent capabilities. Return valid JSON only."
        )
        payload = {
            "stage": "planner",
            "session": context.get("session", {}),
            "goal": context.get("goal", {}),
            "capability_candidates": context.get("capability_candidates", []),
            "conversation_summary": context.get("conversation_summary", ""),
            "memory_context": context.get("memory_context", {}),
            "recent_tasks": context.get("recent_tasks", []),
            "reusable_artifacts": context.get("reusable_artifacts", []),
            "user_constraints": context.get("user_constraints", {}),
            "tool_capability_index": context.get("tool_capability_index", {}),
            "output_shape": {
                "nodes": [
                    {
                        "id": "c1",
                        "capability": "database.read",
                        "reason": "why this capability is needed",
                    }
                ],
                "edges": [
                    {
                        "from": "c1",
                        "to": "c2",
                        "type": "data_dependency",
                        "reason": "why c2 depends on c1",
                    }
                ],
                "parallel_groups": [],
                "reason": "overall DAG reasoning",
            },
        }
        return self._build_json_messages(system_prompt, payload, max_chars=max_chars)

    def build_tool_binding_messages(
        self,
        context: dict[str, Any],
        *,
        max_chars: int | None = 8_000,
    ) -> list[dict[str, str]]:
        """构造 Tool Binding / Tool Rerank 阶段的 LLM messages。

        作用：
            在多个候选工具都能实现同一 capability 时，让 LLM 从候选列表中
            选择最合适的工具。

        参数：
            context: ``build_tool_binding_context`` 的返回值，通常包含 ``goal``、
                ``capability``、``candidate_tools``、``recent_tasks``、
                ``reusable_artifacts``。
            max_chars: payload 字符预算，默认 8000。

        返回：
            LLM messages，要求模型返回 ``{"tool": "tool_name"}``。

        核心计算逻辑：
            system prompt 明确禁止发明工具；payload 只暴露当前 capability 的
            candidate tools，而不是整个 registry。
        """
        system_prompt = (
            "You are a tool reranker. Choose exactly one tool name from candidate_tools. "
            "Do not invent tools. Prefer the tool that best matches the goal, capability, "
            "schemas, and reusable artifacts. Return valid JSON only: {\"tool\":\"tool_name\"}."
        )
        payload = {
            "stage": "tool_binding",
            "session": context.get("session", {}),
            "goal": context.get("goal", {}),
            "capability": context.get("capability"),
            "candidate_tools": context.get("candidate_tools", []),
            "recent_tasks": context.get("recent_tasks", []),
            "reusable_artifacts": context.get("reusable_artifacts", []),
            "output_shape": {"tool": "tool_name"},
        }
        return self._build_json_messages(system_prompt, payload, max_chars=max_chars)

    def build_argument_binding_messages(
        self,
        context: dict[str, Any],
        *,
        max_chars: int | None = 10_000,
    ) -> list[dict[str, str]]:
        """构造 Argument Binding 阶段的 LLM messages。

        作用：
            根据 Goal IR、已选工具 schema、历史 artifact 和约束生成工具调用
            ``input`` 参数。

        参数：
            context: ``build_argument_binding_context`` 的返回值，通常包含 ``goal``、
                ``capability``、``tool``、``goal_constraints``、
                ``reusable_artifacts``、``recent_tasks``。
            max_chars: payload 字符预算，默认 10000。

        返回：
            LLM messages，要求模型返回 ``{"input": {...}}``。

        核心计算逻辑：
            system prompt 要求严格遵守 ``tool.input_schema``，禁止编造缺失值；
            当用户引用“上一次结果/刚才文件”时，可使用 ``reusable_artifacts``。
        """
        system_prompt = (
            "You are an argument binding module for an agent planner. "
            "Bind tool arguments according to tool.input_schema. "
            "Do not invent values that are not implied by the goal or context. "
            "Use reusable_artifacts when the user refers to previous outputs. "
            "Return valid JSON only: {\"input\": {...}}."
        )
        payload = {
            "stage": "argument_binding",
            "session": context.get("session", {}),
            "goal": context.get("goal", {}),
            "capability": context.get("capability"),
            "tool": context.get("tool", {}),
            "goal_constraints": context.get("goal_constraints", {}),
            "reusable_artifacts": context.get("reusable_artifacts", []),
            "recent_tasks": context.get("recent_tasks", []),
            "output_shape": {"input": {}},
        }
        return self._build_json_messages(system_prompt, payload, max_chars=max_chars)

    def build_final_response_messages(
        self,
        context: dict[str, Any],
        *,
        max_chars: int | None = 12_000,
    ) -> list[dict[str, str]]:
        """构造 Final Response 阶段的 LLM messages。

        作用：
            根据任务执行状态、tool traces、产物引用和最终结果生成面向用户的
            最终回答。

        参数：
            context: ``build_final_response_context`` 的返回值，通常包含
                ``user_input``、``task``、``tool_traces``、``runtime_state``、
                ``final_results``。
            max_chars: payload 字符预算，默认 12000。

        返回：
            LLM messages，最终回答阶段通常返回自然语言，不强制 JSON。

        核心计算逻辑：
            system prompt 约束模型只能基于实际结果回答；payload 保留执行结果、
            失败信息、artifact 引用和必要对话上下文。
        """
        system_prompt = (
            "You are preparing the final user-facing response from execution results. "
            "Be concise and faithful. Report successful outputs and failures. "
            "Do not claim actions that did not happen. Mention artifact paths or URIs when useful."
        )
        payload = {
            "stage": "final_response",
            "session": context.get("session", {}),
            "user_input": context.get("user_input", ""),
            "conversation_summary": context.get("conversation_summary", ""),
            "memory_context": context.get("memory_context", {}),
            "recent_messages": context.get("recent_messages", []),
            "task_id": context.get("task_id"),
            "task": context.get("task"),
            "tool_traces": context.get("tool_traces", []),
            "runtime_state": context.get("runtime_state", {}),
            "final_results": context.get("final_results", {}),
        }
        return self._build_json_messages(system_prompt, payload, max_chars=max_chars)

    def build_prompt_messages(
        self,
        stage_context: dict[str, Any],
        *,
        user_content_key: str = "user_input",
        max_chars: int | None = None,
    ) -> list[dict[str, str]]:
        """把某个 stage context 转成 LLM messages。

        参数：
            stage_context: 阶段 context view。
            user_content_key: 需要额外映射为 current_user_input 的字段名。
            max_chars: 可选字符预算。

        返回：
            OpenAI-compatible messages 列表。

        功能：
            通用 stage context 拼装入口，适合没有专用 assembler 的阶段。

        只有确实要调用 LLM 的阶段才需要这个方法。worker/scheduler 不需要。
        """
        prepared_context = stage_context
        if max_chars is not None:
            prepared_context = self.trim_context_to_budget(
                stage_context,
                max_chars=max_chars,
            )

        system_prompt = prepared_context.get("system_prompt", "")
        user_content = {
            key: value
            for key, value in prepared_context.items()
            if key != "system_prompt"
        }
        if user_content_key in prepared_context:
            user_content["current_user_input"] = prepared_context[user_content_key]

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json_dumps(user_content)},
        ]

    def _build_json_messages(
        self,
        system_prompt: str,
        payload: dict[str, Any],
        *,
        max_chars: int | None = None,
    ) -> list[dict[str, str]]:
        """把 system prompt 和 JSON payload 拼成 LLM messages。

        作用：
            作为所有阶段专用 prompt assembler 的底层公共函数，统一处理预算
            裁剪和 JSON 序列化。

        参数：
            system_prompt: 当前阶段的 system prompt。
            payload: 当前阶段要给模型看的结构化上下文。
            max_chars: 可选 payload 字符预算；传入时先调用
                ``trim_context_to_budget``。

        返回：
            OpenAI-compatible messages 列表。

        核心计算逻辑：
            如果设置预算则先对 payload 做阶段感知裁剪；随后生成一条 system
            message 和一条 user message，user content 是 JSON 字符串。
        """
        prepared_payload = payload
        if max_chars is not None:
            prepared_payload = self.trim_context_to_budget(
                payload,
                max_chars=max_chars,
            )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json_dumps(prepared_payload)},
        ]

    def estimate_context_size(self, context: Any) -> int:
        """估算 context 序列化后的字符长度。

        参数：
            context: 任意 JSON-like 上下文值。

        返回：
            序列化后的字符长度。

        功能：
            给 trim_context_to_budget 提供轻量预算估算。

        这里用字符数作为轻量 token budget 近似值，避免引入 tokenizer 依赖。
        """
        return len(json_dumps(context))

    def trim_context_to_budget(
        self,
        context: Any,
        *,
        max_chars: int = DEFAULT_CONTEXT_BUDGET_CHARS,
    ) -> Any:
        """把 context 裁剪到指定字符预算内。

        参数：
            context: 任意 JSON-like 上下文值。
            max_chars: 目标字符预算。

        返回：
            裁剪后的 context。

        功能：
            先做阶段感知预裁剪，再递归裁剪超预算字段。

        保留结构优先于完整内容；字符串会截断，列表会保留尾部较新的元素，
        dict 会优先保留 session/stage/goal/system_prompt 等关键字段。
        """
        prepared = self._prepare_context_for_budget(context, max_chars=max_chars)
        if self.estimate_context_size(prepared) <= max_chars:
            return prepared
        return trim_value_to_budget(prepared, max_chars=max_chars)

    def select_relevant_messages(
        self,
        messages: list[dict[str, Any]],
        query: str,
        *,
        limit: int = DEFAULT_PROMPT_MESSAGES,
    ) -> list[dict[str, Any]]:
        """根据 query 从消息历史中选择相关消息。

        参数：
            messages: 候选消息列表。
            query: 当前查询文本。
            limit: 最多返回消息条数。

        返回：
            相关消息列表，按原始时间顺序排列。

        功能：
            通过关键词重叠从旧消息中找出与当前请求相关的上下文。

        采用确定性关键词重叠评分；分数相同时保留较新的消息。
        """
        scored = [
            (
                score_text_relevance(
                    query,
                    f"{message.get('role', '')} {message.get('content', '')}",
                ),
                index,
                message,
            )
            for index, message in enumerate(messages)
        ]
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected = [message for score, _, message in scored if score > 0][:limit]
        if not selected:
            selected = list(messages)[-limit:]
        return sorted(selected, key=lambda message: messages.index(message))

    def select_relevant_artifacts(
        self,
        artifacts: list[dict[str, Any]],
        query: str,
        *,
        limit: int = DEFAULT_PROMPT_TASKS,
    ) -> list[dict[str, Any]]:
        """根据查询文本从产物列表中选择相关项。

        参数：
            artifacts: 候选产物列表。
            query: 当前查询文本。
            limit: 最多返回产物数量。

        返回：
            相关 artifact 列表，按原始顺序排列。

        功能：
            给 planner/argument_binding 从历史产物中筛出当前请求可能引用的产物。
        """
        scored = [
            (
                score_text_relevance(query, json_dumps(artifact)),
                index,
                artifact,
            )
            for index, artifact in enumerate(artifacts)
        ]
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected = [artifact for score, _, artifact in scored if score > 0][:limit]
        if not selected:
            selected = list(artifacts)[-limit:]
        return sorted(selected, key=lambda artifact: artifacts.index(artifact))

    # ------------------------------------------------------------------
    # 工具注册表视图
    # ------------------------------------------------------------------

    def build_tool_registry_context(
        self,
        *,
        capabilities: list[str] | None = None,
        categories: list[str] | None = None,
        include_disabled: bool = False,
    ) -> list[dict[str, Any]]:
        """读取工具注册表，并按能力或类别过滤。

        参数：
            capabilities: 可选 capability 过滤列表。
            categories: 可选工具类别过滤列表。
            include_disabled: 是否包含 disabled 工具。

        返回：
            序列化后的工具列表。

        功能：
            为上下文或工具选择阶段提供受控工具 registry 视图。
        """
        tools: list[ToolSpec] = []
        for spec in iter_tool_specs(enabled_only=not include_disabled):
            if capabilities and not set(capabilities).intersection(spec.capabilities):
                continue
            if categories and str(spec.category) not in categories and spec.category.value not in categories:
                continue
            tools.append(spec)
        return self.serialize_tools(tools)

    def build_tool_capability_index(self) -> dict[str, list[dict[str, Any]]]:
        """构建能力到工具的摘要索引，供规划器使用。

        参数：
            无。

        返回：
            capability -> tool summary list 的字典。

        功能：
            给 planner 一个能力维度的工具概览，避免传完整 ToolSpec。
        """
        index: dict[str, list[dict[str, Any]]] = {}
        for spec in iter_tool_specs(enabled_only=True):
            tool_summary = {
                "name": spec.name,
                "description": spec.description,
                "category": spec.category.value,
                "source": spec.source.value,
                "tags": spec.tags,
            }
            for capability in spec.capabilities:
                index.setdefault(capability, []).append(tool_summary)
        return index

    def find_tools_for_capability(self, capability: str) -> list[ToolSpec]:
        """从注册表中找出声明支持某个能力的工具。

        参数：
            capability: 要匹配的 capability ID。

        返回：
            支持该 capability 的 ToolSpec 列表。

        功能：
            为 tool_binding 阶段准备候选工具集合。
        """
        return [
            spec
            for spec in iter_tool_specs(enabled_only=True)
            if capability in spec.capabilities
        ]

    def serialize_tools(
        self,
        tools: list[ToolSpec] | list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """序列化多个工具，保证上下文可 JSON 化。

        参数：
            tools: ToolSpec 或已序列化工具字典列表。

        返回：
            可 JSON 序列化的工具字典列表。

        功能：
            统一工具结构，供 LLM payload 和 context view 使用。
        """
        return [self.serialize_tool(tool) for tool in tools]

    def serialize_tool(self, tool: ToolSpec | dict[str, Any]) -> dict[str, Any]:
        """把 ToolSpec 或已有字典转成上下文里的稳定结构。

        参数：
            tool: ToolSpec 实例或工具字典。

        返回：
            工具的稳定字典结构。

        功能：
            提取 name/description/schema/capabilities 等字段，避免直接暴露复杂对象。
        """
        if isinstance(tool, dict):
            return dict(tool)

        return {
            "name": tool.name,
            "description": tool.description,
            "source": tool.source.value,
            "category": tool.category.value,
            "enabled": tool.enabled,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "capabilities": tool.capabilities,
            "tool_name": tool.tool_name,
            "core_name": tool.core_name,
            "tags": tool.tags,
            "config": tool.config,
        }

    # ------------------------------------------------------------------
    # 执行轨迹与结果写入
    # ------------------------------------------------------------------

    def record_tool_result(
        self,
        session_id: str,
        *,
        task_id: str,
        node_id: str | None,
        tool_name: str | None,
        status: str,
        input_payload: dict[str, Any] | None = None,
        output_payload: dict[str, Any] | None = None,
        error: str | None = None,
        worker_id: str | None = None,
        attempt: int | None = None,
    ) -> dict[str, Any]:
        """记录一次工具或节点执行结果。

        参数：
            session_id: 会话 ID。
            task_id: 当前任务 ID。
            node_id: 节点 ID。
            tool_name: 工具名。
            status: 执行状态。
            input_payload: 工具输入。
            output_payload: 工具输出。
            error: 可选错误信息。
            worker_id: 可选 worker 标识。
            attempt: 可选执行尝试次数。

        返回：
            更新后的完整 session context。

        功能：
            把工具执行事件压缩成 tool_trace，提取产物引用，并保存回 Redis context。
        """
        context = self.load(session_id)
        trace = self.memory.build_tool_event(
            task_id=task_id,
            node_id=node_id,
            tool_name=tool_name,
            status=status,
            input_payload=input_payload,
            output_payload=output_payload,
            error=error,
            worker_id=worker_id,
            attempt=attempt,
        )
        context["tool_traces"].append(trace)
        self._extract_artifacts_from_result(
            context,
            task_id,
            node_id,
            output_payload or {},
            tool_name=tool_name,
        )
        self.save(session_id, context)
        return self.load(session_id)

    def record_task_result(
        self,
        session_id: str,
        task_id: str,
        result_summary: dict[str, Any],
    ) -> dict[str, Any]:
        """记录一轮 DAG 任务的最终摘要。

        参数：
            session_id: 会话 ID。
            task_id: 任务 ID。
            result_summary: 任务级摘要。

        返回：
            更新后的完整 session context。

        功能：
            把本轮任务结果写入 tasks，并从任务结果中提取 artifact 引用。
        """
        context = self.load(session_id)
        context = self._append_task_result(context, task_id, result_summary)
        self._extract_artifacts_from_result(
            context,
            task_id,
            None,
            result_summary,
            tool_name=None,
        )
        self.save(session_id, context)
        return self.load(session_id)

    def get_recent_artifacts(
        self,
        session_id: str,
        *,
        limit: int = DEFAULT_PROMPT_TASKS,
        kind: str | None = None,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        """读取最近产物，可按类型或查询文本过滤。

        参数：
            session_id: 会话 ID。
            limit: 最多返回数量。
            kind: 可选 artifact 类型过滤。
            query: 可选相关性查询文本。

        返回：
            最近或相关 artifact 列表。

        功能：
            给外部调用方读取 session 中可复用的文件、URL、数据库等产物。
        """
        context = self.load(session_id)
        artifacts = list(context.get("artifacts", []))
        if kind:
            artifacts = [artifact for artifact in artifacts if artifact.get("kind") == kind]
        if query:
            return self.select_relevant_artifacts(artifacts, query, limit=limit)
        return artifacts[-limit:]

    def get_last_task(
        self,
        session_id: str,
        *,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        """读取最近一次任务摘要，可按状态过滤。

        参数：
            session_id: 会话 ID。
            status: 可选任务状态过滤。

        返回：
            最近匹配任务摘要；找不到返回 None。

        功能：
            支持“上一次任务/最近成功任务”这类多轮引用。
        """
        context = self.load(session_id)
        for task in reversed(context.get("tasks", [])):
            summary = task.get("summary", {})
            if status is None or summary.get("status") == status or task.get("status") == status:
                return task
        return None

    def get_task_trace(
        self,
        session_id: str,
        task_id: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """读取某个任务 ID 下的工具执行轨迹。

        参数：
            session_id: 会话 ID。
            task_id: 任务 ID。
            limit: 可选返回数量上限。

        返回：
            该任务下的 tool trace 列表。

        功能：
            给调试、最终回答或外部 API 查看某轮任务执行过程。
        """
        context = self.load(session_id)
        traces = [
            trace
            for trace in context.get("tool_traces", [])
            if trace.get("task_id") == task_id
        ]
        if limit is not None:
            return traces[-limit:]
        return traces

    def get_last_successful_result(
        self,
        session_id: str,
        *,
        capability: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any] | None:
        """读取最近一次成功 tool trace。

        参数：
            session_id: 会话 ID。
            capability: 可选 capability 过滤。
            tool_name: 可选工具名过滤。

        返回：
            最近匹配的成功 tool trace；找不到返回 None。

        功能：
            支持“复用上次成功结果”的多轮上下文读取。

        如果提供 capability/tool_name，会按对应字段过滤。当前 trace 主要保存
        tool_name，capability 过滤会匹配 trace 中可能存在的 capability 字段。
        """
        context = self.load(session_id)
        for trace in reversed(context.get("tool_traces", [])):
            if trace.get("status") != "success":
                continue
            if tool_name and trace.get("tool_name") != tool_name:
                continue
            if capability and trace.get("capability") != capability:
                continue
            return trace
        return None

    # ------------------------------------------------------------------
    # 标准化与私有辅助方法
    # ------------------------------------------------------------------

    def normalize(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        """补齐会话状态字段，保证下游拿到稳定结构。

        参数：
            session_id: 会话 ID。
            context: 从 Redis 读取或调用方传入的原始 context。

        返回：
            补齐默认字段后的新 context 字典。

        功能：
            保证下游始终能安全读取 messages/tasks/tool_traces/artifacts 等字段。
        """
        normalized = dict(context)
        normalized.setdefault("session_id", session_id)
        normalized.setdefault("turn_index", 0)
        normalized.setdefault("current_task_id", None)
        normalized.setdefault("current_user_input", "")
        normalized.setdefault("summary", "")
        normalized.setdefault("messages", [])
        normalized.setdefault("tasks", [])
        normalized.setdefault("tool_traces", [])
        normalized.setdefault("artifacts", [])
        normalized.setdefault("memory_facts", [])
        normalized.setdefault("clarification_history", [])
        normalized.setdefault("metadata", {})
        normalized.setdefault("context_stats", {})
        normalized.setdefault("created_at", utc_now())
        normalized.setdefault("updated_at", normalized["created_at"])
        return normalized

    def _trim_in_place(self, context: dict[str, Any]) -> None:
        """按容量上限原地裁剪 Redis session context。

        参数：
            context: 已标准化的 session context，会被原地修改。

        返回：
            None。函数直接修改 ``context``。

        功能：
            对 messages/tasks/tool_traces/artifacts 做硬上限裁剪，只保留列表尾部
            最近记录，防止 Redis 中的滚动上下文无限增长。
        """
        context["messages"] = list(context.get("messages", []))[-self.max_messages:]
        context["tasks"] = list(context.get("tasks", []))[-self.max_tasks:]
        context["tool_traces"] = list(context.get("tool_traces", []))[-self.max_tool_traces:]
        context["artifacts"] = list(context.get("artifacts", []))[-self.max_artifacts:]

    def _compact_history_in_place(
        self,
        context: dict[str, Any],
        *,
        max_messages: int,
    ) -> None:
        """在裁剪消息前，把超出的旧消息合并进摘要。

        参数：
            context: 当前 session context，会被原地修改。
            max_messages: 消息窗口阈值。

        返回：
            None。

        功能：
            调用 _compact_messages_in_context，默认保留最近 max_messages/2 条消息，
            其余名额留给相关旧消息。
        """
        self._compact_messages_in_context(
            context,
            max_messages=max_messages,
            keep_recent=max(1, max_messages // 2),
        )

    def _compact_messages_in_context(
        self,
        context: dict[str, Any],
        *,
        max_messages: int,
        keep_recent: int,
    ) -> None:
        """高级消息压缩：摘要旧消息，保留最近消息和少量相关旧消息。

        参数：
            context: 当前 session context，会被原地修改。
            max_messages: 压缩后 messages 的目标上限。
            keep_recent: 必保留的最近消息数量。

        返回：
            None。

        功能：
            当 messages 超过阈值时，先保留最近消息，再从旧消息中检索相关项；
            未保留旧消息会进入 MemoryManager，生成 summary/memory_facts。
        """
        messages = list(context.get("messages", []))
        if len(messages) <= max_messages:
            return

        recent_messages = messages[-keep_recent:]
        old_messages = messages[:-keep_recent]
        query = context.get("current_user_input", "")
        relevant_old = self.select_relevant_messages(
            old_messages,
            query,
            limit=max(1, max_messages - keep_recent),
        ) if query else []
        relevant_ids = {id(message) for message in relevant_old}
        compacted_messages = [
            message for message in old_messages if id(message) not in relevant_ids
        ]

        memory_update = self.memory.build_memory_update(
            old_summary=context.get("summary", ""),
            existing_facts=context.get("memory_facts", []),
            messages=compacted_messages,
            tasks=context.get("tasks", []),
            artifacts=context.get("artifacts", []),
            fact_limit=DEFAULT_MEMORY_FACTS,
            summary_max_chars=DEFAULT_MEMORY_SUMMARY_CHARS,
        )
        context["summary"] = memory_update["session_summary"]
        context["memory_facts"] = memory_update["memory_facts"]
        context["long_term_memory_candidates"] = memory_update["long_term_candidates"]
        context["messages"] = restore_message_order(messages, [*relevant_old, *recent_messages])
        context["context_stats"] = {
            **context.get("context_stats", {}),
            "last_compacted_at": utc_now(),
            "last_compacted_message_count": len(compacted_messages),
            "retained_relevant_old_messages": len(relevant_old),
            "retained_recent_messages": len(recent_messages),
            "last_new_memory_fact_count": len(memory_update["new_facts"]),
            "last_long_term_candidate_count": len(memory_update["long_term_candidates"]),
        }
        self._persist_memory_update(context, memory_update)

    def _prepare_context_for_budget(
        self,
        context: Any,
        *,
        max_chars: int,
    ) -> Any:
        """按阶段先做相关性筛选，再进入递归预算裁剪。

        参数：
            context: 阶段 context view 或任意 JSON-like 值。
            max_chars: 目标字符预算。

        返回：
            阶段感知预裁剪后的 context。

        功能：
            根据 stage 限制 recent_messages/artifacts/tool_traces 数量，并在预算较小时
            压缩 tool_capability_index。
        """
        if not isinstance(context, dict):
            return context

        prepared = dict(context)
        query = extract_budget_query(prepared)
        stage = str(prepared.get("stage", ""))

        message_limits = {
            "goal_parser": 8,
            "planner": 6,
            "tool_binding": 4,
            "argument_binding": 4,
            "worker": 0,
            "final_response": 8,
        }
        artifact_limits = {
            "goal_parser": 2,
            "planner": 6,
            "tool_binding": 4,
            "argument_binding": 6,
            "worker": 6,
            "final_response": 6,
        }
        trace_limits = {
            "final_response": 8,
            "worker": 4,
        }

        if "recent_messages" in prepared:
            limit = message_limits.get(stage, DEFAULT_PROMPT_MESSAGES)
            if limit <= 0:
                prepared.pop("recent_messages", None)
            elif query:
                prepared["recent_messages"] = self.select_relevant_messages(
                    list(prepared.get("recent_messages", [])),
                    query,
                    limit=limit,
                )
            else:
                prepared["recent_messages"] = list(prepared.get("recent_messages", []))[-limit:]

        for key in ("reusable_artifacts", "artifacts"):
            if key in prepared:
                limit = artifact_limits.get(stage, DEFAULT_PROMPT_TASKS)
                if query:
                    prepared[key] = self.select_relevant_artifacts(
                        list(prepared.get(key, [])),
                        query,
                        limit=limit,
                    )
                else:
                    prepared[key] = list(prepared.get(key, []))[-limit:]

        if "tool_traces" in prepared:
            limit = trace_limits.get(stage, DEFAULT_PROMPT_TRACES)
            prepared["tool_traces"] = list(prepared.get("tool_traces", []))[-limit:]

        if "tool_capability_index" in prepared and max_chars < 10_000:
            prepared["tool_capability_index"] = summarize_tool_capability_index(
                prepared["tool_capability_index"],
            )

        return prepared

    def _append_task_result(
        self,
        context: dict[str, Any],
        task_id: str,
        result_summary: dict[str, Any],
    ) -> dict[str, Any]:
        """把一次 DAG 任务结果追加到 session context。

        参数：
            context: 当前 session context。
            task_id: 本轮 DAG 执行 ID。
            result_summary: 任务级摘要，通常包含 status/completed/failed 等字段。

        返回：
            更新后的同一个 context 字典。

        功能：
            把任务结果写入 ``context["tasks"]``，供后续 planner/final_response
            复用最近任务状态。
        """
        context["tasks"].append({
            "task_id": task_id,
            "turn_index": context.get("turn_index", 0),
            "summary": result_summary,
            "created_at": utc_now(),
        })
        return context

    def _session_header(self, context: dict[str, Any]) -> dict[str, Any]:
        """生成阶段上下文中的 session 元信息头。

        参数：
            context: 当前 session context。

        返回：
            包含 session_id/current_task_id/turn_index/created_at/updated_at 的字典。

        功能：
            给各阶段 context view 提供统一会话身份信息，避免传完整 session state。
        """
        return {
            "session_id": context.get("session_id"),
            "task_id": context.get("current_task_id"),
            "turn_index": context.get("turn_index"),
            "created_at": context.get("created_at"),
            "updated_at": context.get("updated_at"),
        }

    def _recent_messages(self, context: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        """读取最近若干条消息。

        参数：
            context: 当前 session context。
            limit: 最多返回的消息条数。

        返回：
            ``context["messages"]`` 尾部最多 ``limit`` 条。

        功能：
            为 goal_parser/final_response/memory_view 提供短期对话窗口。
        """
        return list(context.get("messages", []))[-limit:]

    def _build_memory_view(
        self,
        context: dict[str, Any],
        *,
        query: str,
        max_messages: int = DEFAULT_PROMPT_MESSAGES,
    ) -> dict[str, Any]:
        """构建嵌入阶段 context 的记忆视图。

        参数：
            context: 当前 session context。
            query: 当前阶段的查询文本，用于从 memory_facts/long_term_memory 检索相关项。
            max_messages: memory_context 中附带的最近消息条数。

        返回：
            MemoryManager 生成的记忆切片，包含 session_summary、recent_messages、
            relevant_facts 和 long_term_memory。

        功能：
            把 ContextManager 的滚动状态转换成 MemoryManager 可检索的记忆负载。
        """
        return self.memory.build_memory_context(
            query=query,
            recent_messages=self._recent_messages(context, max_messages),
            session_summary=context.get("summary", ""),
            memory_facts=context.get("memory_facts", []),
            long_term_memory=context.get("long_term_memory", []),
        )

    def _persist_memory_update(
        self,
        context: dict[str, Any],
        memory_update: dict[str, Any],
    ) -> None:
        """把 MemoryManager 产出的存储载荷写入 PostgreSQL memory 表。

        参数：
            context: 当前 session context，会记录持久化状态或错误。
            memory_update: MemoryManager.build_memory_update 返回的结果。

        返回：
            None。

        功能：
            在 PostgreSQL 可用时同步 memory_facts、session_summary 和长期记忆候选；
            失败只写 context_stats，不阻断 Redis context 保存。
        """
        if self.postgres_store is None:
            return

        session_id = context.get("session_id")
        if not session_id:
            return

        payload = memory_update.get("storage_payload", {})
        metadata = context.get("metadata", {})
        owner_id = str(metadata.get("user_id") or session_id)
        owner_type = "user" if metadata.get("user_id") else "session"

        try:
            self.postgres_store.upsert_memory_facts(
                session_id,
                payload.get("memory_facts", []),
            )
            self.postgres_store.save_session_summary(
                session_id,
                payload.get("session_summary", ""),
                task_id=context.get("current_task_id"),
                summary_type="rolling",
                status="active",
                covered_until_turn=context.get("turn_index"),
                source_message_count=context.get("context_stats", {}).get(
                    "last_compacted_message_count"
                ),
                metadata={
                    "source": "context_compaction",
                    "new_fact_count": len(memory_update.get("new_facts", [])),
                    "long_term_candidate_count": len(
                        memory_update.get("long_term_candidates", [])
                    ),
                },
            )
            self.postgres_store.upsert_long_term_memory(
                owner_id,
                payload.get("long_term_memory", []),
                owner_type=owner_type,
                source_session_id=session_id,
            )
            context["context_stats"] = {
                **context.get("context_stats", {}),
                "last_memory_persisted_at": utc_now(),
                "last_memory_persist_owner_id": owner_id,
                "last_memory_persist_owner_type": owner_type,
            }
        except Exception as exc:  # noqa: BLE001 - 记忆落库失败不能阻断 Redis 上下文保存
            context["context_stats"] = {
                **context.get("context_stats", {}),
                "last_memory_persist_error": str(exc),
                "last_memory_persist_error_at": utc_now(),
            }

    def _recent_tasks(
        self,
        context: dict[str, Any],
        limit: int = DEFAULT_PROMPT_TASKS,
    ) -> list[dict[str, Any]]:
        """读取最近若干条任务摘要。

        参数：
            context: 当前 session context。
            limit: 最多返回的任务条数。

        返回：
            ``context["tasks"]`` 尾部最多 ``limit`` 条。

        功能：
            给 goal_parser/planner/final_response 提供最近执行结果概览。
        """
        return list(context.get("tasks", []))[-limit:]

    def _recent_tool_traces(
        self,
        context: dict[str, Any],
        *,
        task_id: str | None = None,
        limit: int = DEFAULT_PROMPT_TRACES,
    ) -> list[dict[str, Any]]:
        """读取最近工具执行轨迹。

        参数：
            context: 当前 session context。
            task_id: 可选任务 ID；传入时只返回该任务下的轨迹。
            limit: 最多返回的轨迹条数。

        返回：
            最近 ``limit`` 条 tool trace。

        功能：
            给 final_response 还原本轮工具执行状态和错误信息。
        """
        traces = list(context.get("tool_traces", []))
        if task_id:
            traces = [trace for trace in traces if trace.get("task_id") == task_id]
        return traces[-limit:]

    def _recent_artifacts(
        self,
        context: dict[str, Any],
        limit: int = DEFAULT_PROMPT_TASKS,
    ) -> list[dict[str, Any]]:
        """读取最近产物引用。

        参数：
            context: 当前 session context。
            limit: 最多返回的产物条数。

        返回：
            ``context["artifacts"]`` 尾部最多 ``limit`` 条。

        功能：
            给 planner/tool_binding/argument_binding 复用上轮文件、URL、数据库等产物。
        """
        return list(context.get("artifacts", []))[-limit:]

    def _find_task(
        self,
        context: dict[str, Any],
        task_id: str | None,
    ) -> dict[str, Any] | None:
        """按 task_id 查找任务摘要。

        参数：
            context: 当前 session context。
            task_id: 要查找的任务 ID；为空时直接返回 None。

        返回：
            匹配的任务摘要字典；找不到返回 None。

        功能：
            final_response 构造阶段定位本轮任务摘要。
        """
        if not task_id:
            return None
        for task in reversed(context.get("tasks", [])):
            if task.get("task_id") == task_id:
                return task
        return None

    def _extract_user_constraints(
        self,
        goal: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """合并默认约束和当前 goal 约束。

        参数：
            goal: Goal IR，可能包含 ``constraints``。
            context: 当前 session context，metadata 中可能包含 default_constraints。

        返回：
            合并后的约束字典；goal 约束会覆盖 metadata 默认约束。

        功能：
            给 planner 提供当前请求需要遵守的用户偏好、格式、范围等限制。
        """
        return {
            **context.get("metadata", {}).get("default_constraints", {}),
            **(goal.get("constraints") or {}),
        }

    def _compact_node(self, node: dict[str, Any]) -> dict[str, Any]:
        """裁剪 DAG node 为 worker context 需要的最小结构。

        参数：
            node: 原始 DAG 节点。

        返回：
            只包含 id/capability/tool/input/binding_status/argument_status 的字典。

        功能：
            避免 worker 阶段拿到完整 planner 节点元数据，降低上下文噪声。
        """
        return {
            "id": node.get("id"),
            "capability": node.get("capability"),
            "tool": node.get("tool"),
            "input": node.get("input", {}),
            "binding_status": node.get("binding_status"),
            "argument_status": node.get("argument_status"),
        }

    def _compact_runtime_state(self, runtime_state: dict[str, Any]) -> dict[str, Any]:
        """裁剪 scheduler runtime_state 为最终回答可用摘要。

        参数：
            runtime_state: Scheduler 维护的完整运行时状态。

        返回：
            包含 status/completed/failed/queued/retry_counter 的小字典。

        功能：
            final_response 只需要任务状态概览，不需要完整 DAG/graph/indegree。
        """
        return {
            "status": runtime_state.get("status"),
            "completed": runtime_state.get("completed", []),
            "failed": runtime_state.get("failed", []),
            "queued": runtime_state.get("queued", []),
            "retry_counter": runtime_state.get("retry_counter", {}),
        }

    def _extract_artifacts_from_result(
        self,
        context: dict[str, Any],
        task_id: str,
        node_id: str | None,
        result: dict[str, Any],
        *,
        tool_name: str | None = None,
    ) -> None:
        """从 tool/task result 中提取标准化 artifact 引用。

        参数：
            context: 当前 session context，会被追加 artifact。
            task_id: 当前任务 ID。
            node_id: 来源节点 ID。
            result: 工具或任务结果。
            tool_name: 可选工具名。

        返回：
            None。

        功能：
            调用 build_artifacts_from_result 标准化产物，并去重写入 context["artifacts"]。

        Artifact 保存的是产物引用，不保存大内容本身。文件、目录、SQLite DB、
        图片、音频等都统一成可查询的 metadata。
        """
        artifacts = build_artifacts_from_result(
            task_id=task_id,
            node_id=node_id,
            result=result,
            tool_name=tool_name,
        )
        for artifact in artifacts:
            self._append_unique_artifact(context, artifact)

    def _append_unique_artifact(
        self,
        context: dict[str, Any],
        artifact: dict[str, Any],
    ) -> None:
        """按 URI、路径或值去重后追加产物。

        参数：
            context: 当前 session context。
            artifact: 待写入的标准 artifact。

        返回：
            None。

        功能：
            如果已有同一 URI/path/value 的 artifact，则更新元数据；否则追加新产物。
        """
        identity = artifact.get("uri") or artifact.get("path") or artifact.get("value")
        for existing in context["artifacts"]:
            existing_identity = (
                existing.get("uri")
                or existing.get("path")
                or existing.get("value")
            )
            if identity and existing_identity == identity:
                existing.update({
                    **artifact,
                    "artifact_id": existing.get("artifact_id", artifact["artifact_id"]),
                    "created_at": existing.get("created_at", artifact["created_at"]),
                    "updated_at": utc_now(),
                })
                return
        context["artifacts"].append(artifact)


@contextmanager
def managed_turn(
    session_id: str,
    user_input: str,
    *,
    task_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    context_manager: ContextManager | None = None,
) -> Iterator[dict[str, Any]]:
    """用 Python 上下文管理器包住一轮对话生命周期。

    参数：
        session_id: 会话 ID。
        user_input: 本轮用户输入。
        task_id: 可选任务 ID；不传则由 ContextManager 生成。
        metadata: 可选会话元数据，会合并到 context["metadata"]。
        context_manager: 可注入的 ContextManager；不传则创建默认实例。

    返回：
        yield 当前轮完整 session context；退出上下文时保存该 context。

    功能：
        给脚本或测试提供 ``with managed_turn(...)`` 风格的会话生命周期封装。
    """
    manager = context_manager or ContextManager()
    context = manager.start_turn(
        session_id,
        user_input,
        task_id=task_id,
        metadata=metadata,
    )
    try:
        yield context
    finally:
        manager.save(session_id, context)


def make_task_id(session_id: str, turn_index: int) -> str:
    """生成默认任务 ID。

    参数：
        session_id: 会话 ID。
        turn_index: 当前会话轮次。

    返回：
        带 session_id、turn_index 和随机后缀的任务 ID 字符串。

    功能：
        当外部没有传 task_id 时，为一轮用户请求创建可追踪的 DAG 执行 ID。
    """
    return f"{session_id}:turn:{turn_index}:{uuid4().hex[:8]}"


def normalize_skill_roots(value: Any = None) -> list[Path]:
    """把显式配置、环境变量和本地约定目录合并成 skill 根目录列表。"""
    raw_values: list[Any] = []
    if value:
        raw_values.extend(value if isinstance(value, list) else [value])

    env_value = os.environ.get("AGENT_SKILL_ROOTS", "")
    if env_value:
        for item in env_value.replace(",", os.pathsep).split(os.pathsep):
            if item.strip():
                raw_values.append(item.strip())

    paths: list[Path] = []
    for item in raw_values:
        try:
            paths.append(Path(str(item)).expanduser().resolve())
        except Exception:
            continue
    return dedupe_paths(paths)


def dedupe_paths(paths: list[Path]) -> list[Path]:
    """按 resolved path 去重，同时保留顺序。"""
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def empty_skill_context_bundle() -> dict[str, Any]:
    """返回 planner 可消费的空 skill bundle。"""
    return {
        "matched_skills": [],
        "skill_contexts": {},
        "reference_contexts": {},
        "capability_hints": [],
        "recommended_step_kinds": [],
        "default_resources": {},
    }


def utc_now() -> str:
    """返回 ISO-8601 UTC 时间戳。

    参数：
        无。

    返回：
        当前 UTC 时间的 ISO-8601 字符串。

    功能：
        统一生成 context、message、task、artifact 的创建/更新时间。
    """
    return datetime.now(timezone.utc).isoformat()


def json_dumps(value: Any) -> str:
    """统一 JSON 序列化，供提示词消息使用。

    参数：
        value: 任意可 JSON 序列化或可转字符串的值。

    返回：
        ``ensure_ascii=False`` 的 JSON 字符串。

    功能：
        保持提示词 payload、人类可读日志和预算估算的序列化规则一致。
    """
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def summarize_value(value: Any, *, max_chars: int = 500) -> Any:
    """生成短摘要，避免把大结果直接写入长期上下文。

    参数：
        value: 原始值，可以是标量、列表、字典或其他对象。
        max_chars: 字符串预览最大长度。

    返回：
        标量截断值、列表摘要、字典摘要或字符串预览。

    功能：
        压缩工具输入/输出和任务结果，避免 context 保存大文本或大 JSON。
    """
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        text = str(value)
        return text[:max_chars]
    if isinstance(value, list):
        return {
            "type": "list",
            "count": len(value),
            "preview": value[:3],
        }
    if isinstance(value, dict):
        summary: dict[str, Any] = {
            "type": "dict",
            "keys": list(value.keys())[:20],
        }
        for key in ("status", "action", "path", "output_path", "db_path", "table"):
            if key in value:
                summary[key] = value[key]
        if "content" in value and isinstance(value["content"], str):
            summary["content_preview"] = value["content"][:max_chars]
        return summary
    return str(value)[:max_chars]


def extract_budget_query(context: dict[str, Any]) -> str:
    """从阶段上下文中抽取用于相关性裁剪的查询文本。

    参数：
        context: 阶段 context view，可能来自 goal_parser/planner/worker/final_response。

    返回：
        用于消息、artifact 相关性筛选的查询字符串；无法提取时返回空字符串。

    功能：
        统一决定预算裁剪阶段“当前问题是什么”，优先 user_input，其次 goal，再次 node。
    """
    if context.get("user_input"):
        return str(context["user_input"])
    goal = context.get("goal")
    if isinstance(goal, dict):
        parts = [
            str(goal.get("text", "")),
            " ".join(str(entity) for entity in goal.get("entities", []) or []),
        ]
        return " ".join(part for part in parts if part.strip())
    node = context.get("node")
    if isinstance(node, dict):
        return json_dumps({
            "capability": node.get("capability"),
            "tool": node.get("tool"),
            "input": node.get("input", {}),
        })
    return ""


def summarize_tool_capability_index(
    index: dict[str, list[dict[str, Any]]],
    *,
    max_tools_per_capability: int = 3,
) -> dict[str, list[dict[str, Any]]]:
    """把能力到工具的索引瘦身，避免预算较小时工具索引过大。

    参数：
        index: capability -> tool 摘要列表的索引。
        max_tools_per_capability: 每个 capability 最多保留的工具数量。

    返回：
        裁剪后的 capability 索引，工具描述和标签也会截短。

    功能：
        当 LLM payload 字符预算较小时，保留工具索引结构但减少冗余字段。
    """
    return {
        capability: [
            {
                "name": tool.get("name"),
                "description": str(tool.get("description", ""))[:160],
                "category": tool.get("category"),
                "tags": tool.get("tags", [])[:5],
            }
            for tool in tools[:max_tools_per_capability]
        ]
        for capability, tools in index.items()
    }


def build_artifacts_from_result(
    *,
    task_id: str,
    node_id: str | None,
    result: dict[str, Any],
    tool_name: str | None = None,
) -> list[dict[str, Any]]:
    """把工具或任务结果转成标准产物引用列表。

    参数：
        task_id: 当前任务 ID。
        node_id: 产物来源节点 ID；任务级结果可为 None。
        result: 工具或任务返回结果。
        tool_name: 可选工具名。

    返回：
        标准 artifact 字典列表。

    功能：
        从 path/output_path/db_path/url 等字段中抽取可复用产物引用。
    """
    artifacts: list[dict[str, Any]] = []
    if not isinstance(result, dict):
        return artifacts

    # Minimax 图像工具会返回一组图片文件。
    output_paths = result.get("output_paths")
    if isinstance(output_paths, list):
        for path in output_paths:
            artifact = build_path_artifact(
                task_id=task_id,
                node_id=node_id,
                key="output_paths",
                value=path,
                result=result,
                tool_name=tool_name,
            )
            if artifact:
                artifacts.append(artifact)

    for key in ("path", "output_path", "output_file", "file_path", "db_path"):
        value = result.get(key)
        if not value:
            continue
        artifact = build_path_artifact(
            task_id=task_id,
            node_id=node_id,
            key=key,
            value=value,
            result=result,
            tool_name=tool_name,
        )
        if artifact:
            artifacts.append(artifact)

    # 部分工具可能返回 URL，而不是本地路径。
    for key in ("url", "uri", "image_url", "audio_url"):
        value = result.get(key)
        if not value:
            continue
        artifacts.append(build_uri_artifact(
            task_id=task_id,
            node_id=node_id,
            key=key,
            uri=str(value),
            result=result,
            tool_name=tool_name,
        ))

    return artifacts


def build_path_artifact(
    *,
    task_id: str,
    node_id: str | None,
    key: str,
    value: Any,
    result: dict[str, Any],
    tool_name: str | None,
) -> dict[str, Any] | None:
    """根据类路径结果字段构造产物。

    参数：
        task_id: 当前任务 ID。
        node_id: 产物来源节点 ID。
        key: 结果中携带路径的字段名。
        value: 路径或 URL 字段值。
        result: 原始工具结果。
        tool_name: 可选工具名。

    返回：
        标准 artifact 字典；value 不是有效路径/URL 时返回 None。

    功能：
        把本地文件、目录、SQLite DB 或 URL 统一成 artifact 引用。
    """
    if not isinstance(value, (str, Path)):
        return None

    raw_path = str(value)
    if not raw_path.strip():
        return None

    path = Path(raw_path).expanduser()
    resolved_path = path.resolve() if not is_url(raw_path) else path
    kind = infer_artifact_kind(key, raw_path, result, tool_name=tool_name)
    storage = "external_url" if is_url(raw_path) else "local_fs"
    uri = raw_path if is_url(raw_path) else resolved_path.as_uri()
    metadata = build_artifact_metadata(resolved_path, result, key=key)

    return {
        "artifact_id": make_artifact_id(),
        "kind": kind,
        "storage": storage,
        "uri": uri,
        "path": raw_path if is_url(raw_path) else str(resolved_path),
        "name": raw_path.rstrip("/").split("/")[-1] if is_url(raw_path) else resolved_path.name,
        "mime_type": infer_mime_type(raw_path, kind, result),
        "summary": summarize_artifact(result, key=key, value=raw_path),
        "task_id": task_id,
        "node_id": node_id,
        "tool_name": tool_name,
        "source_key": key,
        "created_at": utc_now(),
        "metadata": metadata,
        # 给旧调用方保留的兼容字段。
        "value": raw_path if is_url(raw_path) else str(resolved_path),
    }


def build_uri_artifact(
    *,
    task_id: str,
    node_id: str | None,
    key: str,
    uri: str,
    result: dict[str, Any],
    tool_name: str | None,
) -> dict[str, Any]:
    """根据 URL 或 URI 字段构造产物。

    参数：
        task_id: 当前任务 ID。
        node_id: 产物来源节点 ID。
        key: 结果中携带 URI 的字段名。
        uri: URL/URI 字符串。
        result: 原始工具结果。
        tool_name: 可选工具名。

    返回：
        标准 artifact 字典。

    功能：
        把外部 URL、图片 URL、音频 URL 等结果转成可复用 artifact。
    """
    kind = infer_artifact_kind(key, uri, result, tool_name=tool_name)
    return {
        "artifact_id": make_artifact_id(),
        "kind": kind,
        "storage": "external_url" if is_url(uri) else "unknown",
        "uri": uri,
        "path": None,
        "name": uri.rstrip("/").split("/")[-1],
        "mime_type": infer_mime_type(uri, kind, result),
        "summary": summarize_artifact(result, key=key, value=uri),
        "task_id": task_id,
        "node_id": node_id,
        "tool_name": tool_name,
        "source_key": key,
        "created_at": utc_now(),
        "metadata": {
            "action": result.get("action"),
            "status": result.get("status"),
        },
        "value": uri,
    }


def infer_artifact_kind(
    key: str,
    value: str,
    result: dict[str, Any],
    *,
    tool_name: str | None = None,
) -> str:
    """推断产物类型。

    参数：
        key: 结果字段名。
        value: 路径、URL 或 URI。
        result: 原始工具结果。
        tool_name: 可选工具名。

    返回：
        artifact kind，如 database/directory/image/audio/json/file/url。

    功能：
        根据字段名、文件后缀、工具动作和工具名判断产物类别。
    """
    action = str(result.get("action", "")).lower()
    tool = str(tool_name or "").lower()
    suffix = Path(value.split("?", 1)[0]).suffix.lower()

    if key == "db_path" or suffix in {".db", ".sqlite", ".sqlite3"}:
        return "database"
    if "directory" in action or "directory" in tool:
        return "directory"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        return "image"
    if suffix in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}:
        return "audio"
    if suffix in {".json"}:
        return "json"
    if suffix in {".txt", ".md", ".py", ".csv", ".log", ".yaml", ".yml"}:
        return "file"
    if key in {"url", "uri", "image_url", "audio_url"}:
        if "image" in key:
            return "image"
        if "audio" in key:
            return "audio"
        return "url"
    return "file"


def infer_mime_type(value: str, kind: str, result: dict[str, Any]) -> str | None:
    """根据路径后缀和结果元数据推断 MIME 类型。

    参数：
        value: 路径、URL 或 URI。
        kind: 已推断出的 artifact kind。
        result: 原始工具结果。

    返回：
        MIME 类型字符串；无法判断时返回 None。

    功能：
        给 artifact 附带更精确的类型信息，便于前端展示和后续工具选择。
    """
    if kind == "audio" and result.get("audio_format"):
        return f"audio/{result['audio_format']}"

    suffix = Path(value.split("?", 1)[0]).suffix.lower()
    mime_by_suffix = {
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".py": "text/x-python",
        ".csv": "text/csv",
        ".json": "application/json",
        ".db": "application/vnd.sqlite3",
        ".sqlite": "application/vnd.sqlite3",
        ".sqlite3": "application/vnd.sqlite3",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
    }
    if suffix in mime_by_suffix:
        return mime_by_suffix[suffix]
    if kind == "directory":
        return "inode/directory"
    if kind == "database":
        return "application/vnd.sqlite3"
    return None


def build_artifact_metadata(path: Path, result: dict[str, Any], *, key: str) -> dict[str, Any]:
    """构造产物元数据，尽量只放小字段。

    参数：
        path: 产物路径。
        result: 原始工具结果。
        key: 来源字段名。

    返回：
        小型 metadata 字典，包含状态、动作、大小、文件存在性等。

    功能：
        为 artifact 提供可检索元数据，同时避免保存大内容。
    """
    metadata: dict[str, Any] = {
        "source_key": key,
        "status": result.get("status"),
        "action": result.get("action"),
    }
    for field in (
        "bytes_written",
        "count",
        "table",
        "rows_affected",
        "last_row_id",
        "image_count",
        "audio_format",
        "audio_sample_rate",
        "audio_size",
        "audio_length",
        "voice_id",
    ):
        if field in result:
            metadata[field] = result[field]

    try:
        if path.exists():
            metadata["exists"] = True
            metadata["is_file"] = path.is_file()
            metadata["is_dir"] = path.is_dir()
            if path.is_file():
                metadata["size_bytes"] = path.stat().st_size
        else:
            metadata["exists"] = False
    except OSError:
        metadata["exists"] = None

    return metadata


def summarize_artifact(result: dict[str, Any], *, key: str, value: str) -> str:
    """生成给规划器或大模型看的一句话产物摘要。

    参数：
        result: 原始工具结果。
        key: artifact 来源字段名。
        value: artifact 路径、URL 或 URI。

    返回：
        一句话 artifact 摘要。

    功能：
        让 planner/argument_binding 能看懂“这个产物是什么”，无需读取原文件。
    """
    action = result.get("action") or "artifact"
    if key == "db_path" and result.get("table"):
        return f"{action}: SQLite database {value}, table={result.get('table')}"
    if key == "output_paths":
        return f"{action}: generated output file {value}"
    if "content" in result and isinstance(result["content"], str):
        return f"{action}: {value}, content_preview={result['content'][:120]}"
    return f"{action}: {value}"


def is_url(value: str) -> bool:
    """判断字符串是否是常见外部 URL。

    参数：
        value: 待判断字符串。

    返回：
        如果以 http/https/s3/gs 协议开头返回 True，否则返回 False。

    功能：
        区分本地路径和外部资源，决定 artifact storage 和 uri 生成方式。
    """
    lowered = value.lower()
    return lowered.startswith(("http://", "https://", "s3://", "gs://"))


def make_artifact_id() -> str:
    """生成 artifact ID。

    参数：
        无。

    返回：
        ``art_`` 前缀加随机短 ID 的字符串。

    功能：
        给每个标准产物引用分配稳定标识。
    """
    return f"art_{uuid4().hex[:12]}"


def trim_value_to_budget(value: Any, *, max_chars: int) -> Any:
    """递归裁剪任意类 JSON 值到字符预算附近。

    参数：
        value: 待裁剪的 JSON-like 值。
        max_chars: 目标字符预算。

    返回：
        裁剪后的值，类型尽量保持不变。

    功能：
        对字符串截断、列表保留尾部、字典优先保留关键字段，作为 prompt 预算兜底。
    """
    if max_chars <= 0:
        return ""

    if isinstance(value, str):
        return value if len(value) <= max_chars else value[: max(0, max_chars - 3)] + "..."

    if isinstance(value, (int, float, bool)) or value is None:
        return value

    if isinstance(value, list):
        if not value:
            return []
        budget_per_item = max(64, max_chars // max(1, min(len(value), 6)))
        # 保留尾部通常更接近当前轮上下文。
        result = [
            trim_value_to_budget(item, max_chars=budget_per_item)
            for item in value[-6:]
        ]
        while len(result) > 1 and len(json_dumps(result)) > max_chars:
            result.pop(0)
        return result

    if isinstance(value, dict):
        priority = [
            "stage",
            "system_prompt",
            "session",
            "user_input",
            "goal",
            "capability",
            "tool",
            "node",
            "conversation_summary",
            "summary",
            "memory_facts",
            "recent_messages",
            "recent_tasks",
            "reusable_artifacts",
            "artifacts",
            "tool_traces",
            "runtime_state",
            "final_results",
            "metadata",
            "context_stats",
        ]
        ordered_keys = [
            key for key in priority if key in value
        ] + [
            key for key in value.keys() if key not in priority
        ]
        result: dict[str, Any] = {}
        budget_per_key = max(128, max_chars // max(1, min(len(ordered_keys), 8)))
        for key in ordered_keys:
            result[key] = trim_value_to_budget(value[key], max_chars=budget_per_key)
            if len(json_dumps(result)) > max_chars:
                result.pop(key, None)
                break
        return result

    return trim_value_to_budget(str(value), max_chars=max_chars)


def score_text_relevance(query: str, text: str) -> int:
    """轻量关键词重叠评分。

    参数：
        query: 当前用户输入或阶段查询文本。
        text: 待评分文本。

    返回：
        非负整数分数，越高表示关键词重叠越多。

    功能：
        给消息和 artifact 的相关性筛选提供确定性打分，不依赖 LLM。
    """
    query_tokens = tokenize_for_relevance(query)
    text_tokens = tokenize_for_relevance(text)
    if not query_tokens or not text_tokens:
        return 0

    overlap = query_tokens.intersection(text_tokens)
    score = len(overlap) * 2

    lowered_query = query.lower()
    lowered_text = text.lower()
    for token in query_tokens:
        if len(token) >= 2 and token in lowered_text:
            score += 1
    if lowered_query and lowered_query in lowered_text:
        score += 3
    return score


def tokenize_for_relevance(text: str) -> set[str]:
    """中英文混合的简单分词器，用于相关性选择。

    参数：
        text: 待分词文本。

    返回：
        token 集合，包含英文/数字连续串、中文单字和中文二字窗口。

    功能：
        支撑 score_text_relevance 的关键词重叠计算。
    """
    lowered = str(text).lower()
    tokens: set[str] = set()
    current: list[str] = []

    for char in lowered:
        if char.isalnum() or char == "_":
            current.append(char)
        else:
            if current:
                tokens.add("".join(current))
                current = []
            if "\u4e00" <= char <= "\u9fff":
                tokens.add(char)

    if current:
        tokens.add("".join(current))

    # 对中文连续文本再加一些二字窗口，提升“上次结果/保存文件”这类短语匹配。
    chinese_chars = [char for char in lowered if "\u4e00" <= char <= "\u9fff"]
    for index in range(len(chinese_chars) - 1):
        tokens.add("".join(chinese_chars[index:index + 2]))

    return {token for token in tokens if token}

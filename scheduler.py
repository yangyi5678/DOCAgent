"""Distributed Executable DAG Scheduler.

Scheduler 消费 planner 生成的 Executable DAG，但不直接执行工具。

真正分布式执行链路是：

    planner
      -> scheduler.submit()
      -> Redis ready queue
      -> worker pop + lock + ToolDispatcher
      -> Redis result store
      -> scheduler.poll_results_and_advance()
      -> Redis ready queue for downstream nodes

Scheduler 只负责调度：
    1. 构建 node_map、入度表和出边表。
    2. 把入度为 0 的 entry nodes 推入 Redis ready queue。
    3. 轮询 worker 写回的 Redis result。
    4. 根据 edge predicate 处理 success / retry / branch。
    5. 依赖满足后，把下游节点继续推入 Redis ready queue。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from context_manager import ContextManager
from redis_infra import (
    RedisClientFactory,
    RedisLock,
    RedisQueue,
    RedisReadyQueue,
    RedisResultStore,
    RedisTaskStore,
)
from postgres_store import PostgresAgentStore
from replan_policy import analyze_result_for_replan
from task_control import (
    TASK_CANCEL_PERMISSION_DENIED,
    TASK_CANCEL_USER_REJECTED,
    TASK_STATUS_NEEDS_REPLAN,
    TASK_STATUS_WAITING_FOR_HUMAN,
    TERMINAL_TASK_STATUSES,
    TaskControl,
)


ExecutionResult = dict[str, Any]


class Scheduler:
    """基于 Redis 的分布式 Executable DAG 调度器。

    作用：
        把 planner 输出的 Executable DAG 调度给一个或多个独立 worker 执行。

    输入：
        planner 输出的 DAG：``{"nodes": [...], "edges": [...]}``。

    输出：
        Scheduler 会产生 Redis 副作用：
        ``queue:{task_id}`` 存 ready nodes；
        ``result:{task_id}`` 由 worker 写入执行结果；
        ``task:{task_id}`` 存 DAG 调度状态。

    核心计算逻辑：
        使用入度表做拓扑调度。scheduler 初始化 entry nodes；worker 执行节点并
        写回结果；scheduler 读取新结果后，根据边的 predicate 决定是否推进
        下游节点或重试当前节点。

    设计意图：
        scheduler 和 worker 可以运行在不同进程/机器上，中间只通过 Redis 通信。
    """

    def __init__(self, redis_client=None, postgres_store: PostgresAgentStore | None = None):
        """初始化分布式 scheduler。

        参数：
            redis_client: 可选 Redis client。不传时使用默认 Redis 连接。
            postgres_store: 可选 PostgreSQL 持久化存储。

        输出：
            无显式返回值。初始化 queue/result_store/task_store 三个 Redis 封装。
        """
        self.redis = redis_client or RedisClientFactory().create()
        self.queue = RedisQueue(self.redis)
        self.ready_queue = RedisReadyQueue(self.redis)
        self.lock_manager = RedisLock(self.redis)
        self.scheduler_owner_id = f"scheduler-{uuid.uuid4().hex}"
        self.result_store = RedisResultStore(self.redis)
        self.task_store = RedisTaskStore(self.redis)
        self.task_control = TaskControl(self.redis, postgres_store=postgres_store)
        self.postgres_store = postgres_store
        self.context_manager = ContextManager(
            redis_client=self.redis,
            postgres_store=postgres_store,
        )

    def prepare_dag(self, dag: dict[str, Any]) -> dict[str, Any]:
        """补齐 scheduler 运行需要的 DAG 派生结构。

        输入：
            planner 输出的 DAG，至少包含 ``nodes`` 和 ``edges``。

        输出：
            带 ``node_map`` 的 DAG 副本。

        核心计算逻辑：
            根据 ``nodes`` 自动构建 ``node_map``，避免 planner 关心 scheduler
            内部索引。

        设计意图：
            模块解耦：planner 只输出执行图，scheduler 自己维护调度索引。
        """
        prepared = dict(dag)
        prepared.setdefault("nodes", [])
        prepared.setdefault("edges", [])
        prepared["node_map"] = {
            node["id"]: node
            for node in prepared["nodes"]
        }
        return prepared

    def build(self, dag: dict[str, Any]) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]]]:
        """根据 DAG 构建入度表和出边表。

        输入：
            已经 ``prepare_dag`` 的 DAG。

        输出：
            ``(indegree, graph)``：
            ``indegree[node_id]`` 表示节点还有多少前置依赖未完成；
            ``graph[node_id]`` 保存从该节点出发的所有边。

        核心计算逻辑：
            普通依赖边会增加目标节点入度；retry 自环不计入入度，否则入口节点
            会被自己的 retry 边阻塞。

        设计意图：
            用标准拓扑调度表达“哪些节点可以并行执行”。
        """
        indegree: dict[str, int] = {}
        graph: dict[str, list[dict[str, Any]]] = {}

        for node in dag["nodes"]:
            indegree[node["id"]] = 0
            graph[node["id"]] = []

        for edge in dag["edges"]:
            from_id = edge["from"]
            to_id = edge["to"]
            graph.setdefault(from_id, []).append(edge)

            if self.is_dependency_edge(edge):
                indegree[to_id] = indegree.get(to_id, 0) + 1

        return indegree, graph

    def is_dependency_edge(self, edge: dict[str, Any]) -> bool:
        """判断一条边是否影响下游入度。

        输入：
            DAG edge。

        输出：
            需要参与拓扑依赖计算返回 True；retry/self-loop 返回 False。

        核心计算逻辑：
            ``type == retry`` 或 ``from == to`` 的边只表示控制流，不表示前置依赖。

        设计意图：
            把“正常依赖”和“失败重试”分离，避免重试边破坏 DAG 拓扑调度。
        """
        if edge.get("type") == "retry":
            return False
        if edge.get("from") == edge.get("to"):
            return False
        return True

    def submit(
        self,
        task_id: str,
        dag: dict[str, Any],
        reset: bool = True,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """提交一个 DAG 到 Redis，启动分布式执行。

        输入：
            task_id: 本次任务 ID，用于隔离 Redis key。
            dag: planner 输出的 Executable DAG。
            reset: 是否清空同 task_id 的旧队列、旧结果、旧调度状态。
            context: 可选多轮上下文；不传时优先使用 dag["context"]。

        输出：
            任务运行时状态字典，包含 ``indegree``、``graph``、``completed`` 等。

        核心计算逻辑：
            prepare DAG -> build indegree/graph -> 保存任务状态 -> 入队所有
            indegree 为 0 的 entry nodes。

        设计意图：
            submit 只负责“启动调度”，真实工具执行由外部 worker 进程完成。
        """
        dag = self.prepare_dag(dag)
        indegree, graph = self.build(dag)

        if reset:
            self.queue.clear(task_id)
            self.ready_queue.remove_task(task_id)
            self.result_store.clear(task_id)
            self.task_store.clear(task_id)

        runtime_state = {
            "dag": dag,
            "indegree": indegree,
            "graph": graph,
            "retry_counter": {},
            "queued": [],
            "completed": [],
            "failed": [],
            "processed_results": [],
            "status": "running",
            "cancel_reason": None,
            "cancel_message": None,
            "cancelled_at": None,
            "timed_out_at": None,
            "context": context or dag.get("context", {}),
            "metadata": dict((dag.get("metadata") or {}).get("runtime_metadata") or {}),
            "replan_request": None,
        }

        for node_id, degree in indegree.items():
            if degree == 0:
                self.enqueue_node(task_id, dag, runtime_state, node_id)
                if runtime_state.get("status") == TASK_STATUS_WAITING_FOR_HUMAN:
                    break

        self.save_runtime_state(task_id, runtime_state)
        self.log_event(
            "scheduler_submitted",
            task_id,
            runtime_state,
            payload={
                "node_count": len(dag.get("nodes", [])),
                "edge_count": len(dag.get("edges", [])),
                "queued": runtime_state.get("queued", []),
            },
        )
        self.save_checkpoint(task_id, runtime_state, "scheduler_submitted")
        return runtime_state

    def enqueue_node(
        self,
        task_id: str,
        dag: dict[str, Any],
        runtime_state: dict[str, Any],
        node_id: str,
    ) -> None:
        """把一个 ready node 推入 Redis queue。

        输入：
            task_id、DAG、运行时状态、待入队节点 ID。

        输出：
            无显式返回值。副作用是 Redis ``queue:{task_id}`` 增加一个节点。

        核心计算逻辑：
            如果节点不存在、已经完成、已经在 queued 集合中，则不重复入队；
            否则把 node payload 推入 Redis。

        设计意图：
            防止 scheduler 轮询结果时重复推进同一节点。
        """
        queued = set(runtime_state.get("queued", []))
        completed = set(runtime_state.get("completed", []))

        if runtime_state.get("status") != "running":
            return
        if node_id not in dag["node_map"]:
            return
        if node_id in completed or node_id in queued:
            return

        node_payload = dict(dag["node_map"][node_id])
        if node_payload.get("node_type") == "human_interrupt":
            self.pause_for_human_interrupt(task_id, dag, runtime_state, node_payload)
            return

        if runtime_state.get("status") == TASK_STATUS_WAITING_FOR_HUMAN:
            return

        if runtime_state.get("context"):
            node_payload["context"] = runtime_state["context"]

        self.ready_queue.push(task_id, node_payload)
        runtime_state.setdefault("queued", []).append(node_id)

    def pause_for_human_interrupt(
        self,
        task_id: str,
        dag: dict[str, Any],
        runtime_state: dict[str, Any],
        node: dict[str, Any],
    ) -> None:
        """Pause scheduling at a human interrupt node instead of queueing it."""
        node_id = node["id"]
        if runtime_state.get("pending_interrupt_id"):
            return

        interrupt = node.get("interrupt") or {}
        prompt = interrupt.get("prompt") or node.get("description") or "请确认是否继续。"
        required_input = interrupt.get("required_input") or {
            "type": "approval",
            "options": ["approve", "reject"],
        }
        context_payload = interrupt.get("context_payload") or {}

        runtime_state["status"] = "waiting_for_human"
        runtime_state["waiting_node_id"] = node_id
        runtime_state["pending_interrupt"] = {
            "node_id": node_id,
            "prompt": prompt,
            "required_input": required_input,
            "context_payload": context_payload,
            "reason": interrupt.get("reason"),
            "interrupt_type": interrupt.get("interrupt_type", "human_input"),
        }

        checkpoint_id = self.save_checkpoint(task_id, runtime_state, "human_interrupt")
        interrupt_id = None
        if self.postgres_store is not None:
            context = runtime_state.get("context", {})
            plan_id = (dag.get("metadata") or {}).get("plan_id")
            interrupt_id = self.postgres_store.create_interrupt(
                task_id,
                session_id=context.get("session_id"),
                plan_id=plan_id,
                node_id=node_id,
                checkpoint_id=checkpoint_id,
                interrupt_type=interrupt.get("interrupt_type", "human_input"),
                reason=interrupt.get("reason"),
                prompt=prompt,
                required_input=required_input,
                context_payload=context_payload,
            )

        runtime_state["pending_interrupt_id"] = interrupt_id
        runtime_state["pending_interrupt"]["interrupt_id"] = interrupt_id
        self.log_event(
            "human_interrupt.requested",
            task_id,
            runtime_state,
            node_id=node_id,
            payload={
                "interrupt_id": interrupt_id,
                "checkpoint_id": checkpoint_id,
                "prompt": prompt,
                "required_input": required_input,
                "reason": interrupt.get("reason"),
            },
        )

    def save_runtime_state(self, task_id: str, runtime_state: dict[str, Any]) -> None:
        """保存任务运行时状态到 Redis。

        输入：
            task_id 和 runtime_state。

        输出：
            无显式返回值。状态写入 ``task:{task_id}`` hash。

        核心计算逻辑：
            将 DAG、入度表、出边表、重试计数、完成集合等字段分别序列化保存。

        设计意图：
            scheduler 可以重启后恢复调度状态，也方便外部观察任务进度。
        """
        for field, value in runtime_state.items():
            self.task_store.save_field(task_id, field, value)

    def load_runtime_state(self, task_id: str) -> dict[str, Any] | None:
        """从 Redis 读取任务运行时状态。

        输入：
            task_id。

        输出：
            找到任务返回 runtime_state；否则返回 None。

        核心计算逻辑：
            以 ``dag`` 字段是否存在判断任务是否已提交，然后读取 scheduler
            需要的所有状态字段。

        设计意图：
            支持 submit 和调度循环分开运行，也支持 scheduler 进程重启恢复。
        """
        return self.task_control.load_runtime_state(task_id)

    def cancel_task(
        self,
        task_id: str,
        *,
        reason: str = "user_cancelled",
        message: str | None = None,
        clear_queue: bool = True,
    ) -> dict[str, Any]:
        """Cancel a task through the single task-control surface."""
        runtime_state = self.task_control.cancel_task(
            task_id,
            reason=reason,
            message=message,
            clear_queue=clear_queue,
        )
        self.finalize_context_if_terminal(task_id, runtime_state)
        self.save_runtime_state(task_id, runtime_state)
        return runtime_state

    def poll_results_and_advance(self, task_id: str) -> dict[str, Any]:
        """读取 worker 结果，并推进 DAG。

        输入：
            task_id。

        输出：
            更新后的 runtime_state。

        核心计算逻辑：
            从 ``result:{task_id}`` 读取所有结果；跳过已处理结果；对每个新结果
            调用 ``advance_from_result``；最后保存更新后的运行状态。

        设计意图：
            scheduler 不关心 worker 在哪里运行，只关心 Redis 里出现了什么结果。
        """
        scheduler_lock_id = f"scheduler:{task_id}"
        if not self.lock_manager.acquire(scheduler_lock_id, self.scheduler_owner_id):
            runtime_state = self.load_runtime_state(task_id)
            if runtime_state is None:
                raise ValueError(f"任务尚未 submit: {task_id}")
            return runtime_state

        try:
            return self._poll_results_and_advance_locked(task_id)
        finally:
            self.lock_manager.release(scheduler_lock_id, self.scheduler_owner_id)

    def _poll_results_and_advance_locked(self, task_id: str) -> dict[str, Any]:
        """Poll worker results while holding the task scheduler lock."""
        runtime_state = self.load_runtime_state(task_id)
        if runtime_state is None:
            raise ValueError(f"任务尚未 submit: {task_id}")
        if runtime_state.get("status") in TERMINAL_TASK_STATUSES:
            return runtime_state
        if runtime_state.get("status") == TASK_STATUS_NEEDS_REPLAN:
            return runtime_state

        processed = set(runtime_state.get("processed_results", []))
        all_results = self.result_store.all(task_id)

        for node_id, result in all_results.items():
            latest_status = self.task_store.load_field(task_id, "status", runtime_state.get("status"))
            if latest_status in TERMINAL_TASK_STATUSES:
                runtime_state["status"] = latest_status
                return self.load_runtime_state(task_id) or runtime_state
            if runtime_state.get("status") in TERMINAL_TASK_STATUSES:
                break
            result_token = self.result_token(node_id, result)
            if result_token in processed:
                continue
            self.advance_from_result(task_id, runtime_state, node_id, result)
            self.sync_context_from_result(task_id, runtime_state, node_id, result)
            self.log_event(
                "node_result_processed",
                task_id,
                runtime_state,
                node_id=node_id,
                payload={"result": result},
            )
            runtime_state.setdefault("processed_results", []).append(result_token)
            processed.add(result_token)

        latest_status = self.task_store.load_field(task_id, "status", runtime_state.get("status"))
        if latest_status in TERMINAL_TASK_STATUSES and latest_status != runtime_state.get("status"):
            return self.load_runtime_state(task_id) or runtime_state

        self.refresh_task_status(runtime_state)
        self.finalize_context_if_terminal(task_id, runtime_state)
        self.save_runtime_state(task_id, runtime_state)
        self.save_checkpoint(task_id, runtime_state, "scheduler_poll")
        return runtime_state

    def sync_context_from_result(
        self,
        task_id: str,
        runtime_state: dict[str, Any],
        node_id: str,
        result: ExecutionResult,
    ) -> None:
        """Write one processed node result back into session context."""
        context = runtime_state.get("context") or {}
        session_id = context.get("session_id")
        if not session_id:
            return

        node = ((runtime_state.get("dag") or {}).get("node_map") or {}).get(node_id, {})
        output_payload = result.get("result")
        if not isinstance(output_payload, dict):
            output_payload = {"value": output_payload} if output_payload is not None else {}

        try:
            self.context_manager.record_tool_result(
                str(session_id),
                task_id=task_id,
                node_id=node_id,
                tool_name=result.get("tool") or node.get("tool"),
                status=str(result.get("status") or "unknown"),
                input_payload=node.get("input") or {},
                output_payload=output_payload,
                error=result.get("error"),
                worker_id=result.get("worker_id"),
                attempt=result.get("attempt"),
            )
        except Exception as exc:  # noqa: BLE001 - context sync must not break scheduling
            runtime_state["context_sync_error"] = str(exc)
            self.log_event(
                "context_sync_failed",
                task_id,
                runtime_state,
                node_id=node_id,
                level="warning",
                payload={"error": str(exc)},
            )

    def finalize_context_if_terminal(
        self,
        task_id: str,
        runtime_state: dict[str, Any],
    ) -> None:
        """Record the task summary into session context when the DAG reaches a terminal state."""
        status = runtime_state.get("status")
        if status not in TERMINAL_TASK_STATUSES:
            return
        if runtime_state.get("context_task_recorded"):
            return

        context = runtime_state.get("context") or {}
        session_id = context.get("session_id")
        if not session_id:
            return

        summary = {
            "status": status,
            "completed": list(runtime_state.get("completed", [])),
            "failed": list(runtime_state.get("failed", [])),
            "cancel_reason": runtime_state.get("cancel_reason"),
            "cancel_message": runtime_state.get("cancel_message"),
            "timed_out_at": runtime_state.get("timed_out_at"),
        }
        try:
            self.context_manager.record_task_result(str(session_id), task_id, summary)
            self.context_manager.maybe_compact_history(str(session_id))
            runtime_state["context_task_recorded"] = True
        except Exception as exc:  # noqa: BLE001 - context sync must not break scheduling
            runtime_state["context_finalize_error"] = str(exc)
            self.log_event(
                "context_finalize_failed",
                task_id,
                runtime_state,
                level="warning",
                payload={"error": str(exc)},
            )

    def advance_from_result(
        self,
        task_id: str,
        runtime_state: dict[str, Any],
        node_id: str,
        result: ExecutionResult,
    ) -> None:
        """根据一个节点执行结果推进调度状态。

        输入：
            task_id、runtime_state、完成的 node_id、worker 写回的 result。

        输出：
            无显式返回值。会修改 runtime_state，并可能向 Redis queue 推入新节点。

        核心计算逻辑：
            节点成功则加入 completed；节点失败则先看 retry 边是否触发，如果仍有
            重试次数就重新入队当前节点；普通边满足 predicate 时扣减目标入度，
            目标入度归零后入队。

        设计意图：
            把 DAG 控制流集中在 scheduler，worker 只做工具执行。
        """
        dag = runtime_state["dag"]
        graph = runtime_state["graph"]
        indegree = runtime_state["indegree"]

        if runtime_state.get("status") in TERMINAL_TASK_STATUSES:
            return
        if node_id not in dag["node_map"]:
            return

        node = dag["node_map"][node_id]
        if node.get("node_type") == "human_interrupt":
            runtime_state["status"] = "running"
            runtime_state["waiting_node_id"] = None
            runtime_state["pending_interrupt_id"] = None
            runtime_state["pending_interrupt"] = None

        queued = runtime_state.get("queued", [])
        if node_id in queued:
            queued.remove(node_id)

        replan_request = analyze_result_for_replan(
            runtime_state=runtime_state,
            node_id=node_id,
            node=node,
            result=result,
            retry_exhausted=False,
        )
        if replan_request:
            self.mark_needs_replan(task_id, runtime_state, node_id, replan_request)
            return

        retry_enqueued = False

        for edge in graph.get(node_id, []):
            if not self.edge_is_satisfied(edge, result):
                continue

            if edge.get("type") == "retry":
                if result.get("retryable") is False:
                    continue
                retry_enqueued = self.handle_retry_edge(
                    task_id,
                    dag,
                    runtime_state,
                    node_id,
                    edge,
                )
                continue

            target = edge["to"]
            indegree[target] = max(0, indegree.get(target, 0) - 1)
            if indegree[target] == 0:
                self.enqueue_node(task_id, dag, runtime_state, target)

        if result.get("status") == "success":
            self.add_unique(runtime_state, "completed", node_id)
            return

        if not retry_enqueued:
            replan_request = analyze_result_for_replan(
                runtime_state=runtime_state,
                node_id=node_id,
                node=node,
                result=result,
                retry_exhausted=True,
            )
            if replan_request:
                self.mark_needs_replan(task_id, runtime_state, node_id, replan_request)
                return
            self.add_unique(runtime_state, "failed", node_id)

    def mark_needs_replan(
        self,
        task_id: str,
        runtime_state: dict[str, Any],
        node_id: str,
        replan_request: dict[str, Any],
    ) -> None:
        """Pause old DAG scheduling and mark the task for runtime replanning."""
        runtime_state["status"] = TASK_STATUS_NEEDS_REPLAN
        runtime_state["replan_request"] = replan_request
        runtime_state["queued"] = []
        self.queue.clear(task_id)
        self.ready_queue.remove_task(task_id)
        self.log_event(
            "task_needs_replan",
            task_id,
            runtime_state,
            node_id=node_id,
            level="warning",
            payload=replan_request,
        )

    def resume_from_interrupt(
        self,
        *,
        task_id: str | None = None,
        interrupt_id: str | None = None,
        resolution_payload: dict[str, Any] | None = None,
        response_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Resume a task paused at a human interrupt node.

        The human interrupt is treated as a virtual node result. Approval or
        submitted input advances the DAG through success edges; rejection
        cancels the whole task through TaskControl.
        """
        interrupt_record = None
        if interrupt_id is not None and self.postgres_store is not None:
            interrupt_record = self.postgres_store.get_interrupt(interrupt_id)
            if interrupt_record is None:
                raise ValueError(f"未找到 interrupt: {interrupt_id}")
            task_id = interrupt_record["task_id"]

        if task_id is None:
            raise ValueError("resume_from_interrupt 需要 task_id 或 interrupt_id")

        runtime_state = self.load_runtime_state(task_id)
        if runtime_state is None:
            raise ValueError(f"任务尚未 submit: {task_id}")
        if runtime_state.get("status") != "waiting_for_human":
            raise ValueError(f"任务当前不是 waiting_for_human: {runtime_state.get('status')}")

        pending_interrupt = runtime_state.get("pending_interrupt") or {}
        resolved_interrupt_id = interrupt_id or runtime_state.get("pending_interrupt_id")
        node_id = (
            (interrupt_record or {}).get("node_id")
            or runtime_state.get("waiting_node_id")
            or pending_interrupt.get("node_id")
        )
        if not node_id:
            raise ValueError("runtime_state 缺少 waiting_node_id，无法恢复")

        payload = resolution_payload or {}
        decision = str(payload.get("decision") or payload.get("status") or "approve").lower()
        rejected = decision in {"reject", "rejected", "deny", "denied", "cancel", "cancelled"}
        result_status = "failed" if rejected else "success"
        interrupt_status = "rejected" if rejected else "resolved"

        result: ExecutionResult = {
            "status": result_status,
            "result": {
                "interrupt_id": resolved_interrupt_id,
                "decision": decision,
                "resolution_payload": payload,
            },
            "attempt": 1,
            "source": "human_interrupt",
        }
        if rejected:
            result["error"] = payload.get("reason") or "human rejected interrupt"
        else:
            self.apply_interrupt_resolution_to_target(runtime_state, node_id, pending_interrupt, payload)

        if interrupt_id is not None and self.postgres_store is not None:
            self.postgres_store.resolve_interrupt(
                interrupt_id,
                status=interrupt_status,
                resolution_payload=payload,
                response_message_id=response_message_id,
            )

        if rejected:
            reason = (
                TASK_CANCEL_PERMISSION_DENIED
                if pending_interrupt.get("interrupt_type") == "approval"
                else TASK_CANCEL_USER_REJECTED
            )
            return self.cancel_task(
                task_id,
                reason=reason,
                message=payload.get("reason") or "human rejected interrupt",
            )

        self.advance_from_result(task_id, runtime_state, node_id, result)
        self.log_event(
            "human_interrupt.resolved" if not rejected else "human_interrupt.rejected",
            task_id,
            runtime_state,
            node_id=node_id,
            payload={
                "interrupt_id": resolved_interrupt_id,
                "decision": decision,
                "result": result,
            },
        )
        self.refresh_task_status(runtime_state)
        self.save_runtime_state(task_id, runtime_state)
        self.save_checkpoint(task_id, runtime_state, "human_interrupt_resumed")
        return runtime_state

    def apply_interrupt_resolution_to_target(
        self,
        runtime_state: dict[str, Any],
        interrupt_node_id: str,
        pending_interrupt: dict[str, Any],
        resolution_payload: dict[str, Any],
    ) -> None:
        """Apply approved human interrupt data to the downstream executable node."""
        context_payload = pending_interrupt.get("context_payload") or {}
        target_node_id = context_payload.get("target_node_id")
        if not target_node_id:
            return

        dag = runtime_state.get("dag") or {}
        target = (dag.get("node_map") or {}).get(target_node_id)
        if not target:
            return

        interrupt_type = pending_interrupt.get("interrupt_type")
        if interrupt_type == "approval":
            target_input = dict(target.get("input") or {})
            target_input["approved"] = True
            target["input"] = target_input
            return

        if interrupt_type == "clarification":
            form_data = resolution_payload.get("form_data") or {}
            if isinstance(form_data, dict) and form_data:
                target_input = dict(target.get("input") or {})
                target_input.update(form_data)
                target["input"] = target_input

    def handle_retry_edge(
        self,
        task_id: str,
        dag: dict[str, Any],
        runtime_state: dict[str, Any],
        node_id: str,
        edge: dict[str, Any],
    ) -> bool:
        """处理失败重试边。

        输入：
            task_id、DAG、runtime_state、失败节点 ID、retry edge。

        输出：
            本次是否重新入队当前节点。

        核心计算逻辑：
            递增 retry_counter；没有超过 ``max_retry`` 则清理旧结果处理标记并
            重新入队当前节点。

        设计意图：
            retry 是调度策略，不放进 worker；worker 只负责执行一次节点。
        """
        retry_counter = runtime_state.setdefault("retry_counter", {})
        retry_counter[node_id] = retry_counter.get(node_id, 0) + 1

        max_retry = self.node_max_retries(dag, node_id, edge)
        if retry_counter[node_id] > max_retry:
            return False

        self.enqueue_node(task_id, dag, runtime_state, node_id)
        return True

    def node_max_retries(
        self,
        dag: dict[str, Any],
        node_id: str,
        edge: dict[str, Any] | None = None,
    ) -> int:
        """Return retry limit, preferring node sandbox limits over retry edge defaults."""
        node = (dag.get("node_map") or {}).get(node_id) or {}
        sandbox = node.get("sandbox") or {}
        sandbox_limits = sandbox.get("resource_limits") or {}
        limits = node.get("limits") or {}
        if node.get("max_retries") is not None:
            return int(node["max_retries"])
        if sandbox_limits.get("max_retries") is not None:
            return int(sandbox_limits["max_retries"])
        if limits.get("max_retries") is not None:
            return int(limits["max_retries"])
        if edge and edge.get("max_retry") is not None:
            return int(edge["max_retry"])
        return 0

    def refresh_task_status(self, runtime_state: dict[str, Any]) -> None:
        """刷新任务整体状态。

        输入：
            runtime_state。

        输出：
            无显式返回值，会更新 ``runtime_state["status"]``。

        核心计算逻辑：
            所有节点成功完成则 status=completed；存在失败终止节点且没有待运行
            节点则 status=failed；否则保持 running。

        设计意图：
            给外部调用方一个简单的任务级状态。
        """
        node_ids = {node["id"] for node in runtime_state["dag"]["nodes"]}
        completed = set(runtime_state.get("completed", []))
        failed = set(runtime_state.get("failed", []))
        queued = set(runtime_state.get("queued", []))

        if runtime_state.get("status") in TERMINAL_TASK_STATUSES:
            return
        if runtime_state.get("status") == TASK_STATUS_NEEDS_REPLAN:
            return
        if runtime_state.get("status") == TASK_STATUS_WAITING_FOR_HUMAN:
            return
        if node_ids and node_ids.issubset(completed):
            runtime_state["status"] = "completed"
        elif failed and not queued:
            runtime_state["status"] = "failed"
        else:
            runtime_state["status"] = "running"

    def mark_timeout(
        self,
        task_id: str,
        runtime_state: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Mark timeout through the single task-control surface."""
        runtime_state = self.task_control.mark_timeout(
            task_id,
            runtime_state,
            timeout_seconds=timeout_seconds,
        )
        self.finalize_context_if_terminal(task_id, runtime_state)
        self.save_runtime_state(task_id, runtime_state)
        return runtime_state

    def eval_condition(self, condition: dict[str, Any] | None, result: ExecutionResult) -> bool:
        """兼容旧版 edge.condition 的条件判断。

        输入：
            condition 字典和节点执行 result。

        输出：
            条件满足返回 True，否则返回 False。

        核心计算逻辑：
            支持 ``result_field`` 条件：eq/ne/gte/lte。

        设计意图：
            让 scheduler 同时兼容旧 planner 和当前 predicate 边。
        """
        if condition is None:
            return True

        if condition.get("type") == "result_field":
            left = result.get(condition["key"])
            right = condition["value"]
            op = condition.get("op", "eq")

            if op == "eq":
                return left == right
            if op == "ne":
                return left != right
            if op == "gte":
                return float(left) >= float(right)
            if op == "lte":
                return float(left) <= float(right)

        return False

    def eval_predicate(self, predicate: str | None, result: ExecutionResult) -> bool:
        """判断 planner edge.predicate 字符串是否满足。

        输入：
            predicate 字符串和节点执行 result。

        输出：
            predicate 成立返回 True，否则返回 False。

        核心计算逻辑：
            当前支持：
            ``status == success``、``status == failed``、``status != success``。

        设计意图：
            先用最小可用表达式承载 DAG 控制流，后续可替换为安全表达式解释器。
        """
        if predicate is None:
            return True

        normalized = " ".join(predicate.strip().split())
        status = result.get("status")

        if normalized == "status == success":
            return status == "success"
        if normalized in {"status == failed", "status == fail"}:
            return status in {"failed", "fail"}
        if normalized == "status != success":
            return status != "success"

        return False

    def edge_is_satisfied(self, edge: dict[str, Any], result: ExecutionResult) -> bool:
        """统一判断一条边是否被当前执行结果触发。

        输入：
            DAG edge 和节点执行 result。

        输出：
            触发该边返回 True，否则返回 False。

        核心计算逻辑：
            优先使用新版 ``predicate``；其次兼容旧版 ``condition``；都没有时按
            edge type 给默认语义：success/data_dependency 需要成功，retry 需要失败。

        设计意图：
            让 planner 输出的控制流边和旧版条件边都能被 scheduler 消费。
        """
        if "predicate" in edge:
            return self.eval_predicate(edge.get("predicate"), result)
        if "condition" in edge:
            return self.eval_condition(edge.get("condition"), result)

        edge_type = edge.get("type")
        if edge_type == "retry":
            return result.get("status") in {"failed", "fail"}
        if edge_type in {"success", "data_dependency", None}:
            return result.get("status") == "success"

        return True

    def run(
        self,
        task_id: str,
        dag: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        poll_interval_seconds: float = 1.0,
        timeout_seconds: float | None = None,
        reset: bool = True,
    ) -> dict[str, Any]:
        """运行分布式调度循环。

        输入：
            task_id: 任务 ID。
            dag: 可选 Executable DAG。传入时先 submit；不传时从 Redis 恢复。
            context: 可选多轮上下文，会随 ready node 透传给 worker。
            poll_interval_seconds: 没有新结果时的轮询间隔。
            timeout_seconds: 可选超时时间。
            reset: 传入 dag 时是否清空旧任务状态。

        输出：
            最终 runtime_state。

        核心计算逻辑：
            submit 或 load runtime_state；循环 poll_results_and_advance；当任务
            completed/failed 或超时时退出。

        设计意图：
            这个 run 可以作为独立 scheduler 进程入口；worker 进程另行启动。
        """
        if dag is not None:
            runtime_state = self.submit(task_id, dag, reset=reset, context=context)
        else:
            runtime_state = self.load_runtime_state(task_id)
            if runtime_state is None:
                raise ValueError(f"任务尚未 submit: {task_id}")

        started_at = time.monotonic()

        while runtime_state.get("status") == "running":
            runtime_state = self.poll_results_and_advance(task_id)

            if runtime_state.get("status") != "running":
                break

            if timeout_seconds is not None:
                elapsed = time.monotonic() - started_at
                if elapsed >= timeout_seconds:
                    runtime_state = self.mark_timeout(
                        task_id,
                        runtime_state,
                        timeout_seconds=timeout_seconds,
                    )
                    break

            time.sleep(poll_interval_seconds)

        return runtime_state

    def log_event(
        self,
        event_type: str,
        task_id: str,
        runtime_state: dict[str, Any],
        *,
        node_id: str | None = None,
        level: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Write a scheduler event to PostgreSQL when persistence is enabled."""
        if self.postgres_store is None:
            return
        context = runtime_state.get("context", {})
        self.postgres_store.log_event(
            event_type,
            session_id=context.get("session_id"),
            task_id=task_id,
            node_id=node_id,
            level=level,
            payload=payload or {},
        )

    def save_checkpoint(
        self,
        task_id: str,
        runtime_state: dict[str, Any],
        checkpoint_type: str,
    ) -> str | None:
        """Write a durable scheduler checkpoint when PostgreSQL is enabled."""
        if self.postgres_store is None:
            return None
        context = runtime_state.get("context", {})
        return self.postgres_store.save_checkpoint(
            task_id,
            session_id=context.get("session_id"),
            checkpoint_type=checkpoint_type,
            runtime_state=runtime_state,
        )

    @staticmethod
    def add_unique(runtime_state: dict[str, Any], field: str, node_id: str) -> None:
        """向 runtime_state 的列表字段追加唯一节点 ID。"""
        values = runtime_state.setdefault(field, [])
        if node_id not in values:
            values.append(node_id)

    @staticmethod
    def result_token(node_id: str, result: ExecutionResult) -> str:
        """生成结果去重 token。

        输入：
            node_id 和 worker 写回的 result。

        输出：
            ``node_id:attempt`` 字符串。没有 attempt 时退化为 ``node_id:0``。

        核心计算逻辑：
            worker 每执行一次节点都会递增 attempt；scheduler 用 token 判断某个
            执行结果是否已经推进过 DAG。

        设计意图：
            同一个节点 retry 后仍然写同一个 Redis hash field，attempt 可以避免
            scheduler 把新结果当作旧结果跳过。
        """
        return f"{node_id}:{result.get('attempt', 0)}"

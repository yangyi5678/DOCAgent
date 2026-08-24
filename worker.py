"""独立 worker 进程入口。

worker 负责从 Redis ready queue 中取出 scheduler 推入的 DAG 节点，抢占节点锁，
调用 ToolDispatcher 执行工具，并把执行结果写回 Redis。


scheduler 和独立 worker 的异步协作
worker 通过 redis_infra 连接 Redis，通过 tool_dispatcher 调工具。
Redis 负责传 node 和存 result。
tool_dispatcher 负责把 node["tool"] + node["input"] 变成真实 Python 函数调用。



"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

from env_loader import load_agent_env
from redis_infra import (
    RedisClientFactory,
    RedisLock,
    RedisQueue,
    RedisReadyQueue,
    RedisResultStore,
)
from sandbox.database_policy import validate_database_access_for_node
from sandbox.filesystem_policy import validate_filesystem_access_for_node
from sandbox.network_policy import validate_network_access_for_node
from sandbox.process_policy import validate_process_access_for_node
from sandbox.quota_policy import validate_quota_access_for_node
from sandbox.redaction import redact
from sandbox.resource_limits import run_with_limits
from task_control import TaskControl
from tool_dispatcher import ToolDispatcher


load_agent_env()


class Worker:
    """Redis 队列消费者 worker。

    功能：
        持续从某个 ``task_id`` 对应的 Redis 队列中取出 DAG 节点，使用 Redis
        lock 防止重复执行，然后通过 ``ToolDispatcher`` 调用具体工具。

    输入：
        Redis 队列中的节点字典。节点至少需要包含 ``id`` 和 ``tool``。

    输出：
        每个节点的执行结果会写入 Redis hash：``result:{task_id}``。
    """

    def __init__(
        self,
        worker_id: str,
        redis_client=None,
        dispatcher: ToolDispatcher | None = None,
        idle_sleep_seconds: float = 1.0,
    ):
        """初始化 worker。

        参数：
            worker_id: 当前 worker 的唯一 ID 或名称。
            redis_client: 可选 Redis client。不传时使用默认 Redis 连接。
            dispatcher: 可选工具分发器。不传时创建默认 ``ToolDispatcher``。
            idle_sleep_seconds: 队列为空时的休眠秒数。

        输出：
            无显式返回值。初始化后 worker 拥有 Redis queue、lock 和 dispatcher。
        """
        self.worker_id = worker_id
        self.redis = redis_client or RedisClientFactory().create()
        self.queue = RedisQueue(self.redis)
        self.ready_queue = RedisReadyQueue(self.redis)
        self.lock_manager = RedisLock(self.redis)
        self.result_store = RedisResultStore(self.redis)
        self.task_control = TaskControl(self.redis)
        self.dispatcher = dispatcher or ToolDispatcher()
        self.idle_sleep_seconds = idle_sleep_seconds

    def run_once(self, task_id: str) -> dict[str, Any] | None:
        """执行一次取节点、抢锁、调工具流程。

        参数：
            task_id: 当前任务或运行实例 ID，对应 Redis 队列 key。

        输入：
            Redis 队列中的一个节点 payload。

        输出：
            如果队列为空，返回 ``None``。
            如果取到节点，返回该节点的结构化执行结果。

        功能：
            从队列弹出节点；如果节点已经被其他 worker 加锁，则返回 skipped；
            否则调用 dispatcher 执行工具，并保存执行结果。
        """
        if self.task_is_terminal(task_id):
            return None

        node = self.queue.pop(task_id)
        if node is None:
            return None

        return self.execute_node(task_id, node)

    def run_pool_once(self) -> dict[str, Any] | None:
        """执行一次全局 ready queue 消费流程。"""
        envelope = self.ready_queue.pop()
        if envelope is None:
            return None

        task_id = envelope.get("task_id")
        node = envelope.get("node")
        if not task_id or not isinstance(node, dict):
            return {
                "status": "failed",
                "node_id": None,
                "tool": None,
                "error": "全局 ready queue payload 必须包含 task_id 和 node。",
                "error_stage": "queue_payload_validation",
                "retryable": False,
                "worker_id": self.worker_id,
            }

        return self.execute_node(str(task_id), node)

    def execute_node(self, task_id: str, node: dict[str, Any]) -> dict[str, Any]:
        """执行一个已出队节点，并把结果写回对应 task 的 result store。"""
        if self.task_is_terminal(task_id):
            return {
                "status": "skipped",
                "node_id": node.get("id"),
                "tool": node.get("tool"),
                "reason": "任务已进入终态，节点不再执行。",
                "worker_id": self.worker_id,
            }

        node_id = node.get("id")
        if not node_id:
            result = {
                "status": "failed",
                "node_id": None,
                "tool": node.get("tool"),
                "error": "节点缺少 id 字段。",
                "error_stage": "node_validation",
                "retryable": False,
            }
            self.save_result(task_id, "__missing_node_id__", result)
            return result

        if self.task_is_terminal(task_id):
            return {
                "status": "skipped",
                "node_id": node_id,
                "tool": node.get("tool"),
                "reason": "任务已进入终态，节点不再执行。",
            }

        lock_id = self.lock_id(task_id, str(node_id))
        if not self.lock_manager.acquire(lock_id, self.worker_id):
            return {
                "status": "skipped",
                "node_id": node_id,
                "tool": node.get("tool"),
                "reason": "节点已经被其他 worker 锁定。",
            }

        if self.task_is_terminal(task_id):
            self.lock_manager.release(lock_id, self.worker_id)
            return {
                "status": "skipped",
                "node_id": node_id,
                "tool": node.get("tool"),
                "reason": "任务已进入终态，节点不再执行。",
            }

        attempt = self.result_store.next_attempt(task_id, str(node_id))
        try:
            result = self.validate_executable_node(node)
            if result is None:
                validate_filesystem_access_for_node(node)
                validate_database_access_for_node(node)
                validate_process_access_for_node(node)
                validate_network_access_for_node(node)
                validate_quota_access_for_node(node)
                result = run_with_limits(self.dispatcher, node)
        except Exception as exc:  # noqa: BLE001 - worker must persist execution failure
            result = self.build_failed_result(
                node,
                exc,
                error_stage=self.error_stage_for_exception(exc),
                retryable=self.exception_is_retryable(exc),
            )
        finally:
            self.lock_manager.release(lock_id, self.worker_id)

        result = self.annotate_execution_result(result)
        result["attempt"] = attempt
        result["worker_id"] = self.worker_id
        if self.task_is_terminal(task_id):
            result["discarded"] = True
            result["late_result"] = True
            result["discard_reason"] = (
                "任务已进入终态，当前结果属于迟到结果，不写入 result store，"
                "避免取消、超时或失败后的旧结果继续推进 DAG。"
            )
            return result

        self.save_result(task_id, str(node_id), result)
        return result

    def validate_executable_node(self, node: dict[str, Any]) -> dict[str, Any] | None:
        """Return a non-retryable failure if a queued node is not executable."""
        if node.get("node_type") == "human_interrupt":
            return {
                "status": "failed",
                "node_id": node.get("id"),
                "tool": node.get("tool"),
                "error": "human_interrupt 节点不应进入 worker 执行队列。",
                "error_type": "InvalidExecutableNode",
                "error_stage": "node_validation",
                "retryable": False,
            }
        if not node.get("tool"):
            return {
                "status": "failed",
                "node_id": node.get("id"),
                "tool": None,
                "error": "节点没有可执行 tool。",
                "error_type": "InvalidExecutableNode",
                "error_stage": "node_validation",
                "retryable": False,
            }
        return None

    def build_failed_result(
        self,
        node: dict[str, Any],
        exc: Exception,
        *,
        error_stage: str,
        retryable: bool,
    ) -> dict[str, Any]:
        """Build a categorized worker failure result."""
        return {
            "status": "failed",
            "node_id": node.get("id"),
            "tool": node.get("tool"),
            "error": str(exc),
            "error_type": type(exc).__name__,
            "error_stage": error_stage,
            "retryable": retryable,
        }

    @staticmethod
    def error_stage_for_exception(exc: Exception) -> str:
        if isinstance(exc, PermissionError):
            return "policy_validation"
        if isinstance(exc, (TypeError, ValueError)):
            return "node_validation"
        return "tool_execution"

    @staticmethod
    def exception_is_retryable(exc: Exception) -> bool:
        if isinstance(exc, (PermissionError, TypeError, ValueError)):
            return False
        return True

    def annotate_execution_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Fill error_stage/retryable for dispatcher and resource-limit failures."""
        if result.get("status") != "failed":
            return result

        annotated = dict(result)
        error_type = str(annotated.get("error_type") or "")
        failure_reason = str(annotated.get("failure_reason") or "")

        if "error_stage" not in annotated:
            if error_type == "ResourceLimitExceeded":
                annotated["error_stage"] = "resource_limit"
            else:
                annotated["error_stage"] = "tool_execution"

        if "retryable" not in annotated:
            if annotated["error_stage"] in {"node_validation", "policy_validation"}:
                annotated["retryable"] = False
            elif error_type == "ResourceLimitExceeded":
                annotated["retryable"] = failure_reason in {
                    "timeout",
                    "process_exit",
                    "missing_result",
                }
            elif error_type in {"KeyError", "TypeError", "ValueError"}:
                annotated["retryable"] = False
            else:
                annotated["retryable"] = True

        return annotated

    def run_forever(self, task_id: str) -> None:
        """持续消费一个任务队列。

        参数：
            task_id: 当前任务或运行实例 ID。

        输入：
            Redis 队列中的 DAG 节点。

        输出：
            无显式返回值。方法会持续运行，直到进程被外部停止。

        功能：
            不断调用 ``run_once``。队列为空时短暂 sleep，避免空转占用 CPU。
        """
        while True:
            result = self.run_once(task_id)
            if result is None:
                time.sleep(self.idle_sleep_seconds)
                continue
            print(json.dumps(redact(result), ensure_ascii=False))

    def run_until_idle_timeout(
        self,
        task_id: str,
        idle_timeout_seconds: float,
    ) -> None:
        """持续消费任务队列，队列空闲超过阈值后自动退出。"""
        idle_started_at: float | None = None

        while True:
            result = self.run_once(task_id)
            if result is None:
                if idle_started_at is None:
                    idle_started_at = time.monotonic()
                if time.monotonic() - idle_started_at >= idle_timeout_seconds:
                    return
                time.sleep(self.idle_sleep_seconds)
                continue

            idle_started_at = None
            print(json.dumps(redact(result), ensure_ascii=False))

    def run_pool_forever(self) -> None:
        """持续消费全局 ready queue。"""
        while True:
            result = self.run_pool_once()
            if result is None:
                time.sleep(self.idle_sleep_seconds)
                continue
            print(json.dumps(redact(result), ensure_ascii=False))

    def run_pool_until_idle_timeout(self, idle_timeout_seconds: float) -> None:
        """持续消费全局 ready queue，空闲超过阈值后自动退出。"""
        idle_started_at: float | None = None

        while True:
            result = self.run_pool_once()
            if result is None:
                if idle_started_at is None:
                    idle_started_at = time.monotonic()
                if time.monotonic() - idle_started_at >= idle_timeout_seconds:
                    return
                time.sleep(self.idle_sleep_seconds)
                continue

            idle_started_at = None
            print(json.dumps(redact(result), ensure_ascii=False))

    def save_result(self, task_id: str, node_id: str, result: dict[str, Any]) -> int:
        """保存节点执行结果。

        参数：
            task_id: 当前任务或运行实例 ID。
            node_id: 节点 ID。
            result: 节点执行结果字典。

        输入：
            结构化执行结果。

        输出：
            返回 Redis ``hset`` 的结果。

        功能：
            将结果 JSON 写入 Redis hash ``result:{task_id}``，field 为节点 ID。
        """
        return self.result_store.save(task_id, node_id, result)

    def task_is_terminal(self, task_id: str) -> bool:
        """Return whether the task should no longer execute or accept results."""
        return self.task_control.is_terminal_task(task_id)

    @staticmethod
    def lock_id(task_id: str, node_id: str) -> str:
        """生成任务隔离的节点锁 ID。"""
        return f"{task_id}:{node_id}"


def main() -> None:
    """命令行入口。

    输入：
        命令行参数 ``task_id``、``--worker-id``、``--once``。

    输出：
        将执行结果打印到 stdout，并把结果保存到 Redis。

    功能：
        方便直接启动独立 worker。默认持续运行；传入 ``--once`` 时只消费一个节点。
    """
    parser = argparse.ArgumentParser(description="运行 DAG tool worker")
    parser.add_argument("task_id", nargs="?", help="要消费的任务 ID；--pool 模式不需要。")
    parser.add_argument("--pool", action="store_true", help="消费全局 queue:ready。")
    parser.add_argument("--worker-id", default="worker-1", help="当前 worker ID")
    parser.add_argument("--once", action="store_true", help="只执行一次队列消费")
    parser.add_argument(
        "--idle-timeout-seconds",
        type=float,
        default=None,
        help="队列持续空闲超过该秒数后自动退出；默认一直运行。",
    )
    args = parser.parse_args()

    worker = Worker(worker_id=args.worker_id)
    if args.pool:
        if args.once:
            result = worker.run_pool_once()
            print(json.dumps(redact(result), ensure_ascii=False))
            return
        if args.idle_timeout_seconds is not None:
            worker.run_pool_until_idle_timeout(args.idle_timeout_seconds)
            return
        worker.run_pool_forever()
        return

    if not args.task_id:
        parser.error("非 --pool 模式需要 task_id。")

    if args.once:
        result = worker.run_once(args.task_id)
        print(json.dumps(redact(result), ensure_ascii=False))
        return

    if args.idle_timeout_seconds is not None:
        worker.run_until_idle_timeout(args.task_id, args.idle_timeout_seconds)
        return

    worker.run_forever(args.task_id)


if __name__ == "__main__":
    main()

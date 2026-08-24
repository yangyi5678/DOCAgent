"""Task lifecycle control for agent runs.

This module owns task-level terminal state changes such as cancellation and
timeout. Callers should pass a reason; they should not duplicate the details of
clearing queues, writing runtime state, or recording checkpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from redis_infra import RedisQueue, RedisReadyQueue, RedisTaskStore


TASK_STATUS_RUNNING = "running"
TASK_STATUS_WAITING_FOR_HUMAN = "waiting_for_human"
TASK_STATUS_NEEDS_REPLAN = "needs_replan"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_TIMEOUT = "timeout"
TASK_STATUS_CANCELLED = "cancelled"

TERMINAL_TASK_STATUSES = {
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_TIMEOUT,
    TASK_STATUS_CANCELLED,
}

TASK_CANCEL_USER = "user_cancelled"
TASK_CANCEL_USER_REJECTED = "user_rejected"
TASK_CANCEL_SUPERSEDED = "superseded"
TASK_CANCEL_TIMEOUT = "timeout"
TASK_CANCEL_BUDGET = "budget_exceeded"
TASK_CANCEL_PERMISSION_DENIED = "permission_denied"
TASK_CANCEL_POLICY_VIOLATION = "policy_violation"
TASK_CANCEL_SESSION_CLOSED = "session_closed"
TASK_CANCEL_UPSTREAM_FAILED = "upstream_failed"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_terminal_status(status: str | None) -> bool:
    return status in TERMINAL_TASK_STATUSES


class TaskControl:
    """Single task-level control surface for cancellation and timeout."""

    def __init__(self, redis_client, postgres_store=None):
        self.queue = RedisQueue(redis_client)
        self.ready_queue = RedisReadyQueue(redis_client)
        self.task_store = RedisTaskStore(redis_client)
        self.postgres_store = postgres_store

    def load_runtime_state(self, task_id: str) -> dict[str, Any] | None:
        dag = self.task_store.load_field(task_id, "dag")
        if dag is None:
            return None

        return {
            "dag": dag,
            "indegree": self.task_store.load_field(task_id, "indegree", {}),
            "graph": self.task_store.load_field(task_id, "graph", {}),
            "retry_counter": self.task_store.load_field(task_id, "retry_counter", {}),
            "queued": self.task_store.load_field(task_id, "queued", []),
            "completed": self.task_store.load_field(task_id, "completed", []),
            "failed": self.task_store.load_field(task_id, "failed", []),
            "processed_results": self.task_store.load_field(task_id, "processed_results", []),
            "status": self.task_store.load_field(task_id, "status", TASK_STATUS_RUNNING),
            "cancel_reason": self.task_store.load_field(task_id, "cancel_reason"),
            "cancel_message": self.task_store.load_field(task_id, "cancel_message"),
            "cancelled_at": self.task_store.load_field(task_id, "cancelled_at"),
            "timed_out_at": self.task_store.load_field(task_id, "timed_out_at"),
            "context": self.task_store.load_field(task_id, "context", {}),
            "metadata": self.task_store.load_field(task_id, "metadata", {}),
            "replan_request": self.task_store.load_field(task_id, "replan_request"),
            "waiting_node_id": self.task_store.load_field(task_id, "waiting_node_id"),
            "pending_interrupt_id": self.task_store.load_field(task_id, "pending_interrupt_id"),
            "pending_interrupt": self.task_store.load_field(task_id, "pending_interrupt"),
        }

    def save_runtime_state(self, task_id: str, runtime_state: dict[str, Any]) -> None:
        for field, value in runtime_state.items():
            self.task_store.save_field(task_id, field, value)

    def is_terminal_task(self, task_id: str) -> bool:
        status = self.task_store.load_field(task_id, "status", TASK_STATUS_RUNNING)
        return is_terminal_status(status)

    def cancel_task(
        self,
        task_id: str,
        *,
        reason: str = TASK_CANCEL_USER,
        message: str | None = None,
        clear_queue: bool = True,
    ) -> dict[str, Any]:
        """Cancel a task idempotently.

        Already-running tools are not force-killed here. The contract is:
        queued nodes are removed, future worker checks skip execution, and the
        scheduler will not advance the DAG from late results.
        """
        runtime_state = self.load_runtime_state(task_id)
        if runtime_state is None:
            raise ValueError(f"任务尚未 submit: {task_id}")

        if is_terminal_status(runtime_state.get("status")):
            return runtime_state

        runtime_state["status"] = TASK_STATUS_CANCELLED
        runtime_state["cancel_reason"] = reason
        runtime_state["cancel_message"] = message
        runtime_state["cancelled_at"] = utc_now()
        runtime_state["waiting_node_id"] = None
        runtime_state["pending_interrupt_id"] = None
        runtime_state["pending_interrupt"] = None

        if clear_queue:
            self.queue.clear(task_id)
            self.ready_queue.remove_task(task_id)
            runtime_state["queued"] = []

        self.save_runtime_state(task_id, runtime_state)
        self.log_event(
            "task_cancelled",
            task_id,
            runtime_state,
            level="warning",
            payload={
                "reason": reason,
                "message": message,
                "clear_queue": clear_queue,
            },
        )
        self.save_checkpoint(task_id, runtime_state, "cancelled")
        return runtime_state

    def mark_timeout(
        self,
        task_id: str,
        runtime_state: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Mark a running task as timed out and clear pending work."""
        if is_terminal_status(runtime_state.get("status")):
            return runtime_state

        runtime_state["status"] = TASK_STATUS_TIMEOUT
        runtime_state["cancel_reason"] = TASK_CANCEL_TIMEOUT
        runtime_state["timed_out_at"] = utc_now()
        runtime_state["queued"] = []
        runtime_state["waiting_node_id"] = None
        runtime_state["pending_interrupt_id"] = None
        runtime_state["pending_interrupt"] = None
        self.queue.clear(task_id)
        self.ready_queue.remove_task(task_id)
        self.save_runtime_state(task_id, runtime_state)
        self.log_event(
            "scheduler_timeout",
            task_id,
            runtime_state,
            level="warning",
            payload={"timeout_seconds": timeout_seconds},
        )
        self.save_checkpoint(task_id, runtime_state, "timeout")
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
        if self.postgres_store is None:
            return None
        context = runtime_state.get("context", {})
        return self.postgres_store.save_checkpoint(
            task_id,
            session_id=context.get("session_id"),
            checkpoint_type=checkpoint_type,
            runtime_state=runtime_state,
        )

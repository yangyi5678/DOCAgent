"""Agent runtime entrypoint.

This file wires the project modules into the real user-facing flow:

    user input -> goal parser -> planner -> scheduler submit

Workers still run as separate processes and consume the Redis queue for the
submitted task.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

from context_manager import ContextManager
from env_loader import load_agent_env
from goalparser import goal_parser
from planner import build_plan
from postgres_store import get_postgres_store_from_env
from redis_infra import RedisClientFactory
from sandbox.redaction import redact
from scheduler import Scheduler
from task_control import TASK_CANCEL_USER, TaskControl
from worker_manager import worker_manager


load_agent_env()


def build_initial_state(
    user_input: str,
    task_id: str,
    session_id: str,
    context: dict[str, Any] | None = None,
    context_manager: ContextManager | None = None,
) -> dict[str, Any]:
    """Create the state object passed through goal parser and planner."""
    state = {
        "task_id": task_id,
        "session_id": session_id,
        "user_input": user_input,
        "context": context or {},
        "goal": None,
        "dag": None,
    }
    if context_manager is not None:
        state["context_manager"] = context_manager
    return state


def compile_user_input_to_dag(
    user_input: str,
    task_id: str,
    session_id: str,
    context: dict[str, Any] | None = None,
    context_manager: ContextManager | None = None,
) -> dict[str, Any]:
    """Parse user input and compile it into an executable DAG."""
    state = build_initial_state(
        user_input=user_input,
        task_id=task_id,
        session_id=session_id,
        context=context,
        context_manager=context_manager,
    )
    state = goal_parser(state)
    state = build_plan(state)

    if not state.get("dag"):
        raise RuntimeError("planner 未生成 dag，无法提交给 scheduler。")

    return state


def submit_agent_task(
    user_input: str,
    task_id: str,
    session_id: str,
    wait: bool = False,
    poll_interval_seconds: float = 1.0,
    timeout_seconds: float | None = None,
    reset: bool = True,
    start_workers: bool = True,
    worker_count: int | None = None,
) -> dict[str, Any]:
    """Compile user input and submit the DAG to Redis scheduler."""
    postgres_store = get_postgres_store_from_env(setup=True)
    context_manager = ContextManager(postgres_store=postgres_store)
    context = context_manager.start_turn(
        session_id,
        user_input,
        task_id=task_id,
    )
    if postgres_store is not None:
        postgres_store.ensure_session(session_id)
        postgres_store.add_message(
            session_id,
            "user",
            user_input,
            task_id=task_id,
        )
        postgres_store.log_event(
            "task_received",
            session_id=session_id,
            task_id=task_id,
            payload={"user_input": user_input},
        )

    state = compile_user_input_to_dag(
        user_input=user_input,
        task_id=task_id,
        session_id=session_id,
        context=context,
        context_manager=context_manager,
    )

    dag = state["dag"]
    if postgres_store is not None:
        postgres_store.save_plan(
            session_id,
            task_id,
            goal=state.get("goal"),
            dag=dag,
            capability_dag=state.get("step_capability_dag"),
            tool_bindings=state.get("tool_bindings"),
            argument_bindings=state.get("argument_bindings"),
        )
        postgres_store.save_checkpoint(
            task_id,
            session_id=session_id,
            checkpoint_type="planned",
        )

    scheduler = Scheduler(postgres_store=postgres_store)
    runtime_state = scheduler.submit(
        task_id=task_id,
        dag=dag,
        reset=reset,
        context=state.get("context", {}),
    )
    worker_runtime = None
    if start_workers:
        worker_runtime = worker_manager.ensure_pool_started(
            worker_count=worker_count,
        )

    if wait:
        try:
            runtime_state = scheduler.run(
                task_id=task_id,
                dag=None,
                context=state.get("context", {}),
                poll_interval_seconds=poll_interval_seconds,
                timeout_seconds=timeout_seconds,
                reset=False,
            )
        finally:
            if runtime_state.get("status") in {"completed", "failed", "timeout", "cancelled"}:
                worker_manager.cleanup_finished()

    return {
        "task_id": task_id,
        "session_id": session_id,
        "user_input": user_input,
        "goal": state.get("goal"),
        "dag": dag,
        "runtime_state": runtime_state,
        "worker_runtime": worker_runtime,
        "worker_command": None,
    }


def cancel_agent_task(
    task_id: str,
    *,
    reason: str = TASK_CANCEL_USER,
    message: str | None = None,
) -> dict[str, Any]:
    """Cancel a submitted agent task and clear pending work."""
    postgres_store = get_postgres_store_from_env(setup=True)
    task_control = TaskControl(
        RedisClientFactory().create(),
        postgres_store=postgres_store,
    )
    runtime_state = task_control.cancel_task(
        task_id,
        reason=reason,
        message=message,
        clear_queue=True,
    )
    worker_manager.cleanup_finished()
    return {
        "task_id": task_id,
        "status": runtime_state.get("status"),
        "cancel_reason": runtime_state.get("cancel_reason"),
        "cancel_message": runtime_state.get("cancel_message"),
        "runtime_state": runtime_state,
        "worker_runtime": None,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent 入口：读取用户输入并提交 DAG 任务")
    parser.add_argument(
        "user_input",
        nargs="?",
        help="用户自然语言输入；不传时从 stdin 读取一行。",
    )
    parser.add_argument(
        "--task-id",
        default=None,
        help="任务 ID；默认自动生成。",
    )
    parser.add_argument(
        "--session-id",
        default="default-session",
        help="会话 ID，用于后续多轮上下文扩展。",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="提交后自动启动 worker 并持续轮询结果。",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="--wait 模式下的轮询间隔秒数。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="--wait 模式下的超时时间秒数。",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="提交时不清空同 task_id 的旧 Redis 状态。",
    )
    parser.add_argument(
        "--no-start-workers",
        action="store_true",
        help="提交任务后不自动启动 worker。",
    )
    parser.add_argument(
        "--worker-count",
        type=int,
        default=None,
        help="全局 worker pool 目标数量；默认读取 WORKER_POOL_SIZE。",
    )
    parser.add_argument(
        "--cancel-task",
        default=None,
        help="取消一个已提交任务；传入 task_id 后不会提交新任务。",
    )
    parser.add_argument(
        "--cancel-reason",
        default="user_cancelled",
        help="取消原因，例如 user_cancelled、superseded、budget_exceeded。",
    )
    parser.add_argument(
        "--cancel-message",
        default=None,
        help="取消说明，会写入 runtime_state 和事件日志。",
    )
    return parser.parse_args()


def read_user_input(args: argparse.Namespace) -> str:
    if args.user_input:
        return args.user_input.strip()

    if not sys.stdin.isatty():
        return sys.stdin.read().strip()

    return input("User: ").strip()


def main() -> None:
    args = parse_args()
    if args.cancel_task:
        result = cancel_agent_task(
            args.cancel_task,
            reason=args.cancel_reason,
            message=args.cancel_message,
        )
        print(json.dumps(redact(result), ensure_ascii=False, indent=2))
        return

    user_input = read_user_input(args)
    if not user_input:
        raise SystemExit("user_input 不能为空。")

    task_id = args.task_id or f"task-{uuid.uuid4().hex[:12]}"
    try:
        result = submit_agent_task(
            user_input=user_input,
            task_id=task_id,
            session_id=args.session_id,
            wait=args.wait,
            poll_interval_seconds=args.poll_interval,
            timeout_seconds=args.timeout,
            reset=not args.no_reset,
            start_workers=not args.no_start_workers,
            worker_count=args.worker_count,
        )
    except KeyboardInterrupt:
        try:
            cancel_agent_task(
                task_id,
                reason=TASK_CANCEL_USER,
                message="CLI received Ctrl+C.",
            )
        except Exception:
            pass
        raise SystemExit("任务已取消。")
    print(json.dumps(redact(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

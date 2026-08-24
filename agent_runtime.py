"""Runtime orchestration for resuming and replanning agent tasks."""

from __future__ import annotations

import json
import os
from typing import Any

from app import (
    cancel_agent_task as _app_cancel_agent_task,
    compile_user_input_to_dag,
    submit_agent_task as _app_submit_agent_task,
)
from context_manager import ContextManager
from env_loader import load_agent_env
from postgres_store import get_postgres_store_from_env
from redis_infra import RedisClientFactory
from scheduler import Scheduler
from task_control import (
    TASK_CANCEL_USER,
    TASK_CANCEL_USER_REJECTED,
    TASK_STATUS_NEEDS_REPLAN,
    TaskControl,
)
from worker_manager import worker_manager


load_agent_env()

MAX_REPLAN_COUNT = int(os.getenv("AGENT_MAX_REPLAN_COUNT", "3"))


def submit_agent_task(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility runtime entry for submitting a new task."""
    return _app_submit_agent_task(*args, **kwargs)


def cancel_agent_task(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility runtime entry for cancelling a task."""
    return _app_cancel_agent_task(*args, **kwargs)


def resume_agent_task(
    *,
    task_id: str,
    session_id: str,
    resolution_payload: dict[str, Any] | None = None,
    interrupt_id: str | None = None,
    start_workers: bool = True,
) -> dict[str, Any]:
    """Resume an interrupted task, replanning when the interrupt requires it."""
    payload = resolution_payload or {}
    postgres_store = get_postgres_store_from_env(setup=True)
    response_message_id = _record_interrupt_response(
        postgres_store,
        session_id,
        task_id,
        payload,
    )
    scheduler = Scheduler(postgres_store=postgres_store)
    runtime_state = scheduler.load_runtime_state(task_id)
    if runtime_state is None:
        raise ValueError(f"任务尚未 submit: {task_id}")

    pending_interrupt = runtime_state.get("pending_interrupt") or {}
    action = decide_resume_action(
        pending_interrupt=pending_interrupt,
        resolution_payload=payload,
    )

    if action["action"] == "replan":
        return replan_agent_task(
            task_id=task_id,
            session_id=session_id,
            replan_reason=action.get("reason") or "interrupt_requested_replan",
            replan_context={
                "source": "interrupt_resume",
                "pending_interrupt": pending_interrupt,
                "resolution_payload": payload,
            },
            interrupt_id=interrupt_id,
            response_message_id=response_message_id,
            start_workers=start_workers,
        )

    if action["action"] == "cancel":
        task_control = TaskControl(
            RedisClientFactory().create(),
            postgres_store=postgres_store,
        )
        cancelled = task_control.cancel_task(
            task_id,
            reason=TASK_CANCEL_USER_REJECTED,
            message=payload.get("reason") or payload.get("content") or "user cancelled interrupt",
            clear_queue=True,
        )
        return {
            "mode": "cancel",
            "task_id": task_id,
            "session_id": session_id,
            "runtime_state": cancelled,
            "worker_runtime": None,
        }

    if action["action"] == "skip_step":
        runtime_state = _skip_interrupted_target_step(
            scheduler=scheduler,
            task_id=task_id,
            interrupt_id=interrupt_id,
            resolution_payload=payload,
            response_message_id=response_message_id,
        )
    else:
        runtime_state = scheduler.resume_from_interrupt(
            task_id=task_id,
            interrupt_id=interrupt_id,
            resolution_payload=payload,
            response_message_id=response_message_id,
        )

    worker_runtime = None
    if start_workers and runtime_state.get("status") == "running":
        worker_runtime = worker_manager.ensure_pool_started()

    return {
        "mode": action["action"],
        "task_id": task_id,
        "session_id": session_id,
        "runtime_state": runtime_state,
        "worker_runtime": worker_runtime,
    }


def maybe_auto_replan_task(
    *,
    task_id: str,
    session_id: str,
    start_workers: bool = True,
) -> dict[str, Any]:
    """Automatically replan a task that the scheduler marked needs_replan."""
    postgres_store = get_postgres_store_from_env(setup=True)
    scheduler = Scheduler(postgres_store=postgres_store)
    runtime_state = scheduler.load_runtime_state(task_id)
    if runtime_state is None:
        raise ValueError(f"任务尚未 submit: {task_id}")
    if runtime_state.get("status") != TASK_STATUS_NEEDS_REPLAN:
        return {
            "mode": "noop",
            "task_id": task_id,
            "session_id": session_id,
            "runtime_state": runtime_state,
            "worker_runtime": None,
        }

    return replan_agent_task(
        task_id=task_id,
        session_id=session_id,
        replan_reason=(runtime_state.get("replan_request") or {}).get(
            "reason",
            "automatic_replan_requested",
        ),
        replan_context={
            "source": "automatic_replan",
            "replan_request": runtime_state.get("replan_request"),
        },
        start_workers=start_workers,
    )


def replan_agent_task(
    *,
    task_id: str,
    session_id: str,
    replan_reason: str,
    replan_context: dict[str, Any] | None = None,
    interrupt_id: str | None = None,
    response_message_id: str | None = None,
    start_workers: bool = True,
) -> dict[str, Any]:
    """Compile a new DAG and replace the current task runtime state."""
    postgres_store = get_postgres_store_from_env(setup=True)
    scheduler = Scheduler(postgres_store=postgres_store)
    old_runtime_state = scheduler.load_runtime_state(task_id)
    if old_runtime_state is None:
        raise ValueError(f"任务尚未 submit: {task_id}")

    metadata = dict(old_runtime_state.get("metadata") or {})
    replan_count = int(metadata.get("replan_count") or 0)
    if replan_count >= MAX_REPLAN_COUNT:
        task_control = TaskControl(
            RedisClientFactory().create(),
            postgres_store=postgres_store,
        )
        cancelled = task_control.cancel_task(
            task_id,
            reason="replan_limit_exceeded",
            message=f"自动/恢复 replan 次数超过限制 {MAX_REPLAN_COUNT}。",
            clear_queue=True,
        )
        return {
            "mode": "cancel",
            "task_id": task_id,
            "session_id": session_id,
            "runtime_state": cancelled,
            "worker_runtime": None,
        }

    context_manager = ContextManager(postgres_store=postgres_store)
    replan_input = build_replan_input(
        runtime_state=old_runtime_state,
        replan_reason=replan_reason,
        replan_context=replan_context or {},
    )
    context = context_manager.start_turn(
        session_id,
        replan_input,
        task_id=task_id,
    )

    state = compile_user_input_to_dag(
        user_input=replan_input,
        task_id=task_id,
        session_id=session_id,
        context=context,
        context_manager=context_manager,
    )
    dag = state["dag"]
    dag.setdefault("metadata", {})
    history = list(metadata.get("replan_history") or [])
    history.append({
        "reason": replan_reason,
        "context": replan_context or {},
    })
    dag["metadata"]["runtime_metadata"] = {
        **metadata,
        "replan_count": replan_count + 1,
        "replan_history": history[-10:],
    }

    if postgres_store is not None:
        postgres_store.ensure_session(session_id)
        postgres_store.add_message(
            session_id,
            "user",
            replan_input,
            task_id=task_id,
            metadata={
                "message_type": "replan_input",
                "replan_reason": replan_reason,
            },
        )
        postgres_store.save_plan(
            session_id,
            task_id,
            goal=state.get("goal"),
            dag=dag,
            capability_dag=state.get("step_capability_dag"),
            tool_bindings=state.get("tool_bindings"),
            argument_bindings=state.get("argument_bindings"),
        )
        postgres_store.log_event(
            "task_replanned",
            session_id=session_id,
            task_id=task_id,
            payload={
                "reason": replan_reason,
                "replan_count": replan_count + 1,
                "replan_context": replan_context or {},
            },
        )
        if interrupt_id is not None:
            postgres_store.resolve_interrupt(
                interrupt_id,
                status="resolved",
                resolution_payload={
                    "decision": "replan",
                    "reason": replan_reason,
                    "context": replan_context or {},
                },
                response_message_id=response_message_id,
            )

    new_runtime_state = scheduler.submit(
        task_id=task_id,
        dag=dag,
        reset=True,
        context=state.get("context", {}),
    )

    worker_runtime = None
    if start_workers and new_runtime_state.get("status") == "running":
        worker_runtime = worker_manager.ensure_pool_started()

    return {
        "mode": "replan",
        "task_id": task_id,
        "session_id": session_id,
        "goal": state.get("goal"),
        "dag": dag,
        "runtime_state": new_runtime_state,
        "worker_runtime": worker_runtime,
    }


def decide_resume_action(
    *,
    pending_interrupt: dict[str, Any],
    resolution_payload: dict[str, Any],
) -> dict[str, str]:
    """Decide whether an interrupt response resumes, replans, skips, or cancels."""
    explicit_action = _extract_requested_action(resolution_payload)
    if explicit_action in {"replan", "skip_step", "cancel", "resume"}:
        return {"action": explicit_action, "reason": "explicit_user_action"}

    decision = str(
        resolution_payload.get("decision")
        or resolution_payload.get("status")
        or "approve"
    ).lower()
    if decision in {"reject", "rejected", "deny", "denied", "cancel", "cancelled"}:
        return {"action": "cancel", "reason": "user_rejected_interrupt"}

    interrupt_type = pending_interrupt.get("interrupt_type")
    reason = pending_interrupt.get("reason")
    if reason == "goal_needs_clarification":
        return {"action": "replan", "reason": reason}
    if reason == "step_capability_has_no_bound_tool":
        return {"action": "replan", "reason": reason}
    if reason == "tool_arguments_need_clarification":
        llm_action = _llm_decide_resume_action(pending_interrupt, resolution_payload)
        if llm_action:
            return llm_action
        return {"action": "resume", "reason": reason}
    if interrupt_type == "approval":
        return {"action": "resume", "reason": reason or "approval_granted"}

    llm_action = _llm_decide_resume_action(pending_interrupt, resolution_payload)
    return llm_action or {"action": "resume", "reason": reason or "default_resume"}


def build_replan_input(
    *,
    runtime_state: dict[str, Any],
    replan_reason: str,
    replan_context: dict[str, Any],
) -> str:
    """Build the natural-language input used for parser/planner replanning."""
    pending = runtime_state.get("pending_interrupt") or {}
    context_payload = pending.get("context_payload") or {}
    old_goal = context_payload.get("goal") or {}
    context = runtime_state.get("context") or {}
    recent_messages = context.get("recent_messages") or context.get("messages") or []
    last_user_message = _last_user_message(recent_messages)
    original_goal = old_goal.get("text") or last_user_message or ""

    payload = replan_context.get("resolution_payload") or {}
    content = (
        payload.get("content")
        or payload.get("reason")
        or (payload.get("form_data") or {}).get("instruction")
        or ""
    )
    form_data = payload.get("form_data") or {}
    action = payload.get("action") or form_data.get("action")

    replan_request = replan_context.get("replan_request") or {}
    target = (pending.get("context_payload") or {}).get("target_node_id") or replan_request.get("node_id")

    parts = [
        "请基于以下上下文重新规划任务，不要沿用已经失效的执行 DAG。",
    ]
    if original_goal:
        parts.append(f"原始目标：{original_goal}")
    parts.append(f"重规划原因：{replan_reason}")
    if target:
        parts.append(f"相关节点：{target}")
    if pending:
        parts.append("中断信息：" + json.dumps(pending, ensure_ascii=False))
    if replan_request:
        parts.append("执行观察/失败信息：" + json.dumps(replan_request, ensure_ascii=False))
    if action:
        parts.append(f"用户选择的恢复动作：{action}")
    if content:
        parts.append(f"用户补充/替代要求：{content}")
    if form_data:
        parts.append("用户表单输入：" + json.dumps(form_data, ensure_ascii=False))
    completed = runtime_state.get("completed") or []
    failed = runtime_state.get("failed") or []
    if completed:
        parts.append("已完成节点：" + ", ".join(str(node_id) for node_id in completed))
    if failed:
        parts.append("已失败节点：" + ", ".join(str(node_id) for node_id in failed))
    parts.append("请选择当前可用工具和更稳妥的步骤重新生成可执行计划。")
    return "\n".join(parts)


def _skip_interrupted_target_step(
    *,
    scheduler: Scheduler,
    task_id: str,
    interrupt_id: str | None,
    resolution_payload: dict[str, Any],
    response_message_id: str | None,
) -> dict[str, Any]:
    old_runtime_state = scheduler.load_runtime_state(task_id)
    pending = (old_runtime_state or {}).get("pending_interrupt") or {}
    context_payload = pending.get("context_payload") or {}
    target_node_id = context_payload.get("target_node_id")

    runtime_state = scheduler.resume_from_interrupt(
        task_id=task_id,
        interrupt_id=interrupt_id,
        resolution_payload=resolution_payload,
        response_message_id=response_message_id,
    )
    if not target_node_id:
        return runtime_state

    queued = runtime_state.get("queued", [])
    if target_node_id in queued:
        queued.remove(target_node_id)
    scheduler.ready_queue.remove_task(task_id)
    dag = runtime_state.get("dag") or {}
    node_map = dag.get("node_map") or {}
    for queued_node_id in list(queued):
        queued_node = node_map.get(queued_node_id)
        if queued_node:
            scheduler.ready_queue.push(task_id, queued_node)
    scheduler.advance_from_result(
        task_id,
        runtime_state,
        target_node_id,
        {
            "status": "success",
            "result": {"skipped": True, "reason": "user_requested_skip_step"},
            "source": "human_interrupt_skip",
        },
    )
    scheduler.refresh_task_status(runtime_state)
    scheduler.save_runtime_state(task_id, runtime_state)
    scheduler.save_checkpoint(task_id, runtime_state, "human_interrupt_skip_step")
    return runtime_state


def _extract_requested_action(payload: dict[str, Any]) -> str | None:
    form_data = payload.get("form_data") or {}
    raw_action = payload.get("action") or form_data.get("action")
    if raw_action is None:
        return None
    action = str(raw_action).strip().lower()
    aliases = {
        "skip": "skip_step",
        "skip-step": "skip_step",
        "continue": "resume",
        "approve": "resume",
        "reject": "cancel",
    }
    return aliases.get(action, action)


def _record_interrupt_response(
    postgres_store,
    session_id: str,
    task_id: str,
    payload: dict[str, Any],
) -> str | None:
    content = str(payload.get("content") or payload.get("reason") or "").strip()
    form_data = payload.get("form_data") or {}
    if not content and form_data:
        content = json.dumps(form_data, ensure_ascii=False)
    if postgres_store is None or not content:
        return None
    return postgres_store.add_message(
        session_id,
        "user",
        content,
        task_id=task_id,
        metadata={
            "message_type": "interrupt_resolution",
            "decision": payload.get("decision"),
            "action": payload.get("action") or form_data.get("action"),
        },
    )
"""  """

def _llm_decide_resume_action(
    pending_interrupt: dict[str, Any],
    resolution_payload: dict[str, Any],
) -> dict[str, str] | None:
    content = str(resolution_payload.get("content") or "").strip()
    if not content:
        return None

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        return None

    try:
        client = OpenAI(
            api_key=api_key,
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
        response = client.chat.completions.create(
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Decide how an agent should handle an interrupt response. "
                        "Return JSON only: {\"action\":\"resume|replan|cancel|skip_step\","
                        "\"reason\":\"short reason\",\"confidence\":0.0}. "
                        "Use replan when the user changes the goal or asks for another approach."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "pending_interrupt": pending_interrupt,
                            "resolution_payload": resolution_payload,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.choices[0].message.content or "{}")
    except Exception:
        return None

    action = str(parsed.get("action") or "").strip().lower()
    if action not in {"resume", "replan", "cancel", "skip_step"}:
        return None
    confidence = float(parsed.get("confidence") or 0.0)
    if confidence < 0.65:
        return None
    return {
        "action": action,
        "reason": str(parsed.get("reason") or "llm_resume_decision"),
    }


def _last_user_message(messages: list[Any]) -> str:
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user" and message.get("content"):
            return str(message["content"])
    return ""

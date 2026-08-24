"""WebSocket UI server for the Agent runtime.

Run with:

    uvicorn web_server:app --reload --host 127.0.0.1 --port 8000

The browser talks to this server through one WebSocket:

    /ws/{session_id}

Client messages:
    {"type": "user_message", "content": "..."}
    {"type": "cancel_task", "task_id": "...", "reason": "user_cancelled"}
    {"type": "resume_interrupt", "task_id": "...", "decision": "approve", "content": "..."}
    {"type": "resume_interrupt", "task_id": "...", "decision": "reject", "reason": "..."}

Server messages are structured event objects that the frontend can render
without parsing human text.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from env_loader import load_agent_env

load_agent_env()

from agent_runtime import (
    cancel_agent_task,
    maybe_auto_replan_task,
    resume_agent_task,
    submit_agent_task,
)
from postgres_store import get_postgres_store_from_env
from runtime_command import RUNTIME_COMMAND_CANCEL_TASK, is_cancel_phrase, parse_runtime_command
from sandbox.redaction import redact
from scheduler import Scheduler
from task_control import TASK_CANCEL_SUPERSEDED, TASK_CANCEL_USER
from worker_manager import worker_manager


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "web_static"

app = FastAPI(title="Agent Runtime WebSocket UI")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def now_ms() -> int:
    return int(time.time() * 1000)


def make_event(event_type: str, **payload: Any) -> dict[str, Any]:
    return redact({
        "type": event_type,
        "ts": now_ms(),
        **payload,
    })


def summarize_runtime_state(runtime_state: dict[str, Any] | None) -> dict[str, Any]:
    if not runtime_state:
        return {}

    dag = runtime_state.get("dag") or {}
    nodes = dag.get("nodes") or []
    completed = set(runtime_state.get("completed") or [])
    failed = set(runtime_state.get("failed") or [])
    queued = set(runtime_state.get("queued") or [])
    waiting_node_id = runtime_state.get("waiting_node_id")

    node_statuses = []
    for node in nodes:
        node_id = node.get("id")
        if node_id in completed:
            status = "completed"
        elif node_id in failed:
            status = "failed"
        elif node_id == waiting_node_id:
            status = "waiting_user"
        elif node_id in queued:
            status = "queued"
        else:
            status = "pending"
        node_statuses.append({
            "id": node_id,
            "status": status,
            "capability": node.get("capability"),
            "tool": node.get("tool"),
            "description": node.get("description"),
            "node_type": node.get("node_type"),
        })

    return {
        "status": runtime_state.get("status"),
        "completed": list(completed),
        "failed": list(failed),
        "queued": list(queued),
        "waiting_node_id": waiting_node_id,
        "pending_interrupt": runtime_state.get("pending_interrupt"),
        "metadata": runtime_state.get("metadata", {}),
        "replan_request": runtime_state.get("replan_request"),
        "node_statuses": node_statuses,
    }


class WebSocketSession:
    def __init__(self, websocket: WebSocket, session_id: str):
        self.websocket = websocket
        self.session_id = session_id
        self.send_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task[Any]] = set()
        self.active_task_id: str | None = None

    async def send(self, event_type: str, **payload: Any) -> None:
        async with self.send_lock:
            await self.websocket.send_json(make_event(event_type, **payload))

    def spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def close_tasks(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


async def run_blocking(func: Any, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(func, *args, **kwargs)


async def handle_user_message(connection: WebSocketSession, message: dict[str, Any]) -> None:
    content = str(message.get("content") or "").strip()
    if not content:
        await connection.send("error", message="输入不能为空。")
        return

    runtime_command = parse_runtime_command(
        content,
        active_task_id=connection.active_task_id,
    )
    if runtime_command and runtime_command.command_type == RUNTIME_COMMAND_CANCEL_TASK:
        await handle_cancel_task(
            connection,
            {
                "task_id": runtime_command.task_id,
                "reason": runtime_command.reason,
                "message": runtime_command.message,
            },
        )
        return

    if is_cancel_phrase(content):
        if not connection.active_task_id:
            await connection.send("agent_status", status="idle", message="当前没有正在运行的任务。")
            return

    task_id = str(message.get("task_id") or f"task-{uuid.uuid4().hex[:12]}")
    previous_task_id = connection.active_task_id
    if previous_task_id and previous_task_id != task_id:
        try:
            previous_result = await run_blocking(
                cancel_agent_task,
                previous_task_id,
                reason=TASK_CANCEL_SUPERSEDED,
                message=f"新任务 {task_id} 替代旧任务。",
            )
            previous_state = previous_result.get("runtime_state") or {}
            if previous_state.get("status") == "cancelled":
                await connection.send(
                    "task_cancelled",
                    task_id=previous_task_id,
                    reason=previous_state.get("cancel_reason"),
                    message=previous_state.get("cancel_message"),
                    runtime_state=summarize_runtime_state(previous_state),
                )
        except Exception:
            pass

    connection.active_task_id = task_id
    await connection.send(
        "user_message",
        task_id=task_id,
        session_id=connection.session_id,
        content=content,
    )
    await connection.send("agent_status", task_id=task_id, status="planning", message="正在解析目标并生成执行图。")

    try:
        result = await run_blocking(
            submit_agent_task,
            user_input=content,
            task_id=task_id,
            session_id=connection.session_id,
            wait=False,
            reset=True,
        )
    except Exception as exc:  # noqa: BLE001 - send backend failure to UI
        await connection.send("error", task_id=task_id, message=str(exc))
        return

    runtime_summary = summarize_runtime_state(result.get("runtime_state"))
    await connection.send(
        "task_submitted",
        task_id=task_id,
        session_id=connection.session_id,
        goal=result.get("goal"),
        dag=result.get("dag"),
        runtime_state=runtime_summary,
        worker_runtime=result.get("worker_runtime"),
    )
    connection.spawn(monitor_task(connection, task_id))


async def handle_resume_interrupt(connection: WebSocketSession, message: dict[str, Any]) -> None:
    task_id = str(message.get("task_id") or "").strip()
    if not task_id:
        await connection.send("error", message="恢复中断需要 task_id。")
        return

    decision = str(message.get("decision") or "approve").strip().lower()
    content = str(message.get("content") or "").strip()
    reason = str(message.get("reason") or "").strip()
    form_data = message.get("form_data") or {}
    interrupt_id = message.get("interrupt_id")

    payload = {
        "decision": decision,
        "content": content,
        "reason": reason,
        "form_data": form_data,
        "source": "websocket_ui",
    }

    await connection.send(
        "agent_status",
        task_id=task_id,
        status="resuming",
        message="已收到用户确认，正在恢复任务。",
    )

    try:
        result = await run_blocking(
            resume_agent_task,
            task_id=task_id,
            session_id=connection.session_id,
            interrupt_id=interrupt_id,
            resolution_payload=payload,
        )
        runtime_state = result.get("runtime_state", {})
        worker_runtime = result.get("worker_runtime")
    except Exception as exc:  # noqa: BLE001
        await connection.send("error", task_id=task_id, message=str(exc))
        return

    await connection.send(
        "interrupt_resolved",
        task_id=task_id,
        decision=decision,
        mode=result.get("mode"),
        runtime_state=summarize_runtime_state(runtime_state),
        worker_runtime=worker_runtime,
    )
    connection.spawn(monitor_task(connection, task_id))


async def handle_cancel_task(connection: WebSocketSession, message: dict[str, Any]) -> None:
    task_id = str(message.get("task_id") or "").strip()
    if not task_id:
        await connection.send("error", message="取消任务需要 task_id。")
        return

    reason = str(message.get("reason") or TASK_CANCEL_USER).strip() or TASK_CANCEL_USER
    cancel_message = str(message.get("message") or "").strip() or None
    await connection.send(
        "agent_status",
        task_id=task_id,
        status="cancelling",
        message="正在取消任务并清理待执行节点。",
    )

    try:
        result = await run_blocking(
            cancel_agent_task,
            task_id,
            reason=reason,
            message=cancel_message,
        )
    except Exception as exc:  # noqa: BLE001
        await connection.send("error", task_id=task_id, message=str(exc))
        return

    runtime_state = result.get("runtime_state") or {}
    if connection.active_task_id == task_id:
        connection.active_task_id = None
    await connection.send(
        "task_cancelled",
        task_id=task_id,
        reason=runtime_state.get("cancel_reason"),
        message=runtime_state.get("cancel_message"),
        runtime_state=summarize_runtime_state(runtime_state),
        worker_runtime=result.get("worker_runtime"),
    )


async def monitor_task(connection: WebSocketSession, task_id: str) -> None:
    """Poll scheduler state and push runtime changes to the browser."""
    postgres_store = get_postgres_store_from_env(setup=False)
    scheduler = Scheduler(postgres_store=postgres_store)
    last_snapshot = ""
    interrupt_sent_for: str | None = None

    while True:
        try:
            runtime_state = await run_blocking(scheduler.poll_results_and_advance, task_id)
        except Exception as exc:  # noqa: BLE001
            await connection.send("error", task_id=task_id, message=str(exc))
            return

        summary = summarize_runtime_state(runtime_state)
        snapshot = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        if snapshot != last_snapshot:
            await connection.send("runtime_state", task_id=task_id, runtime_state=summary)
            last_snapshot = snapshot

        status = runtime_state.get("status")
        if status == "needs_replan":
            await connection.send(
                "agent_status",
                task_id=task_id,
                status="replanning",
                message="当前执行图需要调整，正在自动重新规划。",
            )
            try:
                replan_result = await run_blocking(
                    maybe_auto_replan_task,
                    task_id=task_id,
                    session_id=connection.session_id,
                )
            except Exception as exc:  # noqa: BLE001
                await connection.send("error", task_id=task_id, message=str(exc))
                return
            runtime_state = replan_result.get("runtime_state", runtime_state)
            summary = summarize_runtime_state(runtime_state)
            await connection.send(
                "task_replanned",
                task_id=task_id,
                runtime_state=summary,
                worker_runtime=replan_result.get("worker_runtime"),
            )
            last_snapshot = ""
            continue

        pending_interrupt = runtime_state.get("pending_interrupt")
        pending_interrupt_id = runtime_state.get("pending_interrupt_id")
        if status == "waiting_for_human" and pending_interrupt:
            interrupt_key = pending_interrupt_id or pending_interrupt.get("node_id")
            if interrupt_key != interrupt_sent_for:
                await connection.send(
                    "interrupt",
                    task_id=task_id,
                    interrupt_id=pending_interrupt_id,
                    interrupt=pending_interrupt,
                    runtime_state=summary,
                )
                interrupt_sent_for = interrupt_key
            return

        if status in {"completed", "failed", "timeout", "cancelled"}:
            await run_blocking(worker_manager.cleanup_finished)
            if connection.active_task_id == task_id:
                connection.active_task_id = None
            await connection.send(
                "task_finished",
                task_id=task_id,
                status=status,
                runtime_state=summary,
            )
            return

        await asyncio.sleep(1)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    connection = WebSocketSession(websocket, session_id)
    await connection.send("connected", session_id=session_id, message="WebSocket 已连接。")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await connection.send("error", message="WebSocket 消息必须是 JSON。")
                continue

            message_type = message.get("type")
            if message_type == "user_message":
                connection.spawn(handle_user_message(connection, message))
            elif message_type == "cancel_task":
                connection.spawn(handle_cancel_task(connection, message))
            elif message_type == "resume_interrupt":
                connection.spawn(handle_resume_interrupt(connection, message))
            elif message_type == "ping":
                await connection.send("pong")
            else:
                await connection.send("error", message=f"未知消息类型: {message_type}")
    except WebSocketDisconnect:
        await connection.close_tasks()

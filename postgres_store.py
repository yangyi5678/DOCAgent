"""PostgreSQL persistence layer for agent sessions and execution history.

Redis is still the short-lived coordination layer: queue, locks, and fast
runtime state. PostgreSQL is the durable system of record: sessions, messages,
events, checkpoints, plans, and tool calls.
"""

from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import uuid4

from sandbox.redaction import redact

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    title TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    task_id TEXT,
    turn_index INTEGER,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content TEXT NOT NULL,
    token_count INTEGER,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS turn_index INTEGER;

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS token_count INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'messages_role_check'
    ) THEN
        ALTER TABLE messages
            ADD CONSTRAINT messages_role_check
            CHECK (role IN ('user', 'assistant', 'system', 'tool'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS agent_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
    task_id TEXT,
    node_id TEXT,
    event_type TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_events_task_created
    ON agent_events(task_id, created_at);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
    task_id TEXT NOT NULL,
    checkpoint_type TEXT NOT NULL,
    runtime_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE checkpoints
    DROP COLUMN IF EXISTS state;

CREATE INDEX IF NOT EXISTS idx_checkpoints_task_created
    ON checkpoints(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS interrupts (
    interrupt_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
    task_id TEXT NOT NULL,
    plan_id TEXT,
    node_id TEXT NOT NULL,
    checkpoint_id TEXT REFERENCES checkpoints(checkpoint_id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    interrupt_type TEXT NOT NULL DEFAULT 'human_input',
    reason TEXT,
    prompt TEXT NOT NULL,
    required_input JSONB NOT NULL DEFAULT '{}'::jsonb,
    context_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    response_message_id TEXT REFERENCES messages(message_id) ON DELETE SET NULL,
    resolution_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_interrupts_task_status_created
    ON interrupts(task_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'compiled',
    goal JSONB NOT NULL DEFAULT '{}'::jsonb,
    dag JSONB NOT NULL DEFAULT '{}'::jsonb,
    capability_dag JSONB NOT NULL DEFAULT '{}'::jsonb,
    tool_bindings JSONB NOT NULL DEFAULT '{}'::jsonb,
    argument_bindings JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_plans_task_created
    ON plans(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS tool_calls (
    tool_call_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
    task_id TEXT NOT NULL,
    node_id TEXT,
    tool_name TEXT,
    worker_id TEXT,
    attempt INTEGER,
    status TEXT NOT NULL DEFAULT 'running',
    input_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_task_node
    ON tool_calls(task_id, node_id);

CREATE TABLE IF NOT EXISTS memory_facts (
    fact_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
    task_id TEXT,
    artifact_id TEXT,
    kind TEXT NOT NULL DEFAULT 'fact',
    text TEXT NOT NULL,
    source TEXT,
    importance DOUBLE PRECISION,
    embedding JSONB,
    embedding_model TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_facts_session_updated
    ON memory_facts(session_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_facts_kind
    ON memory_facts(kind);

CREATE TABLE IF NOT EXISTS session_summaries (
    summary_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    task_id TEXT,
    summary_type TEXT NOT NULL DEFAULT 'rolling',
    status TEXT NOT NULL DEFAULT 'active',
    summary TEXT NOT NULL,
    covered_from_turn INTEGER,
    covered_until_turn INTEGER,
    covered_from_message_id TEXT,
    covered_until_message_id TEXT,
    source_message_count INTEGER,
    source_token_count INTEGER,
    summary_token_count INTEGER,
    model TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE session_summaries
    ADD COLUMN IF NOT EXISTS task_id TEXT;

ALTER TABLE session_summaries
    ADD COLUMN IF NOT EXISTS summary_type TEXT NOT NULL DEFAULT 'rolling';

ALTER TABLE session_summaries
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';

ALTER TABLE session_summaries
    ADD COLUMN IF NOT EXISTS covered_from_turn INTEGER;

ALTER TABLE session_summaries
    ADD COLUMN IF NOT EXISTS covered_from_message_id TEXT;

ALTER TABLE session_summaries
    ADD COLUMN IF NOT EXISTS covered_until_message_id TEXT;

ALTER TABLE session_summaries
    ADD COLUMN IF NOT EXISTS source_message_count INTEGER;

ALTER TABLE session_summaries
    ADD COLUMN IF NOT EXISTS source_token_count INTEGER;

ALTER TABLE session_summaries
    ADD COLUMN IF NOT EXISTS summary_token_count INTEGER;

ALTER TABLE session_summaries
    ADD COLUMN IF NOT EXISTS model TEXT;

CREATE INDEX IF NOT EXISTS idx_session_summaries_session_updated
    ON session_summaries(session_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_session_summaries_session_status
    ON session_summaries(session_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS long_term_memory (
    memory_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    owner_type TEXT NOT NULL DEFAULT 'session',
    source_session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
    source_fact_id TEXT,
    kind TEXT NOT NULL DEFAULT 'fact',
    text TEXT NOT NULL,
    importance DOUBLE PRECISION,
    embedding JSONB,
    embedding_model TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_long_term_memory_owner_updated
    ON long_term_memory(owner_id, owner_type, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_long_term_memory_kind
    ON long_term_memory(kind);
"""


class PostgresAgentStore:
    """Small repository wrapper for the six core agent persistence tables."""

    def __init__(self, dsn: str):
        if psycopg is None:
            raise RuntimeError(
                "缺少 psycopg 依赖，请先安装: pip install 'psycopg[binary]'"
            )
        self.dsn = dsn

    @classmethod
    def from_env(cls) -> "PostgresAgentStore | None":
        dsn = os.getenv("POSTGRES_URI") or os.getenv("DATABASE_URL")
        if not dsn:
            return None
        return cls(dsn)

    @contextmanager
    def connect(self) -> Iterator[Any]:
        with psycopg.connect(self.dsn) as conn:
            yield conn

    def setup(self) -> None:
        with self.connect() as conn:
            conn.execute(SCHEMA_SQL)
            conn.commit()

    def ensure_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, title, metadata, updated_at)
                VALUES (%s, %s, %s::jsonb, %s)
                ON CONFLICT (session_id) DO UPDATE SET
                    title = COALESCE(EXCLUDED.title, sessions.title),
                    metadata = sessions.metadata || EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at
                """,
                (session_id, title, json_dumps(metadata or {}), utc_now()),
            )
            conn.commit()
        return session_id

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        task_id: str | None = None,
        turn_index: int | None = None,
        token_count: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        message_id = new_id("msg")
        self.ensure_session(session_id)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO messages
                    (
                        message_id, session_id, task_id, turn_index, role,
                        content, token_count, metadata
                    )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    message_id,
                    session_id,
                    task_id,
                    turn_index,
                    role,
                    content,
                    token_count,
                    json_dumps(metadata or {}),
                ),
            )
            conn.commit()
        return message_id

    def save_plan(
        self,
        session_id: str | None,
        task_id: str,
        *,
        goal: dict[str, Any] | None,
        dag: dict[str, Any] | None,
        capability_dag: dict[str, Any] | None = None,
        tool_bindings: dict[str, Any] | None = None,
        argument_bindings: dict[str, Any] | None = None,
        status: str = "compiled",
    ) -> str:
        plan_id = new_id("plan")
        if session_id:
            self.ensure_session(session_id)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO plans (
                    plan_id, session_id, task_id, status, goal, dag,
                    capability_dag, tool_bindings, argument_bindings
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                        %s::jsonb, %s::jsonb)
                """,
                (
                    plan_id,
                    session_id,
                    task_id,
                    status,
                    json_dumps(goal or {}),
                    json_dumps(dag or {}),
                    json_dumps(capability_dag or {}),
                    json_dumps(tool_bindings or {}),
                    json_dumps(argument_bindings or {}),
                ),
            )
            conn.commit()
        return plan_id

    def log_event(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
        level: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> str:
        event_id = new_id("evt")
        if session_id:
            self.ensure_session(session_id)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_events
                    (event_id, session_id, task_id, node_id, event_type, level, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    event_id,
                    session_id,
                    task_id,
                    node_id,
                    event_type,
                    level,
                    json_dumps(payload or {}),
                ),
            )
            conn.commit()
        return event_id

    def save_checkpoint(
        self,
        task_id: str,
        *,
        session_id: str | None = None,
        checkpoint_type: str,
        runtime_state: dict[str, Any] | None = None,
    ) -> str:
        checkpoint_id = new_id("ckpt")
        if session_id:
            self.ensure_session(session_id)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints
                    (checkpoint_id, session_id, task_id, checkpoint_type, runtime_state)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                """,
                (
                    checkpoint_id,
                    session_id,
                    task_id,
                    checkpoint_type,
                    json_dumps(runtime_state or {}),
                ),
            )
            conn.commit()
        return checkpoint_id

    def create_interrupt(
        self,
        task_id: str,
        *,
        session_id: str | None = None,
        plan_id: str | None = None,
        node_id: str,
        checkpoint_id: str | None = None,
        interrupt_type: str = "human_input",
        reason: str | None = None,
        prompt: str,
        required_input: dict[str, Any] | None = None,
        context_payload: dict[str, Any] | None = None,
        expires_at: Any | None = None,
    ) -> str:
        interrupt_id = new_id("intr")
        if session_id:
            self.ensure_session(session_id)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO interrupts (
                    interrupt_id, session_id, task_id, plan_id, node_id,
                    checkpoint_id, status, interrupt_type, reason, prompt,
                    required_input, context_payload, expires_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s
                )
                """,
                (
                    interrupt_id,
                    session_id,
                    task_id,
                    plan_id,
                    node_id,
                    checkpoint_id,
                    interrupt_type,
                    reason,
                    prompt,
                    json_dumps(required_input or {}),
                    json_dumps(context_payload or {}),
                    expires_at,
                ),
            )
            conn.commit()
        return interrupt_id

    def get_interrupt(self, interrupt_id: str) -> dict[str, Any] | None:
        """读取一条 human interrupt 记录。"""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                SELECT interrupt_id, session_id, task_id, plan_id, node_id,
                       checkpoint_id, status, interrupt_type, reason, prompt,
                       required_input, context_payload, response_message_id,
                       resolution_payload, created_at, resolved_at, expires_at
                FROM interrupts
                WHERE interrupt_id = %s
                """,
                (interrupt_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return row_to_dict(row, cursor_columns(cursor))

    def resolve_interrupt(
        self,
        interrupt_id: str,
        *,
        status: str = "resolved",
        resolution_payload: dict[str, Any] | None = None,
        response_message_id: str | None = None,
    ) -> dict[str, Any] | None:
        """把 pending interrupt 更新为 resolved/rejected/cancelled 等终态。"""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE interrupts
                SET status = %s,
                    resolution_payload = %s::jsonb,
                    response_message_id = COALESCE(%s, response_message_id),
                    resolved_at = now()
                WHERE interrupt_id = %s
                RETURNING interrupt_id, session_id, task_id, plan_id, node_id,
                          checkpoint_id, status, interrupt_type, reason, prompt,
                          required_input, context_payload, response_message_id,
                          resolution_payload, created_at, resolved_at, expires_at
                """,
                (
                    status,
                    json_dumps(resolution_payload or {}),
                    response_message_id,
                    interrupt_id,
                ),
            )
            row = cursor.fetchone()
            conn.commit()
            if row is None:
                return None
            return row_to_dict(row, cursor_columns(cursor))

    def record_tool_call(
        self,
        task_id: str,
        *,
        session_id: str | None = None,
        node_id: str | None = None,
        tool_name: str | None = None,
        worker_id: str | None = None,
        attempt: int | None = None,
        status: str,
        input_payload: dict[str, Any] | None = None,
        output_payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> str:
        tool_call_id = new_id("tool")
        if session_id:
            self.ensure_session(session_id)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tool_calls (
                    tool_call_id, session_id, task_id, node_id, tool_name,
                    worker_id, attempt, status, input_payload, output_payload,
                    error, finished_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                        %s::jsonb, %s, %s)
                """,
                (
                    tool_call_id,
                    session_id,
                    task_id,
                    node_id,
                    tool_name,
                    worker_id,
                    attempt,
                    status,
                    json_dumps(input_payload or {}),
                    json_dumps(output_payload or {}),
                    error,
                    utc_now(),
                ),
            )
            conn.commit()
        return tool_call_id

    def upsert_memory_facts(
        self,
        session_id: str,
        facts: list[dict[str, Any]],
    ) -> list[str]:
        """写入或更新 session 级结构化记忆事实。"""
        if not facts:
            return []
        self.ensure_session(session_id)
        fact_ids: list[str] = []
        with self.connect() as conn:
            for fact in facts:
                fact_id = str(fact.get("fact_id") or new_id("mem"))
                fact_ids.append(fact_id)
                metadata = {
                    key: value
                    for key, value in fact.items()
                    if key not in {
                        "fact_id",
                        "session_id",
                        "task_id",
                        "artifact_id",
                        "kind",
                        "text",
                        "source",
                        "importance",
                        "embedding",
                        "embedding_model",
                        "created_at",
                        "updated_at",
                    }
                }
                conn.execute(
                    """
                    INSERT INTO memory_facts (
                        fact_id, session_id, task_id, artifact_id, kind, text,
                        source, importance, embedding, embedding_model, metadata,
                        created_at, updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s,
                        %s::jsonb, COALESCE(%s, now()), %s
                    )
                    ON CONFLICT (fact_id) DO UPDATE SET
                        session_id = EXCLUDED.session_id,
                        task_id = EXCLUDED.task_id,
                        artifact_id = EXCLUDED.artifact_id,
                        kind = EXCLUDED.kind,
                        text = EXCLUDED.text,
                        source = EXCLUDED.source,
                        importance = EXCLUDED.importance,
                        embedding = EXCLUDED.embedding,
                        embedding_model = EXCLUDED.embedding_model,
                        metadata = memory_facts.metadata || EXCLUDED.metadata,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        fact_id,
                        session_id,
                        fact.get("task_id"),
                        fact.get("artifact_id"),
                        fact.get("kind", "fact"),
                        str(fact.get("text", "")),
                        fact.get("source"),
                        fact.get("importance"),
                        json_dumps(fact.get("embedding")) if fact.get("embedding") is not None else None,
                        fact.get("embedding_model"),
                        json_dumps(metadata),
                        fact.get("created_at"),
                        utc_now(),
                    ),
                )
            conn.commit()
        return fact_ids

    def list_memory_facts(
        self,
        session_id: str,
        *,
        limit: int = 100,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """读取某个 session 下最近的结构化记忆事实。"""
        query = """
            SELECT fact_id, session_id, task_id, artifact_id, kind, text,
                   source, importance, embedding, embedding_model, metadata,
                   created_at, updated_at
            FROM memory_facts
            WHERE session_id = %s
        """
        params: list[Any] = [session_id]
        if kind:
            query += " AND kind = %s"
            params.append(kind)
        query += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)

        with self.connect() as conn:
            cursor = conn.execute(query, params)
            columns = cursor_columns(cursor)
            rows = cursor.fetchall()
        return [row_to_dict(row, columns) for row in rows]

    def save_session_summary(
        self,
        session_id: str,
        summary: str,
        *,
        summary_id: str | None = None,
        task_id: str | None = None,
        summary_type: str = "rolling",
        status: str = "active",
        covered_from_turn: int | None = None,
        covered_until_turn: int | None = None,
        covered_from_message_id: str | None = None,
        covered_until_message_id: str | None = None,
        source_message_count: int | None = None,
        source_token_count: int | None = None,
        summary_token_count: int | None = None,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """保存一条 session summary 版本。"""
        resolved_summary_id = summary_id or new_id("sum")
        self.ensure_session(session_id)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO session_summaries (
                    summary_id, session_id, task_id, summary_type, status,
                    summary, covered_from_turn, covered_until_turn,
                    covered_from_message_id, covered_until_message_id,
                    source_message_count, source_token_count,
                    summary_token_count, model, metadata, updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s
                )
                ON CONFLICT (summary_id) DO UPDATE SET
                    task_id = EXCLUDED.task_id,
                    summary_type = EXCLUDED.summary_type,
                    status = EXCLUDED.status,
                    summary = EXCLUDED.summary,
                    covered_from_turn = EXCLUDED.covered_from_turn,
                    covered_until_turn = EXCLUDED.covered_until_turn,
                    covered_from_message_id = EXCLUDED.covered_from_message_id,
                    covered_until_message_id = EXCLUDED.covered_until_message_id,
                    source_message_count = EXCLUDED.source_message_count,
                    source_token_count = EXCLUDED.source_token_count,
                    summary_token_count = EXCLUDED.summary_token_count,
                    model = EXCLUDED.model,
                    metadata = session_summaries.metadata || EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    resolved_summary_id,
                    session_id,
                    task_id,
                    summary_type,
                    status,
                    summary,
                    covered_from_turn,
                    covered_until_turn,
                    covered_from_message_id,
                    covered_until_message_id,
                    source_message_count,
                    source_token_count,
                    summary_token_count,
                    model,
                    json_dumps(metadata or {}),
                    utc_now(),
                ),
            )
            conn.commit()
        return resolved_summary_id

    def get_latest_session_summary(self, session_id: str) -> dict[str, Any] | None:
        """读取某个 session 最新的 summary。"""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                SELECT summary_id, session_id, task_id, summary_type, status,
                       summary, covered_from_turn, covered_until_turn,
                       covered_from_message_id, covered_until_message_id,
                       source_message_count, source_token_count,
                       summary_token_count, model, metadata, created_at,
                       updated_at
                FROM session_summaries
                WHERE session_id = %s
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (session_id,),
            )
            columns = cursor_columns(cursor)
            row = cursor.fetchone()
        return row_to_dict(row, columns) if row else None

    def upsert_long_term_memory(
        self,
        owner_id: str,
        memories: list[dict[str, Any]],
        *,
        owner_type: str = "session",
        source_session_id: str | None = None,
    ) -> list[str]:
        """写入或更新长期记忆。"""
        if not memories:
            return []
        if source_session_id:
            self.ensure_session(source_session_id)

        memory_ids: list[str] = []
        with self.connect() as conn:
            for memory in memories:
                memory_id = str(
                    memory.get("memory_id")
                    or memory.get("fact_id")
                    or new_id("ltm")
                )
                memory_ids.append(memory_id)
                metadata = {
                    key: value
                    for key, value in memory.items()
                    if key not in {
                        "memory_id",
                        "fact_id",
                        "source_fact_id",
                        "owner_id",
                        "owner_type",
                        "source_session_id",
                        "kind",
                        "text",
                        "importance",
                        "embedding",
                        "embedding_model",
                        "valid_from",
                        "valid_until",
                        "created_at",
                        "updated_at",
                    }
                }
                conn.execute(
                    """
                    INSERT INTO long_term_memory (
                        memory_id, owner_id, owner_type, source_session_id,
                        source_fact_id, kind, text, importance, embedding,
                        embedding_model, metadata, valid_from, valid_until,
                        updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s,
                        %s::jsonb, COALESCE(%s, now()), %s, %s
                    )
                    ON CONFLICT (memory_id) DO UPDATE SET
                        owner_id = EXCLUDED.owner_id,
                        owner_type = EXCLUDED.owner_type,
                        source_session_id = EXCLUDED.source_session_id,
                        source_fact_id = EXCLUDED.source_fact_id,
                        kind = EXCLUDED.kind,
                        text = EXCLUDED.text,
                        importance = EXCLUDED.importance,
                        embedding = EXCLUDED.embedding,
                        embedding_model = EXCLUDED.embedding_model,
                        metadata = long_term_memory.metadata || EXCLUDED.metadata,
                        valid_from = EXCLUDED.valid_from,
                        valid_until = EXCLUDED.valid_until,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        memory_id,
                        owner_id,
                        owner_type,
                        source_session_id or memory.get("source_session_id"),
                        memory.get("source_fact_id") or memory.get("fact_id"),
                        memory.get("kind", "fact"),
                        str(memory.get("text", "")),
                        memory.get("importance"),
                        json_dumps(memory.get("embedding")) if memory.get("embedding") is not None else None,
                        memory.get("embedding_model"),
                        json_dumps(metadata),
                        memory.get("valid_from"),
                        memory.get("valid_until"),
                        utc_now(),
                    ),
                )
            conn.commit()
        return memory_ids

    def list_long_term_memory(
        self,
        owner_id: str,
        *,
        owner_type: str = "session",
        limit: int = 100,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """读取某个 owner 下最近的长期记忆。"""
        query = """
            SELECT memory_id, owner_id, owner_type, source_session_id,
                   source_fact_id, kind, text, importance, embedding,
                   embedding_model, metadata, valid_from, valid_until,
                   created_at, updated_at
            FROM long_term_memory
            WHERE owner_id = %s
              AND owner_type = %s
              AND valid_until IS NULL
        """
        params: list[Any] = [owner_id, owner_type]
        if kind:
            query += " AND kind = %s"
            params.append(kind)
        query += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)

        with self.connect() as conn:
            cursor = conn.execute(query, params)
            columns = cursor_columns(cursor)
            rows = cursor.fetchall()
        return [row_to_dict(row, columns) for row in rows]


def get_postgres_store_from_env(setup: bool = False) -> PostgresAgentStore | None:
    store = PostgresAgentStore.from_env()
    if store is not None and setup:
        store.setup()
    return store


def json_dumps(value: Any) -> str:
    return json.dumps(redact(value), ensure_ascii=False, default=str)


def cursor_columns(cursor: Any) -> list[str]:
    """读取 cursor 的列名。"""
    return [column.name for column in cursor.description or []]


def row_to_dict(row: Any, columns: list[str] | None = None) -> dict[str, Any]:
    """把 psycopg 查询结果转成普通 dict。"""
    if row is None:
        return {}
    if columns:
        return dict(zip(columns, row))
    if hasattr(row, "_asdict"):
        return dict(row._asdict())
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    columns = getattr(row, "_fields", None)
    if columns:
        return dict(zip(columns, row))
    return dict(row) if isinstance(row, dict) else {"row": row}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description="PostgreSQL agent persistence setup")
    parser.add_argument(
        "--dsn",
        default=os.getenv("POSTGRES_URI") or os.getenv("DATABASE_URL"),
        help="PostgreSQL DSN. Defaults to POSTGRES_URI or DATABASE_URL.",
    )
    parser.add_argument("--setup", action="store_true", help="Create core tables.")
    args = parser.parse_args()

    if not args.dsn:
        raise SystemExit("缺少 PostgreSQL DSN，请设置 POSTGRES_URI 或 DATABASE_URL。")

    store = PostgresAgentStore(args.dsn)
    if args.setup:
        store.setup()
        print("PostgreSQL agent tables are ready.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

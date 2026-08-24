"""智能体记忆子系统。

MemoryManager 负责独立于提示词拼装的记忆流水线：

1. 把原始消息和事件形态的交互记录作为事实输入；
2. 从消息、任务摘要、产物中抽取结构化记忆事实；
3. 合并事实并计算重要性评分；
4. 生成压缩后的会话摘要；
5. 在新请求到来时检索相关记忆片段。

ContextManager 需要记忆材料时调用本模块，但阶段专用 context 的拼装仍保留在
context_manager.py 中。
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import uuid4


DEFAULT_MEMORY_SUMMARY_CHARS = 4_000
DEFAULT_MEMORY_FACTS = 50
DEFAULT_EMBEDDING_MODEL = (
    os.getenv("MEMORY_EMBEDDING_MODEL")
    or os.getenv("BGE4_MODEL")
    or "BAAI/bge-m3"
)
DEFAULT_LLM_SUMMARY_ENABLED = os.getenv("MEMORY_SUMMARY_USE_LLM", "1").lower() not in {
    "0",
    "false",
    "no",
}


class MemoryManager:
    """面向一个或多个会话的无状态记忆业务逻辑。

    这个类暂时不持有 Redis/PostgreSQL 连接，只处理标准化后的会话字典。
    这样存储仍可留在 ContextManager/PostgresAgentStore，未来也可以平滑替换成
    pgvector 或其他长期记忆后端。
    """

    def __init__(
        self,
        *,
        embedding_model_name: str = DEFAULT_EMBEDDING_MODEL,
        embedding_model: Any | None = None,
        prefer_embedding: bool = True,
        llm_summary_enabled: bool = DEFAULT_LLM_SUMMARY_ENABLED,
        llm_summary_model: str | None = None,
    ):
        """初始化记忆管理器。

        embedding_model_name 默认使用 BGE 系列模型；如果需要换成具体的 BGE4
        模型名，可以通过 MEMORY_EMBEDDING_MODEL 环境变量覆盖。
        embedding_model 支持外部注入，便于测试或复用已加载模型。
        llm_summary_enabled 控制压缩时是否优先调用 LLM 生成 rolling summary；
        LLM 不可用或调用失败时会自动回退到确定性摘要。
        """
        self.embedding_model_name = embedding_model_name
        self._embedding_model = embedding_model
        self.prefer_embedding = prefer_embedding
        self.llm_summary_enabled = llm_summary_enabled
        self.llm_summary_model = (
            llm_summary_model
            or os.getenv("MEMORY_SUMMARY_MODEL")
            or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
        )

    # ------------------------------------------------------------------
    # 读取链路：构建上下文时从记忆中取信息
    # ------------------------------------------------------------------

    def retrieve_relevant_facts(
        self,
        facts: list[dict[str, Any]],
        query: str,
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """统一相关事实检索入口，优先使用 embedding，失败时回退关键词。"""
        if self.prefer_embedding:
            embedded = self.retrieve_by_embedding(facts, query, limit=limit)
            if embedded:
                return embedded
        return self.retrieve_by_keywords(facts, query, limit=limit)

    def retrieve_by_embedding(
        self,
        facts: list[dict[str, Any]],
        query: str,
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """使用 BGE embedding 和余弦相似度检索相关 facts。"""
        if not facts:
            return []
        if not query:
            return facts[-limit:]

        query_embedding = self.embed_text(query)
        if not query_embedding:
            return []

        scored: list[tuple[float, int, dict[str, Any]]] = []
        for index, fact in enumerate(facts):
            fact_embedding = fact.get("embedding")
            if not is_vector(fact_embedding):
                fact_embedding = self.embed_text(self.fact_to_embedding_text(fact))
                if fact_embedding:
                    fact["embedding"] = fact_embedding
                    fact["embedding_model"] = self.embedding_model_name
            if not is_vector(fact_embedding):
                continue

            normalized_fact_embedding = normalize_vector(to_float_list(fact_embedding))
            if not normalized_fact_embedding:
                continue

            score = cosine_similarity(query_embedding, normalized_fact_embedding)
            if score > 0:
                scored.append((score, index, fact))

        if not scored:
            return []
        scored.sort(key=lambda item: (item[0], item[1]))
        return [fact for _, _, fact in scored[-limit:]]

    def retrieve_by_keywords(
        self,
        facts: list[dict[str, Any]],
        query: str,
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """使用关键词匹配检索相关 facts，作为 embedding 不可用时的回退。"""
        if not query:
            return facts[-limit:]
        terms = {
            term.strip().lower()
            for term in query.replace("/", " ").replace("_", " ").split()
            if term.strip()
        }
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for index, fact in enumerate(facts):
            haystack = " ".join(
                str(fact.get(key, ""))
                for key in ("kind", "text", "source", "task_id", "artifact_id")
            ).lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, index, fact))
        if not scored:
            return facts[-limit:]
        scored.sort(key=lambda item: (item[0], item[1]))
        return [fact for _, _, fact in scored[-limit:]]

    def embed_text(self, text: str) -> list[float] | None:
        """使用 BGE 模型把文本转成向量；模型不可用时返回 None。"""
        model = self.get_embedding_model()
        if model is None:
            return None
        try:
            embedding = model.encode(text, normalize_embeddings=True)
        except TypeError:
            embedding = model.encode(text)
        except Exception:
            return None
        return normalize_vector(to_float_list(embedding))

    def get_embedding_model(self) -> Any | None:
        """懒加载 BGE embedding 模型；缺少依赖或模型不可用时返回 None。"""
        if self._embedding_model is not None:
            return self._embedding_model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            return None
        try:
            self._embedding_model = SentenceTransformer(self.embedding_model_name)
        except Exception:
            return None
        return self._embedding_model

    def fact_to_embedding_text(self, fact: dict[str, Any]) -> str:
        """把 fact 转成适合做 embedding 的文本。"""
        return "\n".join(
            part
            for part in (
                f"类型: {fact.get('kind', 'fact')}",
                f"内容: {fact.get('text', '')}",
                f"来源: {fact.get('source', '')}",
                f"任务: {fact.get('task_id', '')}",
                f"产物: {fact.get('artifact_id', '')}",
            )
            if part.strip()
        )

    def build_memory_context(
        self,
        *,
        query: str,
        recent_messages: list[dict[str, Any]],
        session_summary: str,
        memory_facts: list[dict[str, Any]],
        long_term_memory: list[dict[str, Any]] | None = None,
        max_facts: int = 8,
        max_long_term: int = 8,
    ) -> dict[str, Any]:
        """构建可嵌入 ContextManager 阶段视图的记忆切片。"""
        return {
            "session_summary": session_summary,
            "recent_messages": recent_messages,
            "relevant_facts": self.retrieve_relevant_facts(
                memory_facts,
                query,
                limit=max_facts,
            ),
            "long_term_memory": self.retrieve_relevant_facts(
                long_term_memory or [],
                query,
                limit=max_long_term,
            ),
        }

    # ------------------------------------------------------------------
    # 写入链路：任务完成或工具调用后写入记忆
    # ------------------------------------------------------------------

    def build_message_event(
        self,
        *,
        role: str,
        content: str,
        session_id: str | None = None,
        task_id: str | None = None,
        turn_index: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构造一条原始消息事件记录。"""
        return {
            "event_id": f"evt_{uuid4().hex[:12]}",
            "session_id": session_id,
            "task_id": task_id,
            "turn_index": turn_index,
            "role": role,
            "content": content,
            "metadata": metadata or {},
            "created_at": utc_now(),
        }

    def build_tool_event(
        self,
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
        """构造一条压缩后的工具执行事件，供记忆和执行轨迹使用。"""
        return {
            "event_id": f"evt_{uuid4().hex[:12]}",
            "task_id": task_id,
            "node_id": node_id,
            "tool_name": tool_name,
            "worker_id": worker_id,
            "attempt": attempt,
            "status": status,
            "input_summary": summarize_value(input_payload or {}),
            "output_summary": summarize_value(output_payload or {}),
            "error": error,
            "created_at": utc_now(),
        }

    def build_memory_update(
        self,
        *,
        old_summary: str,
        existing_facts: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
        fact_limit: int = DEFAULT_MEMORY_FACTS,
        summary_max_chars: int = DEFAULT_MEMORY_SUMMARY_CHARS,
        long_term_min_score: float = 0.8,
        long_term_limit: int = 20,
    ) -> dict[str, Any]:
        """完成一次写入前后的记忆业务处理，但不直接写数据库。

        返回结果分两层：
        1. 业务结果：new_facts、memory_facts、session_summary、long_term_candidates。
        2. 存储载荷：storage_payload，交给 ContextManager 或 PostgresAgentStore 写入。
        """
        new_facts = self.extract_facts(
            messages=messages,
            tasks=tasks,
            artifacts=artifacts,
        )
        memory_facts = self.merge_facts(
            existing_facts,
            new_facts,
            limit=fact_limit,
        )
        session_summary = self.build_session_summary(
            old_summary,
            messages,
            tasks,
            artifacts,
            memory_facts,
            max_chars=summary_max_chars,
        )
        long_term_candidates = self.select_long_term_candidates(
            memory_facts,
            min_score=long_term_min_score,
            limit=long_term_limit,
        )

        return {
            "new_facts": new_facts,
            "memory_facts": memory_facts,
            "session_summary": session_summary,
            "long_term_candidates": long_term_candidates,
            "storage_payload": {
                "memory_facts": memory_facts,
                "session_summary": session_summary,
                "long_term_memory": long_term_candidates,
            },
        }

    def extract_facts(
        self,
        *,
        messages: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """从会话记录中抽取结构化记忆事实。"""
        return extract_memory_facts(
            messages=messages,
            tasks=tasks,
            artifacts=artifacts,
        )

    def merge_facts(
        self,
        existing: list[dict[str, Any]],
        new: list[dict[str, Any]],
        *,
        limit: int = DEFAULT_MEMORY_FACTS,
    ) -> list[dict[str, Any]]:
        """合并事实、按类型和文本去重，并保留有限数量的最新事实。"""
        return merge_memory_facts(existing, new, limit=limit)

    def score_fact_importance(self, fact: dict[str, Any]) -> float:
        """评估某条事实是否值得晋升为长期记忆。

        当前实现刻意保持确定性。后续可以改成 LLM 评分或 embedding 评分，而无需
        改动 ContextManager 的职责边界。
        """
        kind = str(fact.get("kind", "fact"))
        text = str(fact.get("text", ""))
        score_by_kind = {
            "preference": 0.9,
            "decision": 0.85,
            "constraint": 0.8,
            "entity": 0.65,
            "artifact": 0.55,
            "task_summary": 0.4,
        }
        score = score_by_kind.get(kind, 0.5)
        lowered = text.lower()
        if any(keyword in lowered for keyword in ("always", "默认", "以后", "记住")):
            score += 0.08
        if len(text) < 12:
            score -= 0.1
        return max(0.0, min(1.0, score))

    def select_long_term_candidates(
        self,
        facts: list[dict[str, Any]],
        *,
        min_score: float = 0.8,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """筛选高重要性事实，作为后续写入长期记忆的候选项。"""
        scored = []
        for fact in facts:
            item = dict(fact)
            item["importance"] = self.score_fact_importance(fact)
            scored.append(item)
        selected = [fact for fact in scored if fact["importance"] >= min_score]
        return sorted(selected, key=lambda fact: fact["importance"])[-limit:]

    def build_session_summary(
        self,
        old_summary: str,
        messages: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
        memory_facts: list[dict[str, Any]],
        *,
        max_chars: int = DEFAULT_MEMORY_SUMMARY_CHARS,
    ) -> str:
        """生成压缩会话摘要；优先 LLM，失败时回退确定性摘要。"""
        fallback_summary = build_advanced_extractive_summary(
            old_summary,
            messages,
            tasks,
            artifacts,
            memory_facts,
            max_chars=max_chars,
        )
        if not self.llm_summary_enabled:
            return fallback_summary

        llm_summary = build_llm_session_summary(
            old_summary,
            messages,
            tasks,
            artifacts,
            memory_facts,
            max_chars=max_chars,
            model=self.llm_summary_model,
        )
        return llm_summary or fallback_summary


def build_llm_session_summary(
    old_summary: str,
    messages: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    memory_facts: list[dict[str, Any]],
    *,
    max_chars: int = DEFAULT_MEMORY_SUMMARY_CHARS,
    model: str | None = None,
) -> str | None:
    """使用 OpenAI-compatible LLM 生成 rolling summary；不可用时返回 None。"""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        return None

    compacted_messages = [
        {
            "role": message.get("role"),
            "turn_index": message.get("turn_index"),
            "task_id": message.get("task_id"),
            "content": str(message.get("content", ""))[:1200],
        }
        for message in messages[-30:]
        if message.get("content")
    ]
    task_summaries = [
        {
            "task_id": task.get("task_id"),
            "turn_index": task.get("turn_index"),
            "summary": summarize_value(task.get("summary", {}), max_chars=600),
        }
        for task in tasks[-10:]
    ]
    artifact_summaries = [
        {
            "artifact_id": artifact.get("artifact_id"),
            "task_id": artifact.get("task_id"),
            "kind": artifact.get("kind"),
            "name": artifact.get("name") or artifact.get("value") or artifact.get("uri"),
            "summary": str(artifact.get("summary") or "")[:300],
        }
        for artifact in artifacts[-12:]
    ]
    fact_summaries = [
        {
            "kind": fact.get("kind"),
            "text": str(fact.get("text", ""))[:400],
            "source": fact.get("source"),
            "task_id": fact.get("task_id"),
        }
        for fact in memory_facts[-20:]
    ]
    payload = {
        "old_summary": old_summary[-max_chars:] if old_summary else "",
        "compacted_messages": compacted_messages,
        "memory_facts": fact_summaries,
        "recent_tasks": task_summaries,
        "artifacts": artifact_summaries,
        "max_chars": max_chars,
        "output_shape": {
            "summary": "rolling session summary string",
        },
    }

    try:
        client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
        response = client.chat.completions.create(
            model=model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You compress agent conversation history into a durable rolling summary. "
                        "Preserve user preferences, decisions, constraints, unresolved requests, "
                        "important entities, task outcomes, artifact references, and failures. "
                        "Do not invent facts. Remove chatter and duplicate details. "
                        "Return valid JSON only: {\"summary\":\"...\"}."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                },
            ],
            response_format={"type": "json_object"},
        )
    except Exception:
        return None

    content = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None

    summary = str(parsed.get("summary") or "").strip()
    if not summary:
        return None
    return summary[-max_chars:]


def build_advanced_extractive_summary(
    old_summary: str,
    messages: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    memory_facts: list[dict[str, Any]],
    *,
    max_chars: int = DEFAULT_MEMORY_SUMMARY_CHARS,
) -> str:
    """在不调用 LLM 的情况下生成结构化确定性摘要。"""
    sections: list[str] = []
    if old_summary:
        sections.append(f"[existing_summary]\n{old_summary.strip()}")

    facts = merge_memory_facts(
        memory_facts,
        extract_memory_facts(messages=messages, tasks=tasks, artifacts=artifacts),
        limit=12,
    )
    if facts:
        fact_lines = [
            f"- {fact.get('kind', 'fact')}: {fact.get('text', '')[:180]}"
            for fact in facts[-12:]
            if fact.get("text")
        ]
        if fact_lines:
            sections.append("[memory_facts]\n" + "\n".join(fact_lines))

    if tasks:
        task_lines = []
        for task in tasks[-5:]:
            task_id = task.get("task_id")
            summary = summarize_value(task.get("summary", {}), max_chars=220)
            task_lines.append(f"- {task_id}: {summary}")
        sections.append("[recent_tasks]\n" + "\n".join(task_lines))

    if messages:
        message_lines = []
        for message in messages[-10:]:
            role = message.get("role", "unknown")
            turn = message.get("turn_index")
            content = str(message.get("content", "")).strip()
            if content:
                message_lines.append(f"- turn={turn} {role}: {content[:220]}")
        if message_lines:
            sections.append("[compacted_dialogue]\n" + "\n".join(message_lines))

    if artifacts:
        artifact_lines = []
        for artifact in artifacts[-8:]:
            name = artifact.get("name") or artifact.get("value") or artifact.get("uri")
            kind = artifact.get("kind")
            summary = artifact.get("summary") or name
            artifact_lines.append(f"- {kind}: {name} ({str(summary)[:140]})")
        sections.append("[artifacts]\n" + "\n".join(artifact_lines))

    return "\n\n".join(sections)[-max_chars:]


def extract_memory_facts(
    *,
    messages: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从记录中抽取轻量结构化事实。"""
    facts: list[dict[str, Any]] = []

    for message in messages:
        content = str(message.get("content", "")).strip()
        if not content:
            continue
        lowered = content.lower()
        if any(keyword in lowered for keyword in ("以后", "默认", "prefer", "always", "记住")):
            facts.append(make_memory_fact(
                "preference",
                content[:240],
                source="message",
                task_id=message.get("task_id"),
            ))
        elif any(keyword in lowered for keyword in ("决定", "方案", "计划", "改成", "接入")):
            facts.append(make_memory_fact(
                "decision",
                content[:240],
                source="message",
                task_id=message.get("task_id"),
            ))

    for task in tasks[-10:]:
        summary = summarize_value(task.get("summary", {}), max_chars=260)
        if summary:
            facts.append(make_memory_fact(
                "task_summary",
                f"{task.get('task_id')}: {summary}",
                source="task",
                task_id=task.get("task_id"),
            ))

    for artifact in artifacts[-12:]:
        name = artifact.get("name") or artifact.get("value") or artifact.get("uri")
        if name:
            facts.append(make_memory_fact(
                "artifact",
                f"{artifact.get('kind')}: {name}",
                source="artifact",
                task_id=artifact.get("task_id"),
                artifact_id=artifact.get("artifact_id"),
            ))

    return facts


def make_memory_fact(
    kind: str,
    text: str,
    *,
    source: str,
    task_id: str | None = None,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    """构造一条标准化记忆事实。"""
    return {
        "fact_id": f"mem_{uuid4().hex[:12]}",
        "kind": kind,
        "text": text,
        "source": source,
        "task_id": task_id,
        "artifact_id": artifact_id,
        "created_at": utc_now(),
    }


def merge_memory_facts(
    existing: list[dict[str, Any]],
    new: list[dict[str, Any]],
    *,
    limit: int = DEFAULT_MEMORY_FACTS,
) -> list[dict[str, Any]]:
    """按类型和文本去重，并保留有限数量的最近事实。"""
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for fact in [*existing, *new]:
        text = str(fact.get("text", "")).strip()
        if not text:
            continue
        key = (str(fact.get("kind", "fact")), text)
        if key in seen:
            continue
        seen.add(key)
        merged.append(fact)
    return merged[-limit:]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """计算两个向量的余弦相似度。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (left_norm * right_norm)


def normalize_vector(vector: Sequence[float] | None) -> list[float] | None:
    """把向量归一化，便于稳定计算相似度。"""
    if not vector:
        return None
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return None
    return [value / norm for value in vector]


def to_float_list(value: Any) -> list[float] | None:
    """把模型输出转换为普通 float 列表。"""
    if value is None:
        return None
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, list) and value and isinstance(value[0], list):
        value = value[0]
    if not isinstance(value, (list, tuple)):
        return None
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return None


def is_vector(value: Any) -> bool:
    """判断一个值是否是可用于相似度计算的向量。"""
    return bool(to_float_list(value))


def restore_message_order(
    original_messages: list[dict[str, Any]],
    selected_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按对象身份去重，并恢复被选中消息在原消息列表中的顺序。"""
    selected_ids = {id(message) for message in selected_messages}
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for message in original_messages:
        marker = id(message)
        if marker in selected_ids and marker not in seen:
            result.append(message)
            seen.add(marker)
    return result


def summarize_value(value: Any, *, max_chars: int = 500) -> Any:
    """摘要类 JSON 值，避免记忆层保留过大的负载。"""
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


def json_dumps(value: Any) -> str:
    """把记忆负载序列化成稳定的提示词片段。"""
    return json.dumps(value, ensure_ascii=False, default=str)


def utc_now() -> str:
    """返回 ISO-8601 格式的 UTC 时间戳。"""
    return datetime.now(timezone.utc).isoformat()

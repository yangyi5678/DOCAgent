"""Goal Parser 模块。

本模块负责把用户原始输入解析成结构化 Goal IR。它解决的问题是：
用户想做什么、属于哪类任务、可能需要什么工具、是否需要进一步澄清。
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, TypedDict, Literal

from context_manager import ContextManager

Intent = Literal[
    "qa",
    "search",
    "analysis",
    "summarization",
    "planning",
    "tool_call",
]

InterruptLevel = Literal[
    "none",
    "low",
    "high",
]


class Goal(TypedDict):
    text: str
    intents: List[Intent]
    entities: List[str]
    constraints: Dict[str, Any]
    completion_criteria: List[str]
    ambiguity: Dict[str, Any]
    interrupt_level: InterruptLevel
    needs_clarification: bool
    intent_scores: Dict[str, float]


goal_schema = {
    "type": "object",
    "properties": {
        "intents": {
            "type": "array",
            "items": {"type": "string"}
        },

        "entities": {
            "type": "array",
            "items": {"type": "string"}
        },

        "constraints": {
            "type": "object"
        },

        "completion_criteria": {
            "type": "array",
            "items": {"type": "string"}
        },

        "ambiguity": {
            "type": "object"
        },

        "requires_clarification": {"type": "boolean"},
        "clarification_questions": {
            "type": "array",
            "items": {"type": "string"}
        },

        "intent_scores": {
            "type": "object"
        }
    },
    "required": [
        "intents",
        "entities",
        "constraints",
        "completion_criteria",
        "ambiguity",
        "requires_clarification",
        "intent_scores"
    ]
}


def goal_parser(state: dict) -> dict:
    """将 ``state["user_input"]`` 解析成结构化 Goal 并写回 state。

    这是 LangGraph 中的 goal parser 节点入口。函数会优先尝试调用 OpenAI
    Chat Completions API，并使用 ``goal_schema`` 约束模型输出 JSON。
    如果当前环境没有安装 OpenAI SDK，或者模型返回内容不是合法 JSON，
    函数会回退到本地规则解析函数 ``parse_goal``。

    参数：
        state: 可变的图状态字典。函数要求其中包含 ``user_input`` 字段，
            该字段保存用户原始请求文本。

    输入：
        ``state["user_input"]`` 是自然语言用户请求，需要被转换成机器可读的
        Goal 结构。

    输出：
        返回同一个 ``state`` 字典，并写入 ``state["goal"]``。goal 结构包含
        ``text``、``intents``、``entities``、``constraints``、
        ``completion_criteria``、``ambiguity``、``interrupt_level``、
        ``needs_clarification`` 和 ``intent_scores``。
    """
    user_input = state["user_input"]
    context = state.get("context") or {}
    goal_parser_context = build_goal_parser_stage_context(state, user_input, context)

    try:
        from openai import OpenAI
    except ImportError:
        state["goal"] = parse_goal(user_input)
        state["goal_parser_context"] = goal_parser_context
        return state

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        state["goal"] = parse_goal(user_input)
        state["goal_parser_context"] = goal_parser_context
        return state

    client = OpenAI(
        api_key=api_key,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
    system_prompt = goal_parser_context.get("system_prompt") or (
        "You are a Goal Parser. "
        "Convert user input into structured Goal JSON. "
        "Do not hallucinate tools. "
        "Do not plan steps in detail. "
        "Only extract intent, entities, constraints. "
        "Do not invent completion criteria; only extract explicit success conditions. "
        "Use conversation context only to resolve references like previous result, "
        "that file, or the last task; do not copy irrelevant history. "
        "Return valid json only, matching this shape: "
        '{"intents":["qa"],"entities":[],"constraints":{},'
        '"requires_clarification":false,"completion_criteria":[],'
        '"ambiguity":{"is_ambiguous":false,'
        '"reasons":[],"clarification_questions":[]},'
        '"intent_scores":{"qa":0.9}}'
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "user_input": user_input,
                        "context": goal_parser_context,
                    },
                    ensure_ascii=False,
                )
            }
        ],

        # DeepSeek 使用 OpenAI-compatible API；JSON Output 当前使用 json_object。
        response_format={
            "type": "json_object"
        }
    )

    goal_content = response.choices[0].message.content or "{}"

    try:
        parsed_goal = json.loads(goal_content)
    except json.JSONDecodeError:
        state["goal"] = parse_goal(user_input)
        state["goal_parser_context"] = goal_parser_context
        return state

    goal = _normalize_llm_goal(user_input, parsed_goal)

    state["goal"] = goal
    state["goal_parser_context"] = goal_parser_context

    return state


def build_goal_parser_stage_context(
    state: dict,
    user_input: str,
    context: Dict[str, Any],
) -> Dict[str, Any]:
    """构造 goal_parser 阶段上下文。

    优先使用 ContextManager.build_goal_parser_context；如果没有 session_id、
    Redis 不可用或 ContextManager 失败，则回退到本地裁剪逻辑。
    """
    session_id = state.get("session_id") or context.get("session_id")
    context_manager = state.get("context_manager")

    if context_manager is None:
        try:
            context_manager = ContextManager()
        except Exception:
            context_manager = None

    if session_id and context_manager is not None:
        try:
            return context_manager.build_goal_parser_context(
                str(session_id),
                user_input=user_input,
            )
        except Exception as exc:  # noqa: BLE001 - context 失败不应阻断规则/LLM parser
            fallback = _build_goal_prompt_context(context)
            fallback["stage"] = "goal_parser"
            fallback["context_error"] = str(exc)
            fallback["user_input"] = user_input
            return fallback

    fallback = _build_goal_prompt_context(context)
    fallback["stage"] = "goal_parser"
    fallback["user_input"] = user_input
    return fallback


def _build_goal_prompt_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """裁剪传给 Goal Parser LLM 的多轮上下文。

    多轮上下文可能包含很多历史消息和节点结果，这里只保留最近几条消息、
    最近任务摘要和 session 元信息，避免 prompt 膨胀。
    """
    if not isinstance(context, dict):
        return {}

    return {
        "session_id": context.get("session_id"),
        "turn_index": context.get("turn_index"),
        "summary": context.get("summary", ""),
        "recent_messages": list(context.get("messages", []))[-8:],
        "recent_tasks": list(context.get("tasks", []))[-3:],
        "metadata": context.get("metadata", {}),
    }


def _normalize_llm_goal(user_input: str, parsed_goal: Dict[str, Any]) -> Goal:
    """将 LLM 输出的 goal 字典规范化为本地 ``Goal`` 结构。

    OpenAI schema 中使用 ``requires_clarification`` 等字段，而项目内部其他
    模块期望使用 ``needs_clarification`` 和 ``interrupt_level``。本函数负责
    校验 LLM 返回的枚举值，并在字段缺失或非法时回退到本地规则解析结果，
    最终输出统一的 Goal 结构。

    参数：
        user_input: 用户原始请求文本。去除首尾空白后会写入返回结果的
            ``text`` 字段。
        parsed_goal: 从 LLM JSON 响应中解析出的原始字典。

    输入：
        ``parsed_goal`` 可能包含模型生成的 ``intents``、``entities``、
        ``constraints``、``completion_criteria``、``ambiguity``、
        ``requires_clarification`` 和 ``intent_scores`` 等字段。

    输出：
        返回一个符合本模块字段命名和枚举约束的 ``Goal`` 字典。非法的
        ``intents`` 会被本地 ``parse_goal`` 的结果替换。
    """
    fallback = parse_goal(user_input)

    raw_intents = parsed_goal.get("intents")
    if raw_intents is None and parsed_goal.get("intent") is not None:
        raw_intents = [parsed_goal.get("intent")]
    intents = _normalize_literal_list(raw_intents, Intent.__args__)
    if not intents:
        intents = fallback["intents"]

    entities = parsed_goal.get("entities")
    if not isinstance(entities, list):
        entities = fallback["entities"]

    constraints = parsed_goal.get("constraints")
    if not isinstance(constraints, dict):
        constraints = fallback["constraints"]

    raw_completion_criteria = parsed_goal.get("completion_criteria")
    if raw_completion_criteria is None:
        raw_completion_criteria = parsed_goal.get("success_criteria")
    if raw_completion_criteria is None:
        raw_completion_criteria = parsed_goal.get("done_criteria")
    completion_criteria = _normalize_string_list(raw_completion_criteria)
    if not completion_criteria:
        completion_criteria = fallback["completion_criteria"]

    ambiguity = parsed_goal.get("ambiguity")
    if not isinstance(ambiguity, dict):
        ambiguity = fallback["ambiguity"]
    ambiguity = {
        "is_ambiguous": bool(ambiguity.get("is_ambiguous", False)),
        "reasons": _normalize_string_list(ambiguity.get("reasons", [])),
        "clarification_questions": _normalize_string_list(
            ambiguity.get("clarification_questions", [])
        ),
    }

    requires_clarification = parsed_goal.get(
        "requires_clarification",
        fallback["needs_clarification"],
    )
    needs_clarification = bool(requires_clarification) or ambiguity["is_ambiguous"]

    intent_scores = _normalize_intent_scores(
        parsed_goal.get("intent_scores"),
        intents,
        fallback["intent_scores"],
    )

    return {
        "text": user_input.strip(),
        "intents": intents,
        "entities": [str(entity) for entity in entities],
        "constraints": dict(constraints),
        "completion_criteria": completion_criteria,
        "ambiguity": ambiguity,
        "interrupt_level": "high" if needs_clarification else "none",
        "needs_clarification": needs_clarification,
        "intent_scores": intent_scores,
    }


def parse_goal(user_input: str) -> Goal:
    """使用本地确定性规则把用户文本解析成结构化 Goal。

    当 OpenAI SDK 或 API 凭证不可用时，本函数提供一个无外部依赖的解析方案。
        它会基于简单关键词和空输入检查，识别粗粒度意图、实体 token，
        以及是否需要向用户补充澄清。

    参数：
        user_input: 用户原始请求文本。

    输入：
        任意自然语言字符串。空字符串，以及 ``"help"``、``"修复"`` 这类过于
        模糊的请求，会被标记为需要澄清。

    输出：
        返回 ``Goal`` 字典，其中：
        ``text`` 是清理后的输入文本；
        ``intents`` 是粗粒度请求类别列表；
        ``entities`` 是简单抽取的实体 token；
        ``constraints`` 是用户显式给出的硬约束；
        ``completion_criteria`` 是用户显式给出的完成标准；
        ``ambiguity`` 是不确定性信息；
        ``interrupt_level`` 在需要澄清时为 ``"high"``；
        ``needs_clarification`` 表示是否需要用户补充信息；
        ``intent_scores`` 是各候选意图的置信度映射。
    """
    text = user_input.strip()
    lowered = text.lower()

    entities = _extract_entities(text)
    intents = _detect_intents(lowered)

    needs_clarification = len(text) == 0 or lowered in {
        "help",
        "帮我",
        "处理一下",
        "看一下",
        "fix",
        "修复",
    }
    ambiguity = _detect_ambiguity(text, intents, needs_clarification)
    needs_clarification = needs_clarification or ambiguity["is_ambiguous"]

    return {
        "text": text,
        "intents": intents,
        "entities": entities,
        "constraints": {},
        "completion_criteria": _extract_completion_criteria(text),
        "ambiguity": ambiguity,
        "interrupt_level": "high" if needs_clarification else "none",
        "needs_clarification": needs_clarification,
        "intent_scores": _build_intent_scores(intents, ambiguity),
    }


def _detect_intents(lowered: str) -> List[Intent]:
    """根据归一化文本和能力提示识别用户高层意图。

    参数：
        lowered: 用户输入转小写后的文本。
    输入：
        归一化后的用户请求字符串。

    输出：
        返回 ``Intent`` 字面量列表。根据中英文关键词识别搜索、总结、
        分析、规划、工具执行等意图，无法命中时默认返回 ``["qa"]``。
    """
    intents: List[Intent] = []
    if any(word in lowered for word in ["python", "执行", "运行", "代码", "code", "sql", "数据库", "db"]):
        intents.append("tool_call")
    if any(word in lowered for word in ["search", "查询", "搜索", "查找"]):
        intents.append("search")
    if any(word in lowered for word in ["summarize", "summary", "总结", "摘要"]):
        intents.append("summarization")
    if any(word in lowered for word in ["analyze", "analysis", "分析"]):
        intents.append("analysis")
    if any(word in lowered for word in ["plan", "规划", "计划"]):
        intents.append("planning")
    if not intents:
        intents.append("qa")
    return _dedupe(intents)


def _detect_ambiguity(
    text: str,
    intents: List[Intent],
    vague: bool,
) -> Dict[str, Any]:
    """根据规则识别 goal 是否存在不确定性。"""
    reasons: List[str] = []
    questions: List[str] = []

    if vague:
        reasons.append("用户输入过短或过于笼统。")
        questions.append("你希望我具体完成什么任务？")

    if len(intents) > 1:
        reasons.append("检测到多个可能意图。")
        questions.append("这些意图需要都执行，还是只执行其中一个？")

    return {
        "is_ambiguous": bool(reasons),
        "reasons": reasons,
        "clarification_questions": questions,
    }


def _build_intent_scores(
    intents: List[Intent],
    ambiguity: Dict[str, Any],
) -> Dict[str, float]:
    """为候选意图生成轻量置信度映射。"""
    base_score = 0.55 if ambiguity.get("is_ambiguous") else 0.85
    return {intent: round(base_score, 2) for intent in intents}


def _normalize_literal_list(values: Any, allowed_values: tuple[str, ...]) -> List[Any]:
    if not isinstance(values, list):
        return []
    return _dedupe([value for value in values if value in allowed_values])


def _normalize_string_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    return [str(value) for value in values]


def _normalize_intent_scores(
    scores: Any,
    intents: List[Intent],
    fallback: Dict[str, float],
) -> Dict[str, float]:
    if not isinstance(scores, dict):
        return fallback

    normalized: Dict[str, float] = {}
    for intent in intents:
        if intent not in scores:
            continue
        try:
            score = float(scores[intent])
        except (TypeError, ValueError):
            continue
        normalized[intent] = round(max(0.0, min(score, 1.0)), 2)

    return normalized or fallback


def _extract_completion_criteria(text: str) -> List[str]:
    """抽取用户显式完成标准，不把普通动作拆成步骤。"""
    criteria: List[str] = []
    patterns = [
        r"(?:要求|需要|必须|请确保|确保|完成标准是|成功标准是)[:：]?\s*([^。；;\n]+)",
        r"(?:so that|ensure that|must)\s+([^.;\n]+)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = match.group(1).strip(" ，,。.;；")
            if value:
                criteria.append(value)
    return _dedupe(criteria)


def _dedupe(values: List[Any]) -> List[Any]:
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _extract_entities(text: str) -> List[str]:
    """从用户输入中抽取简单实体 token。

    这是一个轻量 tokenizer，不是完整的命名实体识别器。它会根据常见中英文
    标点和空格切分文本，过滤单字符 token，并最多保留前 10 个 token。

    参数：
        text: 原始或已经清理过的用户输入文本。

    输入：
        任意自然语言字符串。

    输出：
        返回最多 10 个字符串 token，可作为实体、主题、文件名、工具名或规划
        提示使用。
    """
    separators = " ，,。.;；:：()（）[]【】{}"
    tokens = []
    current = []

    for char in text:
        if char in separators:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)

    if current:
        tokens.append("".join(current))

    return [token for token in tokens if len(token) > 1][:10]

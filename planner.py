"""Capability-first Planner 模块。

Planner 是从 Goal IR 到 Executable DAG 的编译器。它分六个阶段：

1. capability precheck 根据INTENT_CAPABILITY_MAP，映射获取 capability_candidates；
这里是严格按照规则来的；

2.planner_context，组装规划器的提示词；然后输入给分解step的函数；

3.decompose_goal_to_steps(goal, capability_candidates, planner_context)调用-> Step Decomposition，LLM，
_llm_decompose_goal_to_steps，按照规定schema进行结构化；

4.Step-Capability Binding，使用关键字段kinds 和 tool registry表中的capability进行绑定，生成step_capability_dag，
原来的stepDAG节点增加了capability类型；

5.Tool Binding调用： bind_tools(step_capability_dag)根据 capability 严格从 tool registry 根据函数名找工具：节点
名字绑定工具函数名字，并且带有绑定状态和input schema;

6. Argument Binding
    argument_bindings = bind_arguments(
        goal,
        step_capability_dag,
        tool_bindings,
        planner_context=planner_context,
        state=state,
    )
    输入解释 ：
        goal：整体 Goal IR
        step_capability_dag：每个 step 的 objective / kind / input_hints / capability
        tool_bindings：每个 step 绑定到哪个 tool，以及这个 tool 的 input_schema
        输入state快照和提示词上下文
    使用绑定的上下文给LLM，LLM要求输入的上下文包括goal IR，step_cap_dag,tool-binds ，和当前状态快照；
    LLM 参考了 goal IR 中的constrait,step 中的input_hints（线索） ，tool binds中的input shcema，生成候选参数      
    输出候选参数后,在argument validate中 使用schema validate函数，对生成的候选参数和tool input schema进行校验，
    校验不通过，则在message中append错误信息，重新retry；校验通过则输出最终参数绑定。                
       

7.Executable DAG：生成 scheduler/worker/tool_dispatcher 可消费的 DAG。
    
  dag 是 planner 输出的 DAG，理论结构是：  
    
    {
    "nodes": [...],
    "edges": [...]
}

Goal IR
Goal IR 输入
来自 goalparser.py，现在字段是：
{
    "text": "...",
    "intents": [...],
    "entities": [...],
    "constraints": {...},
    "completion_criteria": [...],
    "ambiguity": {...},
    "interrupt_level": "...",
    "needs_clarification": bool,
    "intent_scores": {...}
}

-> capability precheck 根据INTENT_CAPABILITY_MAP，映射获取 capability_candidates；
这里是严格按照规则来的
  
  capability_candidates = [
    {
        "id": "c1",
        "capability": "file.read",
        "candidate_tools": ["read_text_file"],
        "selection_sources": ["keyword"],
        "score": ...
    }
]
  
-> planner_context，组装规划器的提示词；然后输入给分解step的函数
  
-> decompose_goal_to_steps(goal, capability_candidates, planner_context)
    调用-> Step Decomposition，LLM， _llm_decompose_goal_to_steps，按照规定schema进行结构化
    输出控制，并且定义一个_normalize_llm_step_dag(...) ，改函数对最终输出进行校验，包括使用pydantic
    model 进行基础格式校验，和对kinds 输出的需求枚举是否在定义的允许范围（工具能力先验）
    进行校验，input_hints 输出的允许的能力工具的输入参数是否在定义的允许范围内，最终输出step_dag和step_reasoning；
    还要depends. on 是否合理，是否DAG没有交叉进行验证；
    如果失败，则在输入message 中append 中，重新retry ； 
    输出生成的dag 包括step 节点 和 dependency 边，step 节点包括：
    step_dag = {
        "nodes": [
            {
                "id": "s1",
                "objective": "读取 planner.py",
                "kind": "file_read",
                "input_hints": {"path": "planner.py"},
                "depends_on": []
            }
        ],
        "edges": [        
        {
            "from": "s1",
            "to": "s2",
            "type": "data_dependency",
            "predicate": "status == success"
        }]
        
    }
 retry 边：
        "edges": [
            {
                "from": "s1",
                "to": "s1",
                "type": "retry",
                "predicate": "status == failed",
                "max_retry": 2
            }
        ]

-> Step-Capability Binding
  
    bind_step_capabilities(goal, step_dag, capability_candidates)
    把每个 step 的 kind 映射成 tool registry 支持的 capability：
    s1.kind = "file_read"
    -> capability = "file.read"
    -> candidate_tools = ["read_text_file"]
    输出：
    step_capability_dag :在step dag的节点上，衍生细化节点带有capability
    
        step = {
        "id": "s1",
        "objective": "读取 planner.py",
        "kind": "file_read",
        "input_hints": {"path": "planner.py"},
        "capability": "file.read"
    }

    bind_step_capabilities(...) ：逻辑是：
    step.kind 在 STEP_KIND_DEFINITIONS 里查找映射关系
    找 registry 里支持的 capability
    -> 写入 step_capability_dag.nodes[i]["capability"]
  
  
-> Tool Binding  
    Tool Binding：严格的规则关系，和tool regisry 里支持的 capability 一一对应，不能随意绑定。
    调用： bind_tools(step_capability_dag)
    根据 capability 从 tool registry 找工具：
    file.read -> read_text_file
    database.read -> select_rows
    输出：
        tool_bindings = {
            "s1": {
                "status": "bound",
                "tool": "read_text_file",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"} #实际上就是inout schema
                    },
                    "required": ["path"]
                }
            }
        }
 
->  planner_arguments_context  
-> Argument Binding：使用绑定的上下文给LLM，LLM要求输入的上下文包括goal IR，step_cap_dag,tool-binds ，和当前状态快照；
LLM 参考了 goal IR 中的constrait,step 中的input_hints（线索） ，tool binds中的input shcema，生成候选参数
        
        argument_bindings = bind_arguments(
            goal,
            step_capability_dag,
            tool_bindings,
            planner_context=planner_context,
            state=state,
        )
       然后输出候选参数

            argument_bindings = {
                "s1": {
                    "status": "bound",
                    "input": {
                        "path": "planner.py"
                    },
                    "source": "rule"  # 或 "llm+rule"
                }
            }    
    输出候选参数后,在argument validate中 使用schema validate函数，对生成的候选参数和tool input schema进行校验，
    校验不通过，则在message中append错误信息，重新retry；校验通过则输出最终参数绑定。      
       
  
  -> Executable DAG
        调用：
            build_executable_dag(step_capability_dag, tool_bindings, argument_bindings)
            把step id 做node id 映射 ；
            s1 -> n1
            s2 -> n2
            最终节点长这样：
                {
                    "id": "n1",
                    "step_id": "s1",
                    "objective": "读取 planner.py",
                    "step_kind": "file_read",
                    "capability": "file.read",
                    "tool": "read_text_file",
                    "input": {"path": "planner.py"},
                    "binding_status": "bound",
                    "argument_status": "bound",
                    "input_schema": {...},
                    "output_schema": {...},
                    "sandbox": {...},
                    "limits": {...}
                }
        边也会同步映射：
            step_capability_dag["edges"] = [{"from": "s1", "to": "s2"}]
            变成：executable_dag["edges"] = [{"from": "n1", "to": "n2"}]
  
  
  -> Human Interrupt 插入
        
        如果：goal 不明确/ 没有工具支持/需要审批/参数缺失
        就插入 human interrupt。
        
        if goal.get("needs_clarification") or goal.get("interrupt_level") == "high":
        当goal分解时候出现，need clradiction ，插入到DAG的最前边，
        _prepend_human_interrupt(...)
        
        它先找入口节点：入口节点就是没有上游依赖的节点。
            incoming_targets = {
                edge["to"]
                for edge in dag["edges"]
                if edge["from"] != edge["to"]
            }

            entry_node_ids = [
                node["id"]
                for node in dag["nodes"]
                if node["id"] not in incoming_targets
            ]           
            然后把 human node 放到 nodes 最前面：
            dag["nodes"] = [interrupt_node, *dag["nodes"]]
        
        参数缺失：插到缺参数节点前 if node.get("argument_status") == "needs_clarification":
        工具未绑定：插到未绑定节点前 if node.get("binding_status") != "bound":
        需要审批：插到需审批节点前 permission = node["sandbox"]["permission"]
        add_human_interrupt_nodes(executable_dag, goal) 
        它会把所有指向目标节点的边改到 interrupt：再新增一条interrupt -> target
        
  
  


    
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, TypedDict

from context_manager import ContextManager
from sandbox.policy import build_sandbox_config_for_node
from tools.tool_registry import iter_tool_specs
from tools.tool_type import ToolSpec

try:
    from langgraph.graph import StateGraph
except ImportError:  # pragma: no cover
    StateGraph = None


CapabilityNode = Dict[str, Any]
TaskStep = Dict[str, Any]
StepCapabilityNode = Dict[str, Any]
Dag = Dict[str, Any]


class State(TypedDict, total=False):
    """Planner 节点使用的状态结构。"""

    session_id: str
    task_id: str
    goal: Dict[str, Any]
    context: Dict[str, Any]
    context_manager: Any
    planner_context: Dict[str, Any]
    skill_context_bundle: Dict[str, Any]
    capability_match_report: Dict[str, Any]
    capability_candidates: List[CapabilityNode]
    step_plan: List[TaskStep]
    step_dag: Dag
    step_reasoning: Dict[str, Any]
    step_capability_plan: List[StepCapabilityNode]
    step_capability_dag: Dag
    step_capability_reasoning: Dict[str, Any]
    plan_candidates: List[Dict[str, Any]]
    selected_plan_id: str
    plan_policy: Dict[str, Any]
    tool_bindings: Dict[str, Dict[str, Any]]
    argument_bindings: Dict[str, Dict[str, Any]]
    dag: Dag


INTENT_CAPABILITY_MAP: dict[str, list[str]] = {
    "search": ["search.query"],
    "analysis": ["text.analyze"],
    "summarization": ["text.summarize"],
    "planning": ["plan.create"],
    "codebase_analysis": [
        "code.scan_tree",
        "code.extract_symbols",
        "code.summarize_files",
        "code.architecture.summarize",
    ],
    "code_search": ["code.search"],
    "log_analysis": ["log.parse_timeline"],
    "incident_diagnosis": [
        "log.parse_timeline",
        "code.search",
        "incident.diagnose",
    ],
    "tool_call": [],
    "qa": ["text.answer"],
}

KEYWORD_CAPABILITY_MAP: dict[str, list[str]] = {
    "图片": ["image.generate"],
    "图像": ["image.generate"],
    "生成图": ["image.generate"],
    "语音": ["audio.text_to_speech"],
    "音频": ["audio.text_to_speech"],
    "tts": ["audio.text_to_speech"],
    "文件": ["file.read"],
    "读取": ["file.read"],
    "打开": ["file.read"],
    "目录": ["directory.list"],
    "列出": ["directory.list"],
    "查看目录": ["directory.list"],
    "写入": ["file.write"],
    "保存": ["file.write"],
    "替换": ["file.replace"],
    "删除": ["file.delete"],
    "sqlite": ["database.read"],
    "数据库": ["database.read"],
    "代码": ["code.scan_tree", "code.extract_symbols", "code.summarize_files"],
    "工程": ["code.scan_tree", "code.extract_symbols", "code.architecture.summarize"],
    "架构": ["code.architecture.summarize"],
    "代码框架": ["code.architecture.summarize"],
    "函数": ["code.extract_symbols", "code.summarize_files"],
    "符号": ["code.extract_symbols"],
    "调用链": ["code.search"],
    "code": ["code.scan_tree", "code.search"],
    "codebase": ["code.scan_tree", "code.architecture.summarize"],
    "architecture": ["code.architecture.summarize"],
    "symbol": ["code.extract_symbols"],
    "日志": ["log.parse_timeline"],
    "log": ["log.parse_timeline"],
    "timeline": ["log.parse_timeline"],
    "问题": ["incident.diagnose"],
    "故障": ["incident.diagnose"],
    "根因": ["incident.diagnose"],
    "原因": ["incident.diagnose"],
    "修复": ["incident.diagnose"],
    "incident": ["incident.diagnose"],
    "diagnose": ["incident.diagnose"],
}

STEP_KIND_DEFINITIONS: dict[str, dict[str, Any]] = {
    "file_read": {
        "label": "读取文件",
        "capabilities": ["file.read", "text.read"],
        "input_hint_fields": ["path"],
    },
    "directory_list": {
        "label": "列出目录",
        "capabilities": ["directory.list"],
        "input_hint_fields": ["path", "include_hidden"],
    },
    "file_write": {
        "label": "写入文件",
        "capabilities": ["file.write", "text.write"],
        "input_hint_fields": ["path", "content", "create_dirs"],
    },
    "file_append": {
        "label": "追加文件",
        "capabilities": ["file.append", "text.write"],
        "input_hint_fields": ["path", "content", "create_dirs"],
    },
    "file_replace": {
        "label": "替换文件内容",
        "capabilities": ["file.replace", "text.edit"],
        "input_hint_fields": ["path", "old", "new"],
    },
    "file_delete": {
        "label": "删除文件",
        "capabilities": ["file.delete"],
        "input_hint_fields": ["path", "missing_ok"],
    },
    "database_read": {
        "label": "读取数据库",
        "capabilities": ["database.read", "row.select"],
        "input_hint_fields": ["db_path", "table", "filters", "limit"],
    },
    "database_write": {
        "label": "写入数据库",
        "capabilities": ["database.write", "row.insert", "row.update", "row.delete"],
        "input_hint_fields": ["db_path", "table", "row", "filters", "updates"],
    },
    "database_schema": {
        "label": "维护数据库结构",
        "capabilities": ["database.schema", "table.create", "database.ensure"],
        "input_hint_fields": ["db_path", "table", "columns"],
    },
    "search": {
        "label": "搜索或查询信息",
        "capabilities": ["search.query"],
        "input_hint_fields": ["query"],
    },
    "codebase_scan": {
        "label": "扫描代码工程目录",
        "capabilities": ["code.scan_tree", "codebase.scan", "code.tree"],
        "input_hint_fields": ["project_path", "include_ext", "exclude_dirs", "max_files", "max_depth"],
    },
    "code_symbol_extract": {
        "label": "提取代码符号",
        "capabilities": ["code.extract_symbols", "code.symbol.extract", "code.symbols"],
        "input_hint_fields": [
            "project_path",
            "include_ext",
            "exclude_dirs",
            "max_files",
            "max_symbols_per_file",
        ],
    },
    "code_file_summarize": {
        "label": "总结代码文件",
        "capabilities": ["code.summarize_files", "code.file.summarize", "text.summarize"],
        "input_hint_fields": [
            "project_path",
            "include_ext",
            "exclude_dirs",
            "max_files",
            "max_chars_per_file",
        ],
    },
    "code_architecture_summarize": {
        "label": "总结代码工程架构",
        "capabilities": [
            "code.summarize_architecture",
            "code.architecture.summarize",
            "codebase.architecture",
        ],
        "input_hint_fields": ["project_path", "include_ext", "exclude_dirs", "max_files"],
    },
    "code_search": {
        "label": "检索代码和配置",
        "capabilities": ["code.search", "code.retrieve", "search.query"],
        "input_hint_fields": [
            "project_path",
            "query",
            "include_ext",
            "exclude_dirs",
            "max_files",
            "max_matches",
            "context_lines",
        ],
    },
    "log_timeline_parse": {
        "label": "解析日志时间线",
        "capabilities": ["log.parse_timeline", "log.timeline", "log.parse"],
        "input_hint_fields": ["log_text", "log_path", "event_time", "max_events"],
    },
    "incident_diagnose": {
        "label": "诊断问题根因",
        "capabilities": ["incident.diagnose", "code.incident.diagnose", "root_cause.analyze"],
        "input_hint_fields": [
            "problem_description",
            "event_time",
            "project_path",
            "log_text",
            "log_path",
            "architecture_summary",
            "search_query",
            "max_code_matches",
        ],
    },
    "extract": {
        "label": "抽取信息",
        "capabilities": ["text.extract"],
        "input_hint_fields": ["text", "source_step_ids", "fields"],
    },
    "plan_create": {
        "label": "生成计划",
        "capabilities": ["plan.create"],
        "input_hint_fields": ["text"],
    },
    "shell_command": {
        "label": "执行 shell 命令",
        "capabilities": ["command.run", "shell", "subprocess"],
        "input_hint_fields": ["command", "cwd", "timeout_seconds"],
    },
    "image_generate": {
        "label": "生成图片",
        "capabilities": ["image.generate", "image.text_to_image"],
        "input_hint_fields": ["prompt"],
    },
    "audio_tts": {
        "label": "文本转语音",
        "capabilities": ["audio.text_to_speech", "audio.generate"],
        "input_hint_fields": ["text", "voice_id"],
    },
    "analyze": {
        "label": "分析内容",
        "capabilities": ["text.analyze"],
        "input_hint_fields": ["text", "source_step_ids"],
    },
    "summarize": {
        "label": "总结内容",
        "capabilities": ["text.summarize"],
        "input_hint_fields": ["text", "source_step_ids"],
    },
    "answer": {
        "label": "回答问题",
        "capabilities": ["text.answer"],
        "input_hint_fields": ["question", "text", "source_step_ids"],
    },
}


def build_plan(state: State) -> State:
    """根据 ``state["goal"]`` 构建完整 Executable DAG。

    作用：
        Planner 的 LangGraph 节点入口，也是普通 Python 调用入口。它把
        goalparser 产出的 Goal IR 编译为 scheduler/worker 可执行的 DAG。

    输入：
        state: 至少包含 ``goal`` 字段的状态字典。``goal`` 一般包含
        ``text``、``intents``、``intent_scores``、``completion_criteria``、
        ``ambiguity`` 等字段。

    输出：
        返回同一个 state，并写入中间/最终产物：
        ``step_plan``、``step_dag``、``step_capability_plan``、
        ``step_capability_dag``、``tool_bindings``、``argument_bindings``、``dag``。

    核心计算逻辑：
        依次调用 step 分解、step capability 绑定、工具绑定、参数绑定、
        可执行 DAG 构建。LLM step 分解失败时会自动回退到规则式 step DAG。

    设计意图：
        保留 planner 的“编译器流水线”形态，让后续可以独立升级某一阶段，
        例如把规则式能力分解替换为 LLM 分解，或把简单工具绑定替换为评分排序。
    """
    goal = state["goal"]
    context = state.get("context", {})
    skill_context_bundle = build_skill_context_for_goal(state, goal)
    state["skill_context_bundle"] = skill_context_bundle

    if goal.get("needs_clarification") or goal.get("interrupt_level") == "high":
        executable_dag = build_goal_clarification_dag(goal)
        state["planner_context"] = build_planner_stage_context(
            state,
            goal,
            [],
            skill_context_bundle=skill_context_bundle,
        )
        state["capability_candidates"] = []
        state["capability_match_report"] = {
            "status": "needs_clarification",
            "stage": "goal_parser",
            "reason": "goal_needs_clarification",
            "goal_text": goal.get("text", ""),
            "suggested_action": "ask_clarification",
        }
        state["step_plan"] = []
        state["step_dag"] = {"nodes": [], "edges": []}
        state["step_reasoning"] = {
            "source": "planner_guard",
            "reason": "goal_needs_clarification",
        }
        state["step_capability_plan"] = []
        state["step_capability_dag"] = {"nodes": [], "edges": []}
        state["step_capability_reasoning"] = state["step_reasoning"]
        state["plan_candidates"] = [
            {
                "id": "p_goal_clarification",
                "mode": "fallback",
                "intent_hypothesis": goal.get("intents", []),
                "score": 0.0,
                "goal": goal,
                "capability_candidates": [],
                "capability_match_report": state["capability_match_report"],
                "step_dag": state["step_dag"],
                "step_reasoning": state["step_reasoning"],
                "step_capability_dag": state["step_capability_dag"],
                "step_capability_reasoning": state["step_capability_reasoning"],
                "tool_bindings": {},
                "argument_bindings": {},
                "dag": executable_dag,
            }
        ]
        state["selected_plan_id"] = "p_goal_clarification"
        state["plan_policy"] = {
            "mode": "fallback",
            "candidate_count": 1,
            "reason": "goal needs clarification; generated interrupt-only DAG",
        }
        state["tool_bindings"] = {}
        state["argument_bindings"] = {}
        state["dag"] = executable_dag
        return state

    decomposition = decompose_goal_to_capabilities_with_report(
        goal,
        skill_context_bundle=skill_context_bundle,
    )
    capability_candidates = decomposition["candidates"]
    capability_match_report = decomposition["match_report"]

    state["capability_candidates"] = capability_candidates
    state["capability_match_report"] = capability_match_report

    if capability_match_report.get("status") == "unmatched":
        executable_dag = build_unmatched_capability_dag(goal, capability_match_report)
        state["planner_context"] = build_planner_stage_context(
            state,
            goal,
            capability_candidates,
            skill_context_bundle=skill_context_bundle,
        )
        state["step_plan"] = []
        state["step_dag"] = {"nodes": [], "edges": []}
        state["step_reasoning"] = {
            "source": "planner_guard",
            "reason": capability_match_report.get("reason"),
        }
        state["step_capability_plan"] = []
        state["step_capability_dag"] = {"nodes": [], "edges": []}
        state["step_capability_reasoning"] = {
            "source": "planner_guard",
            "reason": capability_match_report.get("reason"),
        }
        state["plan_candidates"] = [
            {
                "id": "p_unmatched",
                "mode": "fallback",
                "intent_hypothesis": goal.get("intents", []),
                "score": 0.0,
                "goal": goal,
                "capability_candidates": [],
                "capability_match_report": capability_match_report,
                "step_dag": state["step_dag"],
                "step_reasoning": state["step_reasoning"],
                "step_capability_dag": state["step_capability_dag"],
                "step_capability_reasoning": state["step_capability_reasoning"],
                "tool_bindings": {},
                "argument_bindings": {},
                "dag": executable_dag,
            }
        ]
        state["selected_plan_id"] = "p_unmatched"
        state["plan_policy"] = {
            "mode": "fallback",
            "candidate_count": 1,
            "reason": "capability decomposition unmatched; generated feedback interrupt DAG",
        }
        state["tool_bindings"] = {}
        state["argument_bindings"] = {}
        state["dag"] = executable_dag
        return state

    planner_context = build_planner_stage_context(
        state,
        goal,
        capability_candidates,
        skill_context_bundle=skill_context_bundle,
    )
    plan_candidates, plan_policy = build_plan_candidates(
        goal,
        capability_candidates=capability_candidates,
        capability_match_report=capability_match_report,
        planner_context=planner_context,
        state=state,
        context=context,
    )
    selected_plan = select_plan_candidate(plan_candidates)
    capability_match_report = selected_plan.get(
        "capability_match_report",
        capability_match_report,
    )
    step_dag = selected_plan.get("step_dag", {"nodes": [], "edges": []})
    step_plan = step_dag.get("nodes", [])
    step_reasoning = selected_plan.get("step_reasoning", {})
    step_capability_dag = selected_plan["step_capability_dag"]
    step_capability_plan = step_capability_dag["nodes"]
    step_capability_reasoning = selected_plan["step_capability_reasoning"]
    tool_bindings = selected_plan["tool_bindings"]
    argument_bindings = selected_plan["argument_bindings"]
    executable_dag = selected_plan["dag"]

    state["planner_context"] = planner_context
    state["capability_match_report"] = capability_match_report
    state["step_plan"] = step_plan
    state["step_dag"] = step_dag
    state["step_reasoning"] = step_reasoning
    state["step_capability_plan"] = step_capability_plan
    state["step_capability_dag"] = step_capability_dag
    state["step_capability_reasoning"] = step_capability_reasoning
    state["plan_candidates"] = plan_candidates
    state["selected_plan_id"] = selected_plan["id"]
    state["plan_policy"] = plan_policy
    state["tool_bindings"] = tool_bindings
    state["argument_bindings"] = argument_bindings
    state["dag"] = executable_dag
    return state


def build_plan_candidates(
    goal: dict[str, Any],
    *,
    capability_candidates: list[CapabilityNode],
    capability_match_report: dict[str, Any],
    planner_context: dict[str, Any] | None = None,
    state: State | None = None,
    context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """生成执行图候选。

    Planner 语义约定：
        - 不清晰目标已在 ``build_plan`` 开头走 human clarification interrupt。
        - 能走到这里的多个 intents 都是组合任务，应生成一个 combined DAG，
          不能让单 intent 候选互相竞争导致漏做用户明确要求。
        - Capability decomposition 是目标级能力池，只在前置阶段计算一次；
          本函数复用该池进行 step planning 和后续绑定。
    """
    intent_sets = _build_intent_candidate_sets(goal)
    plan_mode = "static" if len(intent_sets) == 1 else "dynamic"
    candidates: list[dict[str, Any]] = []

    for index, intents in enumerate(intent_sets, start=1):
        candidate_goal = _goal_for_intents(goal, intents)
        candidate = _compile_plan_candidate(
            candidate_id=f"p{index}",
            plan_mode=plan_mode,
            goal=candidate_goal,
            capability_candidates=capability_candidates,
            capability_match_report=capability_match_report,
            planner_context=planner_context,
            state=state,
            context=context,
        )
        candidates.append(candidate)

    return candidates, {
        "mode": plan_mode,
        "candidate_count": len(candidates),
        "reason": (
            "clear goal; generated one combined intent DAG"
            if plan_mode == "static"
            else "ambiguous goal; generated multiple plan candidates and reranked"
        ),
    }


def _compile_plan_candidate(
    *,
    candidate_id: str,
    plan_mode: str,
    goal: dict[str, Any],
    capability_candidates: list[CapabilityNode],
    capability_match_report: dict[str, Any],
    planner_context: dict[str, Any] | None,
    state: State | None,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """把清晰目标的组合 intent 编译成完整候选 DAG。"""
    if capability_match_report.get("status") == "unmatched":
        executable_dag = build_unmatched_capability_dag(goal, capability_match_report)
        step_dag = {"nodes": [], "edges": []}
        step_capability_dag = {"nodes": [], "edges": []}
        step_reasoning = {
            "source": "planner_guard",
            "reason": capability_match_report.get("reason"),
        }
        return {
            "id": candidate_id,
            "mode": plan_mode,
            "intent_hypothesis": goal.get("intents", []),
            "score": 0.0,
            "goal": goal,
            "capability_candidates": [],
            "capability_match_report": capability_match_report,
            "step_dag": step_dag,
            "step_reasoning": step_reasoning,
            "step_capability_dag": step_capability_dag,
            "step_capability_reasoning": step_reasoning,
            "tool_bindings": {},
            "argument_bindings": {},
            "dag": executable_dag,
        }

    step_dag, step_reasoning = decompose_goal_to_steps(
        goal,
        capability_candidates,
        planner_context=planner_context,
    )
    step_capability_dag, step_capability_reasoning = bind_step_capabilities(
        goal,
        step_dag,
        capability_candidates,
    )
    tool_bindings = bind_tools(
        step_capability_dag,
        goal=goal,
        planner_context=planner_context,
        state=state,
    )
    argument_bindings = bind_arguments(
        goal,
        step_capability_dag,
        tool_bindings,
        planner_context=planner_context,
        state=state,
    )
    executable_dag = build_executable_dag(
        step_capability_dag,
        tool_bindings,
        argument_bindings,
        context=context,
    )
    executable_dag = add_human_interrupt_nodes(executable_dag, goal)
    score = score_plan_candidate(
        goal,
        step_capability_dag,
        tool_bindings,
        argument_bindings,
    )
    executable_dag.setdefault("metadata", {})
    executable_dag["metadata"].update({
        "plan_id": candidate_id,
        "plan_mode": plan_mode,
        "intent_hypothesis": goal.get("intents", []),
        "plan_score": score,
    })

    return {
        "id": candidate_id,
        "mode": plan_mode,
        "intent_hypothesis": goal.get("intents", []),
        "score": score,
        "goal": goal,
        "capability_candidates": capability_candidates,
        "capability_match_report": capability_match_report,
        "step_dag": step_dag,
        "step_reasoning": step_reasoning,
        "step_capability_dag": step_capability_dag,
        "step_capability_reasoning": step_capability_reasoning,
        "tool_bindings": tool_bindings,
        "argument_bindings": argument_bindings,
        "dag": executable_dag,
    }


def _build_intent_candidate_sets(goal: dict[str, Any]) -> list[list[str]]:
    """从 Goal IR 构造 intent 集合。

    Planner 语义：
        不清晰目标在 build_plan 开头已经中断澄清；因此这里不再把多个
        intents 当成竞争候选。多个明确 intents 表示组合任务，必须一起执行。
    """
    intents = [
        intent
        for intent in _dedupe([str(intent) for intent in goal.get("intents", [])])
        if intent in INTENT_CAPABILITY_MAP
    ]
    if not intents:
        return [["qa"]]
    return [intents]


def _goal_for_intents(goal: dict[str, Any], intents: list[str]) -> dict[str, Any]:
    """复制 Goal IR，并限制为当前 intent 假设。"""
    candidate_goal = dict(goal)
    candidate_goal["intents"] = intents
    intent_scores = goal.get("intent_scores") or {}
    candidate_goal["intent_scores"] = {
        intent: intent_scores.get(intent, 0.5)
        for intent in intents
    }
    candidate_goal["intent_hypothesis"] = intents
    return candidate_goal


def select_plan_candidate(plan_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """确定性 rerank 候选 DAG，返回最终执行图。"""
    if not plan_candidates:
        raise ValueError("planner 未生成任何 plan candidate")
    return max(plan_candidates, key=lambda candidate: candidate.get("score", 0.0))


def score_plan_candidate(
    goal: dict[str, Any],
    step_capability_dag: Dag,
    tool_bindings: dict[str, dict[str, Any]],
    argument_bindings: dict[str, dict[str, Any]],
) -> float:
    """给候选 DAG 打分，用于多 intent plan space 的 rerank。"""
    raw_node_count = len(step_capability_dag.get("nodes", []))
    if raw_node_count == 0:
        return 0.0
    node_count = max(1, raw_node_count)
    bound_count = sum(
        1 for binding in tool_bindings.values()
        if binding.get("status") == "bound"
    )
    argument_bound_count = sum(
        1 for binding in argument_bindings.values()
        if binding.get("status") == "bound"
    )
    candidate_intents = goal.get("intents") or []
    intent_score = _max_score_for_intents(goal, candidate_intents, 0.5)
    tool_score = bound_count / node_count
    argument_score = argument_bound_count / node_count
    graph_score = 1.0 if not _has_cycle(
        step_capability_dag.get("nodes", []),
        [
            edge for edge in step_capability_dag.get("edges", [])
            if edge.get("type") != "retry"
        ],
    ) else 0.0
    return round(
        0.45 * intent_score
        + 0.30 * tool_score
        + 0.20 * argument_score
        + 0.05 * graph_score,
        4,
    )


def retrieve_capability_candidates(goal: dict[str, Any]) -> list[CapabilityNode]:
    """从 Goal IR 检索候选 capability。

    作用：
        作为 LLM DAG 推理前的候选召回层，给模型一个受控的 capability 空间。

    输入：
        goal: goalparser 产出的 Goal IR。

    输出：
        候选 CapabilityNode 列表。这里只说明“可能需要哪些能力”，不决定依赖边。

    核心计算逻辑：
        复用规则式 ``decompose_goal_to_capabilities``，从 intent 和 keyword
        召回候选 capability。

    设计意图：
        让 LLM 在候选集合里做 DAG 推理，降低幻觉 capability 的概率。
    """
    return decompose_goal_to_capabilities(goal)


def decompose_goal_to_capabilities_with_report(
    goal: dict[str, Any],
    *,
    skill_context_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return capability candidates plus a match report for planner branching."""
    proposals = _propose_capabilities(goal, skill_context_bundle=skill_context_bundle)
    proposed_capabilities = _dedupe([
        proposal["capability"]
        for proposal in proposals
    ])
    supported_capability_index = _build_supported_capability_index()
    available_capabilities = _order_capabilities(list(supported_capability_index))
    supported_capabilities = [
        capability
        for capability in proposed_capabilities
        if capability in supported_capability_index
    ]
    unsupported_proposals = [
        capability
        for capability in proposed_capabilities
        if capability not in supported_capability_index
    ]

    if not proposed_capabilities:
        return {
            "candidates": [],
            "match_report": _build_capability_match_report(
                goal,
                status="unmatched",
                reason="no_capability_proposals",
                proposed_capabilities=[],
                supported_capabilities=[],
                unsupported_proposals=[],
                available_capabilities=available_capabilities,
                suggested_action="ask_clarification",
            ),
        }

    if not supported_capabilities:
        return {
            "candidates": [],
            "match_report": _build_capability_match_report(
                goal,
                status="unmatched",
                reason="proposed_capabilities_not_supported",
                proposed_capabilities=proposed_capabilities,
                supported_capabilities=[],
                unsupported_proposals=unsupported_proposals,
                available_capabilities=available_capabilities,
                suggested_action="report_unsupported",
            ),
        }

    intent_scores = goal.get("intent_scores") or {}
    default_score = max(intent_scores.values(), default=0.5)
    candidates = [
        {
            "id": f"c{i}",
            "capability": capability,
            "description": _describe_capability(capability),
            "score": _score_capability(capability, goal, default_score),
            "source_goal_text": goal.get("text", ""),
            "candidate_tools": [
                spec.name for spec in supported_capability_index.get(capability, [])
            ],
            "selection_sources": _proposal_sources_for_capability(proposals, capability),
            "selection_reason": "matched_supported_capability",
            "fallback_used": False,
        }
        for i, capability in enumerate(supported_capabilities, start=1)
    ]

    return {
        "candidates": candidates,
        "match_report": _build_capability_match_report(
            goal,
            status="matched",
            reason="matched_supported_capability",
            proposed_capabilities=proposed_capabilities,
            supported_capabilities=supported_capabilities,
            unsupported_proposals=unsupported_proposals,
            available_capabilities=available_capabilities,
            suggested_action=None,
        ),
    }


def _propose_capabilities(
    goal: dict[str, Any],
    *,
    skill_context_bundle: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Collect raw capability proposals from intent, capability hints, and keywords."""
    proposals: list[dict[str, Any]] = []

    for intent in goal.get("intents", []):
        for capability in INTENT_CAPABILITY_MAP.get(intent, []):
            proposals.append({
                "capability": capability,
                "source": "intent",
                "reason": str(intent),
            })

    lowered_text = str(goal.get("text", "")).lower()
    for keyword, keyword_capabilities in KEYWORD_CAPABILITY_MAP.items():
        if keyword in lowered_text:
            for capability in keyword_capabilities:
                proposals.append({
                    "capability": capability,
                    "source": "keyword",
                    "reason": keyword,
                })

    for capability in _extract_skill_capability_hints(skill_context_bundle):
        proposals.append({
            "capability": capability,
            "source": "skill",
            "reason": "skill_context_bundle.capability_hints",
        })

    return proposals


def _proposal_sources_for_capability(
    proposals: list[dict[str, Any]],
    capability: str,
) -> list[str]:
    return _dedupe([
        str(proposal.get("source"))
        for proposal in proposals
        if proposal.get("capability") == capability
    ])


def _build_capability_match_report(
    goal: dict[str, Any],
    *,
    status: str,
    reason: str,
    proposed_capabilities: list[str],
    supported_capabilities: list[str],
    unsupported_proposals: list[str],
    available_capabilities: list[str],
    suggested_action: str | None,
) -> dict[str, Any]:
    return {
        "status": status,
        "stage": "capability_decomposition",
        "reason": reason,
        "goal_text": goal.get("text", ""),
        "proposed_capabilities": proposed_capabilities,
        "supported_capabilities": supported_capabilities,
        "unsupported_proposals": unsupported_proposals,
        "available_capabilities": available_capabilities,
        "suggested_action": suggested_action,
    }


def build_planner_stage_context(
    state: State,
    goal: dict[str, Any],
    capability_candidates: list[CapabilityNode],
    *,
    skill_context_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 planner 阶段专用 context。

    优先使用 ContextManager.build_planner_context；如果 session_id/Redis 不可用，
    则回退到 state["context"]，保证 planner 仍能在无持久化环境下运行。
    """
    session_id = state.get("session_id") or (state.get("context") or {}).get("session_id")
    context_manager = state.get("context_manager")

    if context_manager is None:
        try:
            context_manager = ContextManager()
        except Exception:
            context_manager = None

    if session_id and context_manager is not None:
        try:
            return context_manager.build_planner_context(
                session_id,
                goal,
                capability_candidates=capability_candidates,
                include_tools=True,
            ) | _skill_context_overlay(skill_context_bundle)
        except Exception as exc:  # noqa: BLE001 - context 不应阻断 planner fallback
            fallback = dict(state.get("context", {}))
            fallback["stage"] = "planner"
            fallback["context_error"] = str(exc)
            fallback["goal"] = goal
            fallback["capability_candidates"] = capability_candidates
            fallback.update(_skill_context_overlay(skill_context_bundle))
            return fallback

    fallback = dict(state.get("context", {}))
    fallback.setdefault("stage", "planner")
    fallback.setdefault("goal", goal)
    fallback.setdefault("capability_candidates", capability_candidates)
    fallback.update(_skill_context_overlay(skill_context_bundle))
    return fallback


def build_skill_context_for_goal(state: State, goal: dict[str, Any]) -> dict[str, Any]:
    """为 capability decomposition 和 planner context 提前构建 skill bundle。"""
    existing = state.get("skill_context_bundle") or (state.get("context") or {}).get("skill_context_bundle")
    if isinstance(existing, dict):
        return existing

    context_manager = state.get("context_manager")
    if context_manager is None:
        try:
            context_manager = ContextManager()
        except Exception:
            context_manager = None

    if context_manager is not None and hasattr(context_manager, "build_skill_context_bundle"):
        try:
            return context_manager.build_skill_context_bundle(
                goal,
                context=state.get("context", {}),
            )
        except Exception:
            pass

    try:
        from context_manager import normalize_skill_roots
        from skills.context_builder import SkillContextBuilder
        from skills.registry import SkillRegistry

        context = state.get("context") or {}
        metadata = context.get("metadata") or {}
        roots = normalize_skill_roots(
            metadata.get("skill_roots") or metadata.get("skill_root")
        )
        if roots:
            registry = SkillRegistry(roots)
            registry.scan()
            return SkillContextBuilder(registry=registry).build_for_goal(goal).to_dict()
    except Exception:
        pass

    return {
        "matched_skills": [],
        "skill_contexts": {},
        "reference_contexts": {},
        "capability_hints": [],
        "recommended_step_kinds": [],
        "default_resources": {},
    }


def _skill_context_overlay(skill_context_bundle: dict[str, Any] | None) -> dict[str, Any]:
    if skill_context_bundle and skill_context_bundle.get("matched_skills"):
        return {"skill_context_bundle": skill_context_bundle}
    return {}


def _extract_skill_capability_hints(
    skill_context_bundle: dict[str, Any] | None,
) -> list[str]:
    if not isinstance(skill_context_bundle, dict):
        return []
    return _dedupe([
        str(capability)
        for capability in skill_context_bundle.get("capability_hints", [])
        if str(capability).strip()
    ])


def _extract_skill_step_kind_hints(
    planner_context: dict[str, Any] | None,
) -> list[str]:
    bundle = (planner_context or {}).get("skill_context_bundle") or {}
    if not isinstance(bundle, dict):
        return []
    return _dedupe([
        str(kind)
        for kind in bundle.get("recommended_step_kinds", [])
        if str(kind).strip()
    ])


def decompose_goal_to_capabilities(goal: dict[str, Any]) -> list[CapabilityNode]:
    """
    Capability Decomposition（能力分解器）

    ------------------------------------------------------------
    作用：
        将 Goal IR（结构化用户目标）转换为“能力节点列表（Capability Nodes）”。

        这里的核心不是生成执行步骤，而是：
        ❗把自然语言意图映射到“能力空间（Capability Space）”

    ------------------------------------------------------------
    输入（Input）：
        goal: dict[str, Any]

        典型结构包括：

        {
            "text": str,                 # 用户原始输入
            "intents": List[str],        # parser识别出的意图
            "constraints": dict,         # 用户显式约束
            "completion_criteria": List[str], # 用户显式完成标准
            "intent_scores": dict        # 各intent置信度
        }

    ------------------------------------------------------------
    输出（Output）：
        List[CapabilityNode]

        每个 capability node 结构如下：

        {
            "id": "c1",                     # capability节点ID（全局唯一）
            "capability": "text.analyze",   # 能力类型（核心语义单元）
            "description": str,             # 能力语义描述（用于debug/解释）
            "score": float,                 # 当前能力与goal匹配分数
            "source_goal_text": str         # 原始用户输入
        }

    ------------------------------------------------------------
    核心计算逻辑（Pipeline）：

        Step 1：Intent → Capability 映射
            从 goal["intents"] 出发，通过 INTENT_CAPABILITY_MAP
            将高层意图转换为能力集合。

        Step 2：Keyword → Capability 映射（兜底规则）
            在文本中匹配关键词（如“语音/文件/数据库”）
            触发对应能力。

        Step 3：能力去重（dedup）
            防止不同来源重复生成相同 capability。

        Step 4：score 计算（重要性评估）
            根据 intent_scores + capability 类型推断
            计算 capability 与 goal 的相关性分数

        Step 5：构建 CapabilityNode 列表
            为每个 capability 分配唯一 id（c1, c2, c3...）

    ------------------------------------------------------------
    设计意图（非常关键）：

        ❗该函数不做“任务拆解（task planning）”
        ❗只做“能力投影（capability projection）”

        即：

            Goal → Capability Space（集合层）

        后续 DAG 构建（dependency + tool binding）在 planner 后续阶段完成

    ------------------------------------------------------------
    """

    return decompose_goal_to_capabilities_with_report(goal)["candidates"]


def _build_supported_capability_index() -> dict[str, list[ToolSpec]]:
    """从 tool registry 构建 capability 白名单。

    Capability Decomposition 只能从这个索引中选择能力；intent/keyword map 只
    负责提名，不能让未注册工具支持的 capability 进入 executable DAG。
    """
    index: dict[str, list[ToolSpec]] = {}
    for spec in iter_tool_specs(enabled_only=True):
        for capability in spec.capabilities:
            index.setdefault(capability, []).append(spec)
    return index


def decompose_goal_to_steps(
    goal: dict[str, Any],
    capability_candidates: list[CapabilityNode],
    planner_context: dict[str, Any] | None = None,
) -> tuple[Dag, dict[str, Any]]:
    """把 Goal IR 分解为 step DAG。

    Step 是 planner 的执行语义主语；capability 只在下一阶段作为 step 的能力
    标签。这里优先让 LLM 理解任务步骤和依赖，失败时回退到规则式文本拆分。
    """
    llm_result = _llm_decompose_goal_to_steps(
        goal,
        capability_candidates,
        planner_context=planner_context,
    )
    if llm_result is not None:
        normalized = _normalize_llm_step_dag(
            llm_result,
            capability_candidates=capability_candidates,
            planner_context=planner_context,
        )
        if normalized is not None:
            return normalized, {
                "source": "llm",
                "reason": llm_result.get("reason", ""),
            }

    fallback_dag = build_fallback_step_dag(
        goal,
        capability_candidates=capability_candidates,
        planner_context=planner_context,
    )
    return fallback_dag, {
        "source": "fallback",
        "reason": "LLM step decomposition unavailable or invalid; used rule fallback.",
    }


def _llm_decompose_goal_to_steps(
    goal: dict[str, Any],
    capability_candidates: list[CapabilityNode],
    planner_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """调用 LLM 生成 step DAG 候选。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        return None

    client = OpenAI(
        api_key=api_key,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
    payload = {
        "goal": goal,
        "capability_candidates": capability_candidates,
        "available_step_kinds": build_step_kind_index(
            capability_candidates=capability_candidates,
            planner_context=planner_context,
        ),
    }
    if planner_context:
        payload["planner_context"] = planner_context

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a task step decomposition module for an agent planner. "
                        "Return valid json only. Decompose the goal into concrete task steps. "
                        "Do not choose tools. Do not use capability ids as steps. "
                        "Choose kind only from available_step_kinds. "
                        "Never invent kinds or capabilities. "
                        "Use only input_hints fields allowed by the selected kind. "
                        "Use completion_criteria as required outcomes when deciding steps. "
                        "Use depends_on to express execution dependency. "
                        "Return shape: "
                        "{\"steps\":[{\"id\":\"s1\",\"objective\":\"...\","
                        "\"kind\":\"file_read\",\"input_hints\":{},"
                        "\"depends_on\":[],\"reason\":\"...\"}],"
                        "\"reason\":\"...\"}."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            response_format={"type": "json_object"},
        )
    except Exception:
        return None

    try:
        parsed = json.loads(response.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_llm_step_dag(
    llm_result: dict[str, Any],
    *,
    capability_candidates: list[CapabilityNode] | None = None,
    planner_context: dict[str, Any] | None = None,
) -> Dag | None:
    """校验并规范化 LLM 输出的 step DAG。"""
    raw_steps = llm_result.get("steps") or llm_result.get("nodes")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None

    nodes: list[TaskStep] = []
    seen_ids: set[str] = set()
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            return None
        step_id = str(raw_step.get("id") or f"s{index}")
        if not re.fullmatch(r"s\d+", step_id) or step_id in seen_ids:
            return None
        objective = str(raw_step.get("objective") or raw_step.get("description") or "").strip()
        if not objective:
            return None
        input_hints = raw_step.get("input_hints") or {}
        if not isinstance(input_hints, dict):
            input_hints = {}
        kind = _normalize_step_kind(
            raw_step.get("kind"),
            objective=objective,
            available_step_kinds=build_step_kind_index(
                capability_candidates=capability_candidates,
                planner_context=planner_context,
            ),
        )
        if kind == "unsupported":
            return None
        input_hints = _filter_step_input_hints(kind, input_hints)
        depends_on = _normalize_step_dependencies(raw_step.get("depends_on"))
        node = {
            "id": step_id,
            "objective": objective,
            "kind": kind,
            "input_hints": input_hints,
            "depends_on": depends_on,
        }
        if raw_step.get("reason"):
            node["reason"] = str(raw_step["reason"])
        nodes.append(node)
        seen_ids.add(step_id)

    allowed_ids = {node["id"] for node in nodes}
    edges: list[dict[str, Any]] = []
    for node in nodes:
        filtered_dependencies = [
            dep for dep in node.get("depends_on", [])
            if dep in allowed_ids and dep != node["id"]
        ]
        node["depends_on"] = filtered_dependencies
        for dep in filtered_dependencies:
            edges.append({
                "from": dep,
                "to": node["id"],
                "type": "data_dependency",
                "predicate": "status == success",
            })

    if _has_cycle(nodes, edges):
        return None

    return {
        "nodes": nodes,
        "edges": [*edges, *_build_retry_edges(nodes)],
    }


def build_fallback_step_dag(
    goal: dict[str, Any],
    *,
    capability_candidates: list[CapabilityNode] | None = None,
    planner_context: dict[str, Any] | None = None,
) -> Dag:
    """规则式 fallback：始终产出 step，而不是直接排序 capability。"""
    objectives = _split_goal_text_into_step_objectives(str(goal.get("text", "")))
    if not objectives:
        objectives = [str(goal.get("text", "")).strip() or "完成用户目标"]

    nodes: list[TaskStep] = []
    available_step_kinds = build_step_kind_index(
        capability_candidates=capability_candidates,
        planner_context=planner_context,
    )
    for index, objective in enumerate(objectives, start=1):
        depends_on = [f"s{index - 1}"] if index > 1 else []
        inferred_kind = _infer_step_kind(objective)
        kind = inferred_kind if inferred_kind in available_step_kinds else "unsupported"
        node = {
            "id": f"s{index}",
            "objective": objective,
            "kind": kind,
            "input_hints": _merge_skill_default_hints(
                _infer_step_input_hints(objective, goal, kind=kind),
                kind,
                planner_context,
            ),
            "depends_on": depends_on,
        }
        if kind == "unsupported":
            node["unsupported_reason"] = (
                f"没有注册工具支持步骤类型 {inferred_kind}。"
            )
            node["requested_kind"] = inferred_kind
        nodes.append(node)

    edges = [
        {
            "from": dep,
            "to": node["id"],
            "type": "data_dependency",
            "predicate": "status == success",
        }
        for node in nodes
        for dep in node.get("depends_on", [])
    ]
    return {
        "nodes": nodes,
        "edges": [*edges, *_build_retry_edges(nodes)],
    }


def bind_step_capabilities(
    goal: dict[str, Any],
    step_dag: Dag,
    capability_candidates: list[CapabilityNode],
) -> tuple[Dag, dict[str, Any]]:
    """给每个 step 绑定 capability，生成 step-capability DAG。"""
    supported_index = _build_supported_capability_index()
    available = set(supported_index)
    candidate_signal = {
        str(node.get("capability"))
        for node in capability_candidates
        if node.get("capability")
    }

    nodes: list[CapabilityNode] = []
    unbound_steps: list[str] = []
    for step in step_dag.get("nodes", []):
        capability = _select_capability_for_step(
            step,
            goal,
            available,
            candidate_capabilities=candidate_signal,
        )
        node = {
            **step,
            "capability": capability,
            "description": step.get("objective") or _describe_capability(capability),
            "score": _score_capability(capability, goal, 0.5),
            "source_goal_text": goal.get("text", ""),
            "selection_reason": "step_capability_binding",
            "capability_candidate_signal": capability in candidate_signal,
            "candidate_tools": [
                spec.name for spec in supported_index.get(capability, [])
            ],
        }
        if capability not in supported_index:
            unbound_steps.append(step["id"])
        nodes.append(node)

    edges = [
        dict(edge)
        for edge in step_dag.get("edges", [])
    ]
    return {
        "nodes": nodes,
        "edges": edges,
    }, {
        "source": "step_capability_binding",
        "reason": "bound capabilities per concrete task step",
        "unbound_steps": unbound_steps,
    }


def _build_retry_edges(nodes: list[CapabilityNode]) -> list[dict[str, Any]]:
    """为每个 capability node 构建 retry 自环边。

    作用：
        给 DAG 增加失败重试控制流。

    输入：
        nodes: capability node 列表。

    输出：
        retry edge 列表。

    核心计算逻辑：
        每个节点生成一条 ``node -> node`` 的 retry 边，并设置 ``max_retry=2``。

    设计意图：
        把重试策略和主依赖边分开，LLM 只负责正常依赖推理，系统统一补齐失败控制流。
    """
    return [
        {
            "from": node["id"],
            "to": node["id"],
            "type": "retry",
            "predicate": "status == failed",
            "max_retry": 2,
        }
        for node in nodes
    ]


def _has_cycle(nodes: list[CapabilityNode], edges: list[dict[str, Any]]) -> bool:
    """检测 Capability DAG 是否存在环。

    作用：
        校验 LLM 输出的 dependency edges 是否仍然是 DAG。

    输入：
        nodes: capability node 列表。
        edges: 不包含 retry 自环的依赖边列表。

    输出：
        有环返回 True，无环返回 False。

    核心计算逻辑：
        使用 DFS 三色标记检测有向图环。

    设计意图：
        LLM 可能输出 A->B、B->A 这种不可调度结构；这里在进入 scheduler 前拦截。
    """
    graph: dict[str, list[str]] = {node["id"]: [] for node in nodes}
    for edge in edges:
        graph.setdefault(edge["from"], []).append(edge["to"])

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False

        visiting.add(node_id)
        for next_id in graph.get(node_id, []):
            if visit(next_id):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node["id"]) for node in nodes)


def _normalize_step_dependencies(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item)
        for item in value
        if isinstance(item, (str, int)) and str(item).strip()
    ]


def build_step_kind_index(
    *,
    capability_candidates: list[CapabilityNode] | None = None,
    planner_context: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """基于当前 tool registry 构造 LLM 可用 kind 先验。

    LLM 只能看到当前至少有一个工具支持的 kind；没有工具支持的语义能力不进入
    decomposition 输出空间。传入 capability_candidates 时，优先返回与任务级
    capability pool 相交的 kind；skill 推荐的 kind 会作为额外先验保留。
    """
    supported_index = _build_supported_capability_index()
    candidate_capabilities = _capability_candidate_set(capability_candidates or [])
    skill_step_kinds = set(_extract_skill_step_kind_hints(planner_context))
    matched: dict[str, dict[str, Any]] = {}
    fallback: dict[str, dict[str, Any]] = {}
    for kind, definition in STEP_KIND_DEFINITIONS.items():
        capabilities = list(definition.get("capabilities", []))
        candidate_tools = _candidate_tools_for_capabilities(capabilities, supported_index)
        if not candidate_tools:
            continue
        capability_pool_match = bool(candidate_capabilities.intersection(capabilities))
        skill_recommended = kind in skill_step_kinds
        entry = {
            "label": definition.get("label", kind),
            "capabilities": capabilities,
            "input_hint_fields": list(definition.get("input_hint_fields", [])),
            "candidate_tools": candidate_tools,
            "tool_supported": True,
            "capability_pool_match": capability_pool_match,
            "skill_recommended": skill_recommended,
        }
        if capability_pool_match or skill_recommended or not candidate_capabilities:
            matched[kind] = entry
        else:
            fallback[kind] = entry
    return matched or fallback


def _capability_candidate_set(capability_candidates: list[CapabilityNode]) -> set[str]:
    """Extract capability IDs from task-level capability candidates."""
    return {
        str(node.get("capability"))
        for node in capability_candidates
        if node.get("capability")
    }


def _candidate_tools_for_capabilities(
    capabilities: list[str],
    supported_index: dict[str, list[ToolSpec]],
) -> list[str]:
    names: list[str] = []
    for capability in capabilities:
        names.extend(spec.name for spec in supported_index.get(capability, []))
    return _dedupe(names)


def _normalize_step_kind(
    raw_kind: Any,
    *,
    objective: str = "",
    available_step_kinds: dict[str, dict[str, Any]] | None = None,
) -> str:
    kind = str(raw_kind or "").strip()
    aliases = {
        "read_file": "file_read",
        "file.read": "file_read",
        "text_read": "file_read",
        "read_directory": "directory_list",
        "list_directory": "directory_list",
        "dir_list": "directory_list",
        "write_file": "file_write",
        "append_file": "file_append",
        "replace_file": "file_replace",
        "delete_file": "file_delete",
        "db_read": "database_read",
        "database_query": "database_read",
        "sql_query": "database_read",
        "db_write": "database_write",
        "run_shell": "shell_command",
        "command_run": "shell_command",
        "code.index": "codebase_scan",
        "code_index": "codebase_scan",
        "codebase.index": "codebase_scan",
        "codebase_scan": "codebase_scan",
        "scan_code": "codebase_scan",
        "scan_code_tree": "codebase_scan",
        "code.scan_tree": "codebase_scan",
        "extract_symbols": "code_symbol_extract",
        "symbol_extract": "code_symbol_extract",
        "code.extract_symbols": "code_symbol_extract",
        "code_symbol_extract": "code_symbol_extract",
        "summarize_code": "code_file_summarize",
        "code_file_summary": "code_file_summarize",
        "code.file.summarize": "code_file_summarize",
        "summarize_architecture": "code_architecture_summarize",
        "architecture_summary": "code_architecture_summarize",
        "code.architecture.summarize": "code_architecture_summarize",
        "code_search": "code_search",
        "code.search": "code_search",
        "code.retrieve": "code_search",
        "parse_log": "log_timeline_parse",
        "log_parse": "log_timeline_parse",
        "log.timeline": "log_timeline_parse",
        "log.parse_timeline": "log_timeline_parse",
        "diagnose": "incident_diagnose",
        "incident": "incident_diagnose",
        "incident.diagnose": "incident_diagnose",
        "root_cause": "incident_diagnose",
        "root_cause_analyze": "incident_diagnose",
        "generate_image": "image_generate",
        "text_to_image": "image_generate",
        "tts": "audio_tts",
        "text_to_speech": "audio_tts",
        "analysis": "analyze",
        "summary": "summarize",
        "qa": "answer",
    }
    kind = aliases.get(kind, kind)
    available = available_step_kinds or build_step_kind_index()
    if kind in available:
        return kind
    inferred = _infer_step_kind(objective)
    if inferred in available:
        return inferred
    return "unsupported"


def _filter_step_input_hints(kind: str, input_hints: dict[str, Any]) -> dict[str, Any]:
    allowed = set((STEP_KIND_DEFINITIONS.get(kind) or {}).get("input_hint_fields", []))
    if not allowed:
        return {}
    return {
        key: value
        for key, value in input_hints.items()
        if key in allowed and value is not None
    }


def _split_goal_text_into_step_objectives(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    parts = re.split(
        r"(?:然后|再|接着|并且|同时|以及|，然后|，并|, then| then |, and| and )",
        text,
    )
    objectives = [part.strip(" ，,。.;；") for part in parts if part.strip(" ，,。.;；")]
    return objectives if len(objectives) > 1 else [text]


def _infer_step_kind(objective: str) -> str:
    lowered = objective.lower()
    if any(word in objective for word in ["问题", "故障", "根因", "原因", "修复方案"]) or any(
        word in lowered for word in ["incident", "diagnose", "root cause", "bug"]
    ):
        return "incident_diagnose"
    if any(word in objective for word in ["日志", "时间线"]) or any(
        word in lowered for word in ["log", "timeline"]
    ):
        return "log_timeline_parse"
    if any(word in objective for word in ["代码架构", "代码框架", "工程架构", "架构总结"]) or (
        "architecture" in lowered and "code" in lowered
    ):
        return "code_architecture_summarize"
    if any(word in objective for word in ["函数总结", "文件总结", "代码总结"]):
        return "code_file_summarize"
    if any(word in objective for word in ["提取函数", "提取符号", "函数列表", "符号表"]) or any(
        word in lowered for word in ["symbol", "function list"]
    ):
        return "code_symbol_extract"
    if any(word in objective for word in ["扫描代码", "工程目录", "代码目录", "读代码"]) or any(
        word in lowered for word in ["codebase", "code index"]
    ):
        return "codebase_scan"
    if any(word in objective for word in ["查代码", "搜索代码", "检索代码"]) or (
        "代码" in objective and any(word in objective for word in ["查", "搜索", "检索"])
    ) or (
        "code" in lowered and any(word in lowered for word in ["search", "retrieve"])
    ):
        return "code_search"
    if any(word in objective for word in ["读取", "打开", "查看文件"]) or re.search(
        r"[\w./-]+\.(?:txt|md|json|csv|py|db)",
        objective,
    ):
        return "file_read"
    if any(word in objective for word in ["列出", "目录"]):
        return "directory_list"
    if any(word in lowered for word in ["search", "查询", "搜索", "查找"]):
        return "search"
    if any(word in objective for word in ["提取", "抽取"]) or "extract" in lowered:
        return "extract"
    if any(word in objective for word in ["总结", "摘要"]) or "summarize" in lowered:
        return "summarize"
    if any(word in objective for word in ["分析", "对比", "比较"]) or "analyze" in lowered:
        return "analyze"
    if any(word in objective for word in ["计划", "规划"]) or "plan" in lowered:
        return "plan_create"
    if any(word in objective for word in ["写入", "保存"]):
        return "file_write"
    if any(word in objective for word in ["替换", "修改"]):
        return "file_replace"
    if any(word in objective for word in ["删除"]):
        return "file_delete"
    if any(word in objective for word in ["图片", "图像", "生成图"]):
        return "image_generate"
    if any(word in objective for word in ["语音", "音频", "tts"]):
        return "audio_tts"
    if any(word in objective for word in ["数据库", "sqlite"]) or "sql" in lowered:
        return "database_read"
    return "answer"


def _infer_step_input_hints(
    objective: str,
    goal: dict[str, Any],
    *,
    kind: str | None = None,
) -> dict[str, Any]:
    kind = kind or _infer_step_kind(objective)
    hints: dict[str, Any] = {}
    path = _infer_path({"text": objective, "entities": goal.get("entities", []), "constraints": goal.get("constraints", {})})
    if path:
        hints["path"] = path
    table = _infer_table_name({"text": objective, "entities": goal.get("entities", []), "constraints": goal.get("constraints", {})})
    if table:
        hints["table"] = table
    limit = _infer_limit(objective)
    if limit:
        hints["limit"] = limit
    if kind == "search":
        hints.setdefault("query", objective)
    elif kind in {
        "codebase_scan",
        "code_symbol_extract",
        "code_file_summarize",
        "code_architecture_summarize",
    }:
        project_path = _infer_project_path({
            "text": objective,
            "entities": goal.get("entities", []),
            "constraints": goal.get("constraints", {}),
        })
        if project_path:
            hints["project_path"] = project_path
    elif kind == "code_search":
        project_path = _infer_project_path({
            "text": objective,
            "entities": goal.get("entities", []),
            "constraints": goal.get("constraints", {}),
        })
        if project_path:
            hints["project_path"] = project_path
        hints.setdefault("query", _infer_search_query(objective))
    elif kind == "log_timeline_parse":
        log_path = _infer_log_path({
            "text": objective,
            "entities": goal.get("entities", []),
            "constraints": goal.get("constraints", {}),
        })
        if log_path:
            hints["log_path"] = log_path
        event_time = _infer_event_time(objective)
        if event_time:
            hints["event_time"] = event_time
    elif kind == "incident_diagnose":
        project_path = _infer_project_path({
            "text": objective,
            "entities": goal.get("entities", []),
            "constraints": goal.get("constraints", {}),
        })
        if project_path:
            hints["project_path"] = project_path
        log_path = _infer_log_path({
            "text": objective,
            "entities": goal.get("entities", []),
            "constraints": goal.get("constraints", {}),
        })
        if log_path:
            hints["log_path"] = log_path
        event_time = _infer_event_time(objective)
        if event_time:
            hints["event_time"] = event_time
        hints.setdefault("problem_description", objective)
    elif kind in {"analyze", "summarize", "audio_tts", "answer", "extract", "plan_create"}:
        hints.setdefault("text", objective)
    elif kind == "image_generate":
        hints.setdefault("prompt", objective)
    return _filter_step_input_hints(kind, hints)


def _merge_skill_default_hints(
    hints: dict[str, Any],
    kind: str,
    planner_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """把 skill.default_resources 中与当前 kind schema 匹配的默认值并入 hints。"""
    bundle = (planner_context or {}).get("skill_context_bundle") or {}
    if not isinstance(bundle, dict):
        return hints
    defaults = bundle.get("default_resources") or {}
    if not isinstance(defaults, dict):
        return hints
    allowed = set((STEP_KIND_DEFINITIONS.get(kind) or {}).get("input_hint_fields", []))
    if not allowed:
        return hints
    merged = dict(hints)
    for key, value in defaults.items():
        if key in allowed and key not in merged and value is not None:
            merged[key] = value
    return _filter_step_input_hints(kind, merged)


def _select_capability_for_step(
    step: TaskStep,
    goal: dict[str, Any],
    available_capabilities: set[str],
    *,
    candidate_capabilities: set[str] | None = None,
) -> str:
    kind = str(step.get("kind") or "")
    objective = str(step.get("objective") or "")
    if kind == "unsupported":
        return "unsupported"
    capability_preferences = _capability_preferences_for_step(kind, objective, goal)
    candidate_capabilities = candidate_capabilities or set()
    for capability in capability_preferences:
        if capability in available_capabilities and capability in candidate_capabilities:
            return capability
    for capability in capability_preferences:
        if capability in available_capabilities:
            return capability
    if capability_preferences:
        return capability_preferences[0]
    if "text.answer" in available_capabilities:
        return "text.answer"
    return next(iter(sorted(available_capabilities)), "text.answer")


def _capability_preferences_for_step(
    kind: str,
    objective: str,
    goal: dict[str, Any],
) -> list[str]:
    lowered = objective.lower()
    preferences: list[str] = []
    preferences.extend((STEP_KIND_DEFINITIONS.get(kind) or {}).get("capabilities", []))
    if re.search(r"[\w./-]+\.(?:txt|md|json|csv|py|db)", objective):
        preferences.append("file.read")
    if any(word in objective for word in ["总结", "摘要"]) or "summarize" in lowered:
        preferences.append("text.summarize")
    if any(word in objective for word in ["分析", "对比", "比较"]) or "analyze" in lowered:
        preferences.append("text.analyze")
    if any(word in lowered for word in ["search", "查询", "搜索", "查找"]):
        preferences.append("search.query")
    if any(word in objective for word in ["代码", "工程", "架构", "函数", "符号"]) or "code" in lowered:
        preferences.extend([
            "code.scan_tree",
            "code.extract_symbols",
            "code.summarize_files",
            "code.architecture.summarize",
        ])
    if any(word in objective for word in ["日志"]) or "log" in lowered:
        preferences.append("log.parse_timeline")
    if any(word in objective for word in ["问题", "故障", "根因", "原因"]) or any(
        word in lowered for word in ["incident", "diagnose", "root cause"]
    ):
        preferences.append("incident.diagnose")
    for intent in goal.get("intents", []):
        preferences.extend(INTENT_CAPABILITY_MAP.get(intent, []))
    return _dedupe(preferences)


def _goal_with_step_context(goal: dict[str, Any], step_node: dict[str, Any]) -> dict[str, Any]:
    """为单个 step 构造参数绑定视图。"""
    step_goal = dict(goal)
    objective = str(step_node.get("objective") or step_node.get("description") or "")
    input_hints = step_node.get("input_hints") or {}
    constraints = {
        **(goal.get("constraints") or {}),
        **({} if not isinstance(input_hints, dict) else input_hints),
    }
    step_goal["text"] = " ".join(
        part for part in [objective, str(goal.get("text", ""))] if part
    )
    step_goal["constraints"] = constraints
    step_goal["current_step"] = {
        "id": step_node.get("id"),
        "objective": objective,
        "kind": step_node.get("kind"),
        "input_hints": input_hints,
        "depends_on": step_node.get("depends_on", []),
    }
    return step_goal


def bind_tools(
    step_capability_dag: Dag,
    *,
    goal: dict[str, Any] | None = None,
    planner_context: dict[str, Any] | None = None,
    state: State | None = None,
) -> dict[str, dict[str, Any]]:
    """根据 step capability 为每个 step 绑定具体工具。

    作用：
        把抽象能力节点转换为“能力 -> 工具”的绑定关系。

    输入：
        step_capability_dag: Step-capability DAG。函数读取其中每个节点的
            ``capability`` 字段。
        goal: 可选 Goal IR，用于 ContextManager 构造 tool binding context。
        planner_context: planner 阶段上下文。
        state: 可选 LangGraph state，用于读取 session_id/context_manager。

    输出：
        以 step id 为 key 的绑定表。绑定成功时包含工具名、
        tool/core 名称、类别、input_schema、output_schema 和 capabilities；
        绑定失败时包含 ``status="unbound"`` 和失败原因。

    核心计算逻辑：
        对每个 capability 调用 ``_find_best_tool_for_capability``，从
        ``ToolSpec.capabilities`` 中找精确匹配的 enabled tool。

    设计意图：
        解耦“需要什么能力”和“用哪个工具实现”。这样 planner 可以先在能力层
        做推理，再由 registry 决定实际工具，避免 goalparser 直接幻觉工具名。
    """
    bindings: dict[str, dict[str, Any]] = {}

    for node in step_capability_dag["nodes"]:
        capability = node["capability"]
        candidates = _find_tool_candidates_for_capability(capability)
        tool_binding_context = build_tool_binding_stage_context(
            state=state,
            goal=goal or {},
            capability=capability,
            candidates=candidates,
            planner_context=planner_context,
        )
        spec = _select_tool_for_capability_with_llm(
            capability,
            candidates,
            tool_binding_context=tool_binding_context,
        )
        if spec is None:
            bindings[node["id"]] = {
                "status": "unbound",
                "capability": capability,
                "reason": "没有注册具备该 capability 的工具。",
                "tool_binding_context": tool_binding_context,
            }
            continue

        bindings[node["id"]] = {
            "status": "bound",
            "capability": capability,
            "tool": spec.name,
            "description": spec.description,
            "tool_name": spec.tool_name,
            "core_name": spec.core_name,
            "category": spec.category.value,
            "input_schema": spec.input_schema,
            "output_schema": spec.output_schema,
            "capabilities": spec.capabilities,
            "candidate_tools": [candidate.name for candidate in candidates],
            "tool_binding_context": tool_binding_context,
        }

    return bindings


def bind_arguments(
    goal: dict[str, Any],
    step_capability_dag: Dag,
    tool_bindings: dict[str, dict[str, Any]],
    *,
    planner_context: dict[str, Any] | None = None,
    state: State | None = None,
) -> dict[str, dict[str, Any]]:
    """根据 goal、step 和工具 input_schema 为每个 step 绑定调用参数。

    作用：
        把“已经选好的工具”转换成“可以直接调用的工具入参”。这是
        Tool Binding 和 Executable DAG 之间的独立阶段。

    输入：
        goal: Goal IR，主要读取 ``text``、``entities``、``constraints``。
        step_capability_dag: Step-capability DAG，用于遍历 step 节点。
        tool_bindings: ``bind_tools`` 产出的工具绑定表。
        planner_context: planner 阶段上下文。
        state: 可选 LangGraph state，用于读取 session_id/context_manager。

    输出：
        以 step id 为 key 的参数绑定表。绑定成功时包含 ``input``；
        如果缺少必填字段，会返回 ``status="partial"`` 和 ``missing_required``；
        如果工具本身未绑定，会返回 ``status="skipped"``。

    核心计算逻辑：
        先让 LLM 根据 goal、tool 和 input_schema 生成候选参数；如果 LLM 不可用
        或输出不完整，再用本地规则补缺。随后对结果做 schema 校验。仍缺必填
        字段或类型错误时，生成 clarification_questions，交给上层 agent 暂停询问。

    设计意图：
        让参数推断留在 planner 内部，而不是散落到 worker 或 tool_dispatcher。
        LLM 负责理解自然语言，本地 schema 校验负责把关，澄清问题负责处理
        不能安全执行的情况。
    """
    argument_bindings: dict[str, dict[str, Any]] = {}

    for node in step_capability_dag["nodes"]:
        node_id = node["id"]
        binding = tool_bindings[node_id]

        if binding["status"] != "bound":
            argument_bindings[node_id] = {
                "status": "skipped",
                "input": {},
                "missing_required": [],
                "reason": "工具未绑定，跳过参数绑定。",
            }
            continue

        input_schema = binding.get("input_schema") or {}
        binding_goal = _goal_with_step_context(goal, node)
        argument_binding_context = build_argument_binding_stage_context(
            state=state,
            goal=binding_goal,
            capability=node["capability"],
            binding=binding,
            planner_context=planner_context,
        )
        llm_input = _llm_bind_tool_input(
            goal=binding_goal,
            capability=node["capability"],
            binding=binding,
            input_schema=input_schema,
            argument_binding_context=argument_binding_context,
        )
        rule_input = _infer_tool_input(
            goal=binding_goal,
            capability=node["capability"],
            tool_name=binding["tool"],
            input_schema=input_schema,
        )

        if llm_input is None:
            raw_input = rule_input
            source = "rule"
        else:
            raw_input = {**rule_input, **llm_input}
            source = "llm+rule"

        input_payload, missing_required, type_errors = _validate_tool_input(
            raw_input,
            input_schema,
        )
        clarification_questions = _build_clarification_questions(
            missing_required,
            type_errors,
            binding["tool"],
        )
        status = "bound"
        if missing_required or type_errors:
            status = "needs_clarification"

        argument_bindings[node_id] = {
            "status": status,
            "input": input_payload,
            "missing_required": missing_required,
            "type_errors": type_errors,
            "clarification_questions": clarification_questions,
            "source": source,
            "argument_binding_context": argument_binding_context,
        }

    return argument_bindings


def build_executable_dag(
    step_capability_dag: Dag,
    tool_bindings: dict[str, dict[str, Any]],
    argument_bindings: dict[str, dict[str, Any]],
    context: dict[str, Any] | None = None,
) -> Dag:
    """生成 scheduler/worker/tool_dispatcher 可消费的 Executable DAG。

    作用：
        把 Step-capability DAG 和 Tool Binding 合并为最终执行图。

    输入：
        step_capability_dag: Step-capability DAG。
        tool_bindings: ``bind_tools`` 生成的绑定表。
        argument_bindings: ``bind_arguments`` 生成的参数绑定表。
        context: 可选多轮会话上下文，作为 DAG 级 metadata 透传给 scheduler/worker。

    输出：
        Executable DAG，节点 ID 从 ``s1`` 转为 ``n1``，节点中包含 ``tool``、
        ``input``、``input_schema``、``output_schema``、``binding_status`` 等字段。

    核心计算逻辑：
        对每个 step-capability node 查找工具绑定和参数绑定结果；绑定成功则写入
        真实工具名和 ``input``，绑定失败则写入 ``tool=None`` 和错误原因。
        边会从 step id 映射到 executable node id，并保留边的控制流元数据。

    设计意图：
        让 worker 只关心可执行节点；step/capability 元数据只用于追踪和解释。
    """
    executable_nodes: list[dict[str, Any]] = []

    for step_capability_node in step_capability_dag["nodes"]:
        binding = tool_bindings[step_capability_node["id"]]
        argument_binding = argument_bindings[step_capability_node["id"]]
        tool_name = binding.get("tool") if binding.get("status") == "bound" else None
        tool_input = argument_binding.get("input", {})
        sandbox_config = build_sandbox_config_for_node(
            capability=step_capability_node,
            tool_name=tool_name,
            tool_input=tool_input,
            context=context,
        )
        executable_node = {
            "id": _to_executable_node_id(step_capability_node["id"]),
            "step_id": step_capability_node.get("id"),
            "capability": step_capability_node["capability"],
            "description": step_capability_node["description"],
            "objective": step_capability_node.get("objective", step_capability_node["description"]),
            "step_kind": step_capability_node.get("kind"),
            "input_hints": step_capability_node.get("input_hints", {}),
            "binding_status": binding["status"],
            "argument_status": argument_binding["status"],
            "input": tool_input,
            "sandbox": sandbox_config,
            "limits": dict(sandbox_config["resource_limits"]),
            "missing_required": argument_binding.get("missing_required", []),
            "input_schema": binding.get("input_schema", {}),
            "output_schema": binding.get("output_schema", {}),
        }

        if binding["status"] == "bound":
            executable_node["tool"] = tool_name
        else:
            executable_node["tool"] = None
            executable_node["error"] = binding["reason"]

        if argument_binding.get("status") == "needs_clarification":
            executable_node["error"] = _format_argument_error(argument_binding)
            executable_node["clarification_questions"] = argument_binding.get(
                "clarification_questions",
                [],
            )

        executable_nodes.append(executable_node)

    id_map = {
        node["id"]: _to_executable_node_id(node["id"])
        for node in step_capability_dag["nodes"]
    }
    executable_edges = [
        {
            "from": id_map[edge["from"]],
            "to": id_map[edge["to"]],
            **{
                key: value
                for key, value in edge.items()
                if key not in {"from", "to"}
            },
        }
        for edge in step_capability_dag["edges"]
    ]

    executable_dag: Dag = {"nodes": executable_nodes, "edges": executable_edges}
    if context:
        executable_dag["context"] = _build_executable_context(context)
    return executable_dag


def add_human_interrupt_nodes(dag: Dag, goal: dict[str, Any]) -> Dag:
    """Insert explicit human interrupt gates when planning needs user input."""
    result = {
        "nodes": list(dag.get("nodes", [])),
        "edges": list(dag.get("edges", [])),
    }
    if dag.get("context"):
        result["context"] = dag["context"]

    for node in list(result.get("nodes", [])):
        if node.get("node_type") == "human_interrupt":
            continue
        if node.get("argument_status") != "needs_clarification":
            continue

        interrupt_id = f"{node['id']}_human_input"
        result = _insert_human_interrupt_before_node(
            result,
            target_node_id=node["id"],
            interrupt_node={
                "id": interrupt_id,
                "node_type": "human_interrupt",
                "capability": "human.input",
                "description": "等待用户补充工具调用所需参数。",
                "tool": None,
                "binding_status": "not_required",
                "argument_status": "pending_human_input",
                "input": {},
                "interrupt": {
                    "interrupt_type": "clarification",
                    "reason": "tool_arguments_need_clarification",
                    "prompt": _build_argument_interrupt_prompt(node),
                    "required_input": {
                        "type": "form",
                        "fields": [
                            {"name": name, "type": "text", "required": True}
                            for name in node.get("missing_required", [])
                        ],
                    },
                    "context_payload": {
                        "target_node_id": node["id"],
                        "capability": node.get("capability"),
                        "tool": node.get("tool"),
                        "missing_required": node.get("missing_required", []),
                        "clarification_questions": node.get("clarification_questions", []),
                    },
                },
            },
        )

    for node in list(result.get("nodes", [])):
        if node.get("node_type") == "human_interrupt":
            continue
        if node.get("binding_status") == "bound":
            continue

        interrupt_id = f"{node['id']}_capability_unbound"
        result = _insert_human_interrupt_before_node(
            result,
            target_node_id=node["id"],
            interrupt_node={
                "id": interrupt_id,
                "node_type": "human_interrupt",
                "capability": "human.feedback",
                "description": "反馈当前步骤缺少可用工具。",
                "tool": None,
                "binding_status": "not_required",
                "argument_status": "pending_human_input",
                "input": {},
                "interrupt": {
                    "interrupt_type": "unsupported_capability",
                    "reason": "step_capability_has_no_bound_tool",
                    "prompt": _build_step_unbound_interrupt_prompt(node),
                    "required_input": _build_unsupported_capability_required_input(),
                    "context_payload": {
                        "target_node_id": node["id"],
                        "step_id": node.get("step_id"),
                        "objective": node.get("objective"),
                        "capability": node.get("capability"),
                        "error": node.get("error"),
                    },
                },
            },
        )

    for node in list(result.get("nodes", [])):
        if node.get("node_type") == "human_interrupt":
            continue

        permission = ((node.get("sandbox") or {}).get("permission") or {})
        if not permission.get("require_approval"):
            continue

        interrupt_id = f"{node['id']}_approval"
        result = _insert_human_interrupt_before_node(
            result,
            target_node_id=node["id"],
            interrupt_node={
                "id": interrupt_id,
                "node_type": "human_interrupt",
                "capability": "human.approval",
                "description": "等待用户审批工具调用。",
                "tool": None,
                "binding_status": "not_required",
                "argument_status": "pending_human_approval",
                "input": {},
                "interrupt": {
                    "interrupt_type": "approval",
                    "reason": permission.get("approval_reason") or "tool_requires_approval",
                    "prompt": _build_approval_interrupt_prompt(node, permission),
                    "required_input": {
                        "type": "approval",
                        "options": ["approve", "reject"],
                    },
                    "context_payload": {
                        "target_node_id": node["id"],
                        "capability": node.get("capability"),
                        "tool": node.get("tool"),
                        "input": node.get("input", {}),
                        "permission": permission,
                    },
                },
            },
        )

    return result


def build_unmatched_capability_dag(
    goal: dict[str, Any],
    match_report: dict[str, Any],
) -> Dag:
    """Build a feedback DAG when capability decomposition cannot continue."""
    reason = match_report.get("reason")
    suggested_action = match_report.get("suggested_action")
    if suggested_action == "ask_clarification" or reason == "no_capability_proposals":
        prompt = _build_capability_clarification_prompt(goal, match_report)
        required_input = {
            "type": "text",
            "placeholder": "请说明你希望 agent 执行的具体操作。",
        }
        interrupt_type = "clarification"
        capability = "human.input"
        description = "等待用户澄清目标能力。"
    else:
        prompt = _build_unsupported_capability_prompt(goal, match_report)
        required_input = _build_unsupported_capability_required_input()
        interrupt_type = "unsupported_capability"
        capability = "human.feedback"
        description = "反馈当前缺少可用能力或工具。"

    node = {
        "id": "n_capability_unmatched",
        "node_type": "human_interrupt",
        "capability": capability,
        "description": description,
        "tool": None,
        "binding_status": "not_required",
        "argument_status": "pending_human_input",
        "input": {},
        "interrupt": {
            "interrupt_type": interrupt_type,
            "reason": reason,
            "prompt": prompt,
            "required_input": required_input,
            "context_payload": {
                "goal": goal,
                "capability_match_report": match_report,
            },
        },
    }
    return {
        "nodes": [node],
        "edges": [],
        "metadata": {
            "plan_type": "capability_unmatched",
            "capability_match_report": match_report,
        },
    }


def build_goal_clarification_dag(goal: dict[str, Any]) -> Dag:
    """Build an interrupt-only DAG when the parsed goal is not actionable yet."""
    node = {
        "id": "n_human_goal",
        "node_type": "human_interrupt",
        "capability": "human.input",
        "description": "等待用户补充或确认目标。",
        "tool": None,
        "binding_status": "not_required",
        "argument_status": "pending_human_input",
        "input": {},
        "interrupt": {
            "interrupt_type": "clarification",
            "reason": "goal_needs_clarification",
            "prompt": _build_goal_interrupt_prompt(goal),
            "required_input": {
                "type": "text",
                "placeholder": "请补充目标、范围或确认继续执行。",
            },
            "context_payload": {
                "goal": goal,
                "ambiguity": goal.get("ambiguity", {}),
            },
        },
    }
    return {
        "nodes": [node],
        "edges": [],
        "metadata": {
            "plan_type": "goal_clarification",
            "reason": "goal_needs_clarification",
        },
    }


def _build_capability_clarification_prompt(
    goal: dict[str, Any],
    match_report: dict[str, Any],
) -> str:
    questions = (goal.get("ambiguity") or {}).get("clarification_questions") or []
    if questions:
        return " ".join(str(question) for question in questions)
    text = str(goal.get("text", "")).strip()
    if text:
        return (
            "我还不能确定这个目标需要哪类能力。"
            f"当前输入是：{text}。"
            "请补充你想让我执行的具体操作。"
        )
    return "我还不能确定你想让我执行哪类操作，请补充具体目标。"


def _build_unsupported_capability_prompt(
    goal: dict[str, Any],
    match_report: dict[str, Any],
) -> str:
    proposed = ", ".join(match_report.get("proposed_capabilities") or [])
    unsupported = ", ".join(match_report.get("unsupported_proposals") or [])
    available = ", ".join(match_report.get("available_capabilities") or [])
    text = str(goal.get("text", "")).strip()
    parts = ["当前目标已经识别出能力需求，但没有可用工具可以执行。"]
    if text:
        parts.append(f"目标：{text}。")
    if proposed:
        parts.append(f"识别到的能力：{proposed}。")
    if unsupported:
        parts.append(f"缺少支持的能力或工具：{unsupported}。")
    if available:
        parts.append(f"当前可用能力：{available}。")
    return "".join(parts)


def _build_unsupported_capability_required_input() -> dict[str, Any]:
    return {
        "type": "form",
        "fields": [
            {
                "name": "action",
                "type": "select",
                "options": ["replan", "skip_step", "cancel"],
                "required": True,
            },
            {
                "name": "instruction",
                "type": "text",
                "required": False,
            },
        ],
    }


def _build_approval_interrupt_prompt(node: dict[str, Any], permission: dict[str, Any]) -> str:
    reason = permission.get("approval_reason") or "该工具调用需要审批。"
    tool = node.get("tool") or "unknown_tool"
    description = node.get("description") or ""
    return f"工具 {tool} 需要审批后执行。原因：{reason} {description}".strip()


def _prepend_human_interrupt(
    dag: Dag,
    *,
    node_id: str,
    prompt: str,
    reason: str,
    required_input: dict[str, Any],
    context_payload: dict[str, Any],
) -> Dag:
    """Create a human gate before every current entry node."""
    existing_ids = {node["id"] for node in dag.get("nodes", [])}
    if node_id in existing_ids:
        return dag

    incoming_targets = {
        edge["to"]
        for edge in dag.get("edges", [])
        if edge.get("from") != edge.get("to")
    }
    entry_node_ids = [
        node["id"]
        for node in dag.get("nodes", [])
        if node["id"] not in incoming_targets
    ]

    interrupt_node = {
        "id": node_id,
        "node_type": "human_interrupt",
        "capability": "human.input",
        "description": "等待用户补充或确认目标。",
        "tool": None,
        "binding_status": "not_required",
        "argument_status": "pending_human_input",
        "input": {},
        "interrupt": {
            "interrupt_type": "clarification",
            "reason": reason,
            "prompt": prompt,
            "required_input": required_input,
            "context_payload": context_payload,
        },
    }
    dag["nodes"] = [interrupt_node, *dag.get("nodes", [])]
    dag["edges"] = [
        {
            "from": node_id,
            "to": target,
            "type": "success",
            "predicate": "status == success",
        }
        for target in entry_node_ids
    ] + dag.get("edges", [])
    return dag


def _insert_human_interrupt_before_node(
    dag: Dag,
    *,
    target_node_id: str,
    interrupt_node: dict[str, Any],
) -> Dag:
    """Insert an interrupt node immediately before a target executable node."""
    interrupt_id = interrupt_node["id"]
    existing_ids = {node["id"] for node in dag.get("nodes", [])}
    if interrupt_id in existing_ids:
        return dag

    new_edges: list[dict[str, Any]] = []
    for edge in dag.get("edges", []):
        if edge.get("to") == target_node_id and edge.get("from") != target_node_id:
            redirected = dict(edge)
            redirected["to"] = interrupt_id
            new_edges.append(redirected)
        else:
            new_edges.append(edge)

    new_edges.append({
        "from": interrupt_id,
        "to": target_node_id,
        "type": "success",
        "predicate": "status == success",
    })
    dag["nodes"].append(interrupt_node)
    dag["edges"] = new_edges
    return dag


def _build_goal_interrupt_prompt(goal: dict[str, Any]) -> str:
    questions = goal.get("ambiguity", {}).get("clarification_questions") or []
    if questions:
        return " ".join(str(question) for question in questions)
    text = str(goal.get("text", "")).strip()
    if text:
        return f"当前目标需要补充信息后才能继续执行：{text}"
    return "当前目标不够明确，请补充你希望 agent 执行的具体内容。"


def _build_argument_interrupt_prompt(node: dict[str, Any]) -> str:
    questions = node.get("clarification_questions") or []
    if questions:
        return " ".join(str(question) for question in questions)
    missing = ", ".join(str(name) for name in node.get("missing_required", []))
    if missing:
        return f"工具节点 {node.get('id')} 缺少必要参数：{missing}。请补充。"
    return f"工具节点 {node.get('id')} 需要用户补充信息后才能继续。"


def _build_step_unbound_interrupt_prompt(node: dict[str, Any]) -> str:
    objective = node.get("objective") or node.get("description") or node.get("id")
    capability = node.get("capability") or "unknown"
    return (
        "当前步骤缺少可用工具，无法继续自动执行。"
        f"步骤：{objective}。"
        f"需要能力：{capability}。"
        "请补充可用工具，或调整目标。"
    )


def _build_executable_context(context: dict[str, Any]) -> dict[str, Any]:
    """裁剪写入 DAG 的多轮上下文。

    DAG 会被存入 Redis，也会随 ready node 被 worker 读取；因此这里只保留
    session 元信息、最近消息和最近任务摘要。
    """
    return {
        "session_id": context.get("session_id"),
        "turn_index": context.get("turn_index"),
        "summary": context.get("summary", ""),
        "recent_messages": list(context.get("messages", []))[-8:],
        "recent_tasks": list(context.get("tasks", []))[-3:],
        "metadata": context.get("metadata", {}),
    }


def _to_executable_node_id(node_id: str) -> str:
    """把 step/capability node id 映射为 scheduler 可执行 node id。"""
    text = str(node_id)
    if re.fullmatch(r"[cs]\d+", text):
        return "n" + text[1:]
    if text.startswith("n"):
        return text
    return f"n_{text}"


def _find_tool_candidates_for_capability(capability: str) -> list[ToolSpec]:
    """按 capability 从工具注册表中检索候选工具。

    作用：
        查询当前有哪些工具可以实现某个 capability。

    输入：
        capability: 能力字符串，例如 ``database.read`` 或 ``image.generate``。

    输出：
        匹配到的 ``ToolSpec`` 列表；没有匹配则返回空列表。

    核心计算逻辑：
        遍历 enabled tool specs，检查 capability 是否出现在
        ``spec.capabilities`` 中。

    设计意图：
        Tool Binding 不让 LLM 编造工具，只允许它在 registry 召回的候选中选择。
    """
    return [
        spec
        for spec in iter_tool_specs(enabled_only=True)
        if capability in spec.capabilities
    ]


def build_tool_binding_stage_context(
    *,
    state: State | None,
    goal: dict[str, Any],
    capability: str,
    candidates: list[ToolSpec],
    planner_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 tool binding 阶段上下文。"""
    session_id = _extract_session_id(state, planner_context)
    context_manager = _extract_context_manager(state)

    if session_id and context_manager is not None:
        try:
            return context_manager.build_tool_binding_context(
                session_id,
                goal=goal,
                capability=capability,
                candidate_tools=candidates,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "stage": "tool_binding",
                "context_error": str(exc),
                "goal": goal,
                "capability": capability,
                "candidate_tools": [
                    {
                        "name": candidate.name,
                        "description": candidate.description,
                        "category": candidate.category.value,
                        "input_schema": candidate.input_schema,
                        "output_schema": candidate.output_schema,
                        "capabilities": candidate.capabilities,
                    }
                    for candidate in candidates
                ],
                "planner_context": planner_context or {},
            }

    return {
        "stage": "tool_binding",
        "goal": goal,
        "capability": capability,
        "candidate_tools": [
            {
                "name": candidate.name,
                "description": candidate.description,
                "category": candidate.category.value,
                "input_schema": candidate.input_schema,
                "output_schema": candidate.output_schema,
                "capabilities": candidate.capabilities,
            }
            for candidate in candidates
        ],
        "planner_context": planner_context or {},
    }


def build_argument_binding_stage_context(
    *,
    state: State | None,
    goal: dict[str, Any],
    capability: str,
    binding: dict[str, Any],
    planner_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 argument binding 阶段上下文。"""
    session_id = _extract_session_id(state, planner_context)
    context_manager = _extract_context_manager(state)
    tool_payload = {
        "name": binding.get("tool"),
        "description": binding.get("description", ""),
        "category": binding.get("category"),
        "input_schema": binding.get("input_schema", {}),
        "output_schema": binding.get("output_schema", {}),
        "capabilities": binding.get("capabilities", []),
    }

    if session_id and context_manager is not None:
        try:
            return context_manager.build_argument_binding_context(
                session_id,
                goal=goal,
                capability=capability,
                tool=tool_payload,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "stage": "argument_binding",
                "context_error": str(exc),
                "goal": goal,
                "capability": capability,
                "tool": tool_payload,
                "planner_context": planner_context or {},
            }

    return {
        "stage": "argument_binding",
        "goal": goal,
        "capability": capability,
        "tool": tool_payload,
        "goal_constraints": goal.get("constraints", {}),
        "planner_context": planner_context or {},
    }


def _extract_session_id(
    state: State | None,
    planner_context: dict[str, Any] | None = None,
) -> str | None:
    """从 state/planner_context 中提取 session_id。"""
    if state:
        if state.get("session_id"):
            return str(state["session_id"])
        raw_context = state.get("context") or {}
        if raw_context.get("session_id"):
            return str(raw_context["session_id"])

    session = (planner_context or {}).get("session") or {}
    if session.get("session_id"):
        return str(session["session_id"])
    if (planner_context or {}).get("session_id"):
        return str((planner_context or {})["session_id"])
    return None


def _extract_context_manager(state: State | None) -> ContextManager | None:
    """从 state 中读取 ContextManager；没有则尝试创建默认实例。"""
    if state and state.get("context_manager") is not None:
        return state["context_manager"]

    try:
        return ContextManager()
    except Exception:
        return None


def _select_tool_for_capability_with_llm(
    capability: str,
    candidates: list[ToolSpec],
    tool_binding_context: dict[str, Any] | None = None,
) -> ToolSpec | None:
    """从候选工具中选择最终工具。

    作用：
        对 registry 召回的候选工具做最终选择。候选只有一个时确定性返回；
        多个候选时可用 LLM rerank。

    输入：
        capability: 当前能力。
        candidates: registry 召回的候选工具列表。
        tool_binding_context: tool binding 阶段上下文。

    输出：
        选中的 ``ToolSpec``，没有候选时返回 ``None``。

    核心计算逻辑：
        0 个候选返回 None；1 个候选直接返回；多个候选调用
        ``_llm_rerank_tool_candidates``，失败时返回第一个候选。

    设计意图：
        registry 保证工具存在和能力匹配，LLM 只负责在多个可行工具中做语义排序。
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    selected_name = _llm_rerank_tool_candidates(
        capability,
        candidates,
        tool_binding_context=tool_binding_context,
    )
    if selected_name:
        for candidate in candidates:
            if candidate.name == selected_name:
                return candidate

    return candidates[0]


def _llm_rerank_tool_candidates(
    capability: str,
    candidates: list[ToolSpec],
    tool_binding_context: dict[str, Any] | None = None,
) -> str | None:
    """使用 LLM 在多个候选工具中 rerank。

    作用：
        当同一 capability 有多个工具可用时，让 LLM 选择更合适的工具。

    输入：
        capability: 当前能力。
        candidates: 候选 ToolSpec 列表。
        tool_binding_context: ContextManager 为 tool binding 阶段构造的上下文。

    输出：
        选中的工具 name；失败或输出非法时返回 ``None``。

    核心计算逻辑：
        将候选工具的 name、description、category、input_schema、output_schema
        传给 LLM，要求返回 ``{"tool": "..."}``。

    设计意图：
        给未来多工具竞争预留 rerank 点，同时保持“只能选已有工具”的安全边界。
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        return None

    client = OpenAI(
        api_key=api_key,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
    payload = {
        "capability": capability,
        "candidates": [
            {
                "name": candidate.name,
                "description": candidate.description,
                "category": candidate.category.value,
                "input_schema": candidate.input_schema,
                "output_schema": candidate.output_schema,
                "capabilities": candidate.capabilities,
            }
            for candidate in candidates
        ],
    }
    if tool_binding_context:
        payload["tool_binding_context"] = tool_binding_context

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a tool reranker. Return valid json only. "
                        "Choose exactly one tool name from candidates. "
                        "Return shape: {\"tool\":\"tool_name\"}."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            response_format={"type": "json_object"},
        )
    except Exception:
        return None

    try:
        parsed = json.loads(response.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        return None

    selected = parsed.get("tool")
    candidate_names = {candidate.name for candidate in candidates}
    if selected in candidate_names:
        return str(selected)
    return None


def _infer_tool_input(
    *,
    goal: dict[str, Any],
    capability: str,
    tool_name: str,
    input_schema: dict[str, Any],
) -> dict[str, Any]:
    """根据 goal 和 input_schema 推断一个工具节点的入参。

    作用：
        为 ``bind_arguments`` 提供具体参数推断逻辑。

    输入：
        goal: Goal IR。
        capability: 当前 capability。
        tool_name: 已绑定工具名。
        input_schema: 工具输入 JSON schema。

    输出：
        可直接放入 executable node ``input`` 的字典。

    核心计算逻辑：
        先复制 ``goal["constraints"]`` 中和 schema properties 匹配的字段，
        再按工具/能力类型做启发式填充。

    设计意图：
        把参数推断规则集中在一个小函数里，方便后续替换为 LLM 参数绑定或
        更严格的 schema validator。
    """
    properties = (input_schema.get("properties") or {}).keys()
    constraints = goal.get("constraints") or {}
    inferred = {
        key: value
        for key, value in constraints.items()
        if key in properties
    }

    text = str(goal.get("text", ""))

    if tool_name == "select_rows" or capability == "database.read":
        inferred.setdefault("table", _infer_table_name(goal))
        inferred.setdefault("limit", _infer_limit(text) or 100)
        if "filters" in properties:
            inferred.setdefault("filters", constraints.get("filters", {}))

    elif capability in {"file.read", "directory.list", "file.delete"}:
        inferred.setdefault("path", _infer_path(goal))

    elif capability in {"file.write", "file.replace"}:
        inferred.setdefault("path", _infer_path(goal))
        if "content" in properties:
            inferred.setdefault("content", constraints.get("content", text))
        if "old" in properties:
            inferred.setdefault("old", constraints.get("old", ""))
        if "new" in properties:
            inferred.setdefault("new", constraints.get("new", ""))

    elif capability == "image.generate":
        inferred.setdefault("prompt", constraints.get("prompt", text))

    elif capability == "audio.text_to_speech":
        inferred.setdefault("text", constraints.get("text", text))

    elif capability in {
        "code.scan_tree",
        "code.extract_symbols",
        "code.summarize_files",
        "code.architecture.summarize",
    }:
        inferred.setdefault("project_path", _infer_project_path(goal))

    elif capability == "code.search":
        inferred.setdefault("project_path", _infer_project_path(goal))
        inferred.setdefault("query", constraints.get("query") or _infer_search_query(text))

    elif capability == "log.parse_timeline":
        inferred.setdefault("log_path", _infer_log_path(goal))
        inferred.setdefault("event_time", constraints.get("event_time") or _infer_event_time(text))

    elif capability == "incident.diagnose":
        inferred.setdefault("problem_description", constraints.get("problem_description") or text)
        inferred.setdefault("project_path", _infer_project_path(goal))
        inferred.setdefault("log_path", _infer_log_path(goal))
        inferred.setdefault("event_time", constraints.get("event_time") or _infer_event_time(text))

    return {
        key: value
        for key, value in inferred.items()
        if value is not None
    }


def _llm_bind_tool_input(
    *,
    goal: dict[str, Any],
    capability: str,
    binding: dict[str, Any],
    input_schema: dict[str, Any],
    argument_binding_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """使用 LLM 根据 tool input_schema 生成工具参数。

    作用：
        让模型从自然语言 goal 中抽取结构化工具入参。

    输入：
        goal: Goal IR。
        capability: 当前能力。
        binding: 已选中的工具绑定信息。
        input_schema: 工具输入 JSON schema。
        argument_binding_context: ContextManager 为 argument binding 阶段构造的上下文。

    输出：
        成功时返回参数 dict；LLM 不可用、请求失败或输出非法时返回 ``None``。

    核心计算逻辑：
        使用 OpenAI-compatible client 调 DeepSeek，要求返回
        ``{"input": {...}}`` JSON object。函数只负责拿到候选参数，不做信任；
        后续必须经过 ``_validate_tool_input``。

    设计意图：
        把自然语言理解能力集中给 LLM，但把安全性留给本地校验和澄清机制。
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        return None

    client = OpenAI(
        api_key=api_key,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

    payload = {
        "goal": goal,
        "capability": capability,
        "tool": binding.get("tool"),
        "input_schema": input_schema,
    }
    if argument_binding_context:
        payload["argument_binding_context"] = argument_binding_context

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an argument binding module for an agent planner. "
                        "Return valid json only. "
                        "Bind tool arguments from the user goal according to the input_schema. "
                        "Do not invent values that are not implied by the goal. "
                        "Return shape: {\"input\": {}}."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
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

    candidate = parsed.get("input")
    if not isinstance(candidate, dict):
        return None
    return candidate


def _validate_tool_input(
    input_payload: dict[str, Any],
    input_schema: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[dict[str, str]]]:
    """按工具 input_schema 校验和规范化参数。

    作用：
        对 LLM/规则生成的参数做本地把关。

    输入：
        input_payload: 候选参数。
        input_schema: 工具输入 JSON schema。

    输出：
        ``(normalized_input, missing_required, type_errors)``。

    核心计算逻辑：
        丢弃 schema properties 之外的字段；检查 required 字段；按 JSON schema
        的基础 type 检查 string、integer、number、boolean、object、array。

    设计意图：
        不直接信任 LLM 输出，保证 worker 看到的是已知字段和基本类型正确的参数。
    """
    properties = input_schema.get("properties") or {}
    required = input_schema.get("required") or []

    if properties:
        normalized = {
            key: value
            for key, value in input_payload.items()
            if key in properties
        }
    else:
        normalized = dict(input_payload)

    missing_required = [
        field
        for field in required
        if field not in normalized or normalized[field] in (None, "")
    ]
    type_errors: list[dict[str, str]] = []

    for field, value in list(normalized.items()):
        expected_type = (properties.get(field) or {}).get("type")
        if expected_type and not _matches_json_type(value, expected_type):
            type_errors.append({
                "field": field,
                "expected": str(expected_type),
                "actual": type(value).__name__,
            })

    return normalized, missing_required, type_errors


def _matches_json_type(value: Any, expected_type: str) -> bool:
    """判断 Python 值是否匹配基础 JSON schema type。

    作用：
        支持 ``_validate_tool_input`` 的类型检查。

    输入：
        value: 待检查值。
        expected_type: JSON schema type。

    输出：
        匹配返回 True，否则 False。

    核心计算逻辑：
        将 JSON schema 类型映射到 Python 类型，注意 bool 不能被当作 integer。

    设计意图：
        避免引入额外 jsonschema 依赖，先覆盖工具参数校验的基础需求。
    """
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    return True


def _build_clarification_questions(
    missing_required: list[str],
    type_errors: list[dict[str, str]],
    tool_name: str,
) -> list[str]:
    """根据参数校验错误生成用户澄清问题。

    作用：
        当参数绑定不完整时，告诉上层 agent 应该问用户什么。

    输入：
        missing_required: 缺失的必填字段。
        type_errors: 类型错误列表。
        tool_name: 当前工具名。

    输出：
        中文澄清问题列表。

    核心计算逻辑：
        缺字段时询问具体字段；类型错误时说明期望类型和实际类型。

    设计意图：
        把“不能安全执行”的情况显式暴露给 scheduler/agent loop，而不是让
        worker 带着坏参数执行失败。
    """
    questions: list[str] = []
    for field in missing_required:
        questions.append(f"工具 {tool_name} 缺少必填参数 {field}，请补充。")

    for error in type_errors:
        questions.append(
            "工具 "
            f"{tool_name} 的参数 {error['field']} 类型不正确，"
            f"需要 {error['expected']}，当前是 {error['actual']}。"
        )

    return questions


def _format_argument_error(argument_binding: dict[str, Any]) -> str:
    """把参数绑定错误格式化为 executable node error。

    作用：
        为最终 DAG 节点提供简短错误摘要。

    输入：
        argument_binding: ``bind_arguments`` 产出的单节点参数绑定结果。

    输出：
        错误摘要字符串。

    核心计算逻辑：
        合并 missing_required 和 type_errors 的关键信息。

    设计意图：
        让 DAG 节点在日志和 UI 中一眼能看出为什么不能执行。
    """
    parts: list[str] = []
    missing_required = argument_binding.get("missing_required") or []
    type_errors = argument_binding.get("type_errors") or []

    if missing_required:
        parts.append("缺少必填参数: " + ", ".join(missing_required))
    if type_errors:
        fields = [str(error.get("field")) for error in type_errors]
        parts.append("参数类型错误: " + ", ".join(fields))

    return "；".join(parts) if parts else "参数绑定需要澄清。"


def _infer_table_name(goal: dict[str, Any]) -> str | None:
    """从 constraints、entities 和原文中推断 SQLite table 名称。

    作用：
        为数据库查询类工具补齐 ``table`` 参数。

    输入：
        goal: Goal IR。

    输出：
        推断出的 table 名称；无法推断则返回 ``None``。

    核心计算逻辑：
        优先读取 ``constraints.table``；然后从 entities 中找合法标识符；
        最后匹配 “xxx 表” 或 “table xxx”。

    设计意图：
        先覆盖 demo/常见中文输入场景，让 planner 产出的 DAG 能被 worker
        直接执行；复杂 SQL 解析后续再扩展。
    """
    constraints = goal.get("constraints") or {}
    if constraints.get("table"):
        return str(constraints["table"])

    for entity in goal.get("entities", []):
        candidate = str(entity).strip()
        if _is_identifier(candidate):
            return candidate

    text = str(goal.get("text", ""))
    patterns = [
        r"([A-Za-z_][A-Za-z0-9_]*)\s*表",
        r"table\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"from\s+([A-Za-z_][A-Za-z0-9_]*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)

    return None


def _infer_limit(text: str) -> int | None:
    """从用户文本中推断查询 limit。

    作用：
        为查询类工具补齐 ``limit`` 参数。

    输入：
        text: 用户原始文本。

    输出：
        推断出的正整数；无法推断则返回 ``None``。

    核心计算逻辑：
        匹配 “前 10 条”、“limit 10” 等常见表达。

    设计意图：
        用非常轻量的规则覆盖常见查询需求，避免 planner 直接生成空 input。
    """
    patterns = [
        r"前\s*(\d+)\s*条",
        r"limit\s+(\d+)",
        r"top\s+(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return max(1, int(match.group(1)))
    return None


def _infer_path(goal: dict[str, Any]) -> str | None:
    """从 constraints、entities 和文本中推断文件或目录路径。

    作用：
        为文件系统类工具补齐 ``path`` 参数。

    输入：
        goal: Goal IR。

    输出：
        推断出的路径字符串；无法推断则返回 ``None``。

    核心计算逻辑：
        优先读取 ``constraints.path``；再从 entities 或 text 中找带 ``/``、
        ``.txt``、``.md``、``.json`` 等特征的 token。

    设计意图：
        保持参数绑定规则简单透明，同时为常见文件操作提供可执行 input。
    """
    constraints = goal.get("constraints") or {}
    if constraints.get("path"):
        return str(constraints["path"])

    candidates = [str(entity) for entity in goal.get("entities", [])]
    candidates.extend(re.findall(r"[\w./-]+\.(?:txt|md|json|csv|py|db)", str(goal.get("text", ""))))

    for candidate in candidates:
        if "/" in candidate or "." in candidate:
            return candidate

    return None


def _infer_project_path(goal: dict[str, Any]) -> str | None:
    """从 constraints、entities 和文本中推断工程目录路径。"""
    constraints = goal.get("constraints") or {}
    for key in ("project_path", "codebase_path", "repo_path", "directory", "path"):
        if constraints.get(key):
            return str(constraints[key])

    text = str(goal.get("text", ""))
    candidates = [str(entity) for entity in goal.get("entities", [])]
    candidates.extend(
        re.findall(
            r"(?:工程|目录|代码|项目|repo|project|codebase)\s*[:：]?\s*([A-Za-z0-9_./-]+)",
            text,
            flags=re.IGNORECASE,
        )
    )
    candidates.extend(re.findall(r"[A-Za-z0-9_.-]*[/][A-Za-z0-9_./-]+", text))
    candidates.extend(re.findall(r"\b[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*\b", text))

    for candidate in candidates:
        candidate = candidate.strip(" ，,。.;；'\"`")
        if not candidate:
            continue
        if os.path.isdir(candidate):
            return candidate
        if "/" in candidate or "_" in candidate or candidate.startswith("."):
            return candidate
    return None


def _infer_log_path(goal: dict[str, Any]) -> str | None:
    """从 constraints、entities 和文本中推断日志文件路径。"""
    constraints = goal.get("constraints") or {}
    for key in ("log_path", "log_file", "path"):
        value = constraints.get(key)
        if value and str(value).lower().endswith((".log", ".txt")):
            return str(value)

    text = str(goal.get("text", ""))
    candidates = [str(entity) for entity in goal.get("entities", [])]
    candidates.extend(re.findall(r"[\w./-]+\.(?:log|txt)", text, flags=re.IGNORECASE))
    for candidate in candidates:
        candidate = candidate.strip(" ，,。.;；'\"`")
        if candidate.lower().endswith((".log", ".txt")):
            return candidate
    return None


def _infer_event_time(text: str) -> str | None:
    patterns = [
        r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?",
        r"\d{2}:\d{2}:\d{2}(?:[.,]\d+)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return None


def _infer_search_query(text: str) -> str:
    cleaned = re.sub(r"[\w./-]+\.(?:log|txt|md|json|csv|py|db)", " ", text)
    cleaned = re.sub(r"\b[A-Za-z0-9_.-]*[/][A-Za-z0-9_./-]+\b", " ", cleaned)
    return " ".join(cleaned.split()) or text


def _is_identifier(value: str) -> bool:
    """判断字符串是否是安全的数据库标识符。

    作用：
        过滤明显不适合作为 SQLite table 名称的 entity。

    输入：
        value: 待判断字符串。

    输出：
        合法标识符返回 True，否则 False。

    核心计算逻辑：
        使用正则限制首字符为字母或下划线，后续为字母、数字或下划线。

    设计意图：
        避免把整句中文 entity 或包含危险字符的文本直接当作表名。
    """
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value))


def _score_capability(
    capability: str,
    goal: dict[str, Any],
    default_score: float,
) -> float:
    """计算 capability 与 goal 的相关性分数。

    作用：
        给能力节点打分，方便后续排序、剪枝或解释 planner 为什么选择某能力。

    输入：
        capability: 当前能力字符串。
        goal: Goal IR，主要读取 ``intent_scores``。
        default_score: 找不到对应 intent score 时使用的默认分数。

    输出：
        0 到 1 之间的 float，保留两位小数。

    核心计算逻辑：
        根据 capability 反查最相关 intent，例如 ``database.read`` 对应
        ``tool_call``，``text.summarize`` 对应 ``summarization``。若没有明确映射，
        使用 default_score。

    设计意图：
        把 goalparser 的意图置信度传递到 capability 层，避免能力节点失去
        原始解析的不确定性信息。
    """
    if capability in {"database.read", "code.execute"}:
        return _max_score_for_intents(goal, ["tool_call"], default_score)
    if capability == "search.query":
        return _max_score_for_intents(goal, ["search"], default_score)
    if capability == "text.summarize":
        return _max_score_for_intents(goal, ["summarization"], default_score)
    if capability == "text.analyze":
        return _max_score_for_intents(goal, ["analysis"], default_score)
    if capability == "plan.create":
        return _max_score_for_intents(goal, ["planning"], default_score)
    if capability in {
        "code.scan_tree",
        "code.extract_symbols",
        "code.summarize_files",
        "code.architecture.summarize",
    }:
        return _max_score_for_intents(
            goal,
            ["codebase_analysis", "analysis", "summarization"],
            default_score,
        )
    if capability == "code.search":
        return _max_score_for_intents(goal, ["code_search", "search"], default_score)
    if capability == "log.parse_timeline":
        return _max_score_for_intents(goal, ["log_analysis", "incident_diagnosis"], default_score)
    if capability == "incident.diagnose":
        return _max_score_for_intents(goal, ["incident_diagnosis", "analysis"], default_score)
    return round(float(default_score), 2)


def _max_score_for_intents(
    goal: dict[str, Any],
    intents: list[str],
    default_score: float,
) -> float:
    """从多个 intent 中取最高置信度。

    作用：
        为一个 capability 聚合相关 intent 的最高分。

    输入：
        goal: Goal IR，读取 ``intent_scores``。
        intents: 与当前 capability 相关的 intent 名称列表。
        default_score: 没有命中任何 intent 时的兜底分数。

    输出：
        最高 intent score，或 default_score，保留两位小数。

    核心计算逻辑：
        遍历 intents，在 ``goal["intent_scores"]`` 中取分数并求 max。

    设计意图：
        一个 capability 可能来自多个 intent 信号，这里集中处理分数聚合，
        避免在 ``_score_capability`` 中重复写取分逻辑。
    """
    intent_scores = goal.get("intent_scores") or {}
    scores = [
        float(intent_scores[intent])
        for intent in intents
        if intent in intent_scores
    ]
    return round(max(scores, default=default_score), 2)


def _describe_capability(capability: str) -> str:
    """返回 capability 的人类可读描述。

    作用：
        为 capability node 增加解释性文本。

    输入：
        capability: 能力字符串。

    输出：
        中文描述；未知 capability 原样返回。

    核心计算逻辑：
        从本地 descriptions 字典中查找描述。

    设计意图：
        让调试日志、DAG 可视化和 planner 解释更容易读。该描述不参与执行，
        只服务于可观测性。
    """
    descriptions = {
        "search.query": "查询或搜索外部信息。",
        "text.extract": "从文本或文件中抽取结构化信息。",
        "text.summarize": "总结已有内容。",
        "text.analyze": "分析已有内容并产出判断。",
        "text.answer": "直接回答用户问题。",
        "plan.create": "生成计划或步骤。",
        "code.execute": "执行代码。",
        "database.read": "读取数据库记录。",
        "file.read": "读取文件内容。",
        "file.write": "写入文件内容。",
        "file.replace": "替换文件内容。",
        "file.delete": "删除文件。",
        "directory.list": "列出目录内容。",
        "image.generate": "根据文本生成图片。",
        "audio.text_to_speech": "将文本合成为语音。",
        "code.scan_tree": "扫描代码工程目录并生成文件树。",
        "code.extract_symbols": "提取代码中的类、函数、方法和依赖符号。",
        "code.summarize_files": "生成代码文件职责和关键函数摘要。",
        "code.architecture.summarize": "总结代码工程模块架构。",
        "code.search": "在代码和配置中检索关键字、符号和日志线索。",
        "log.parse_timeline": "解析日志时间线和异常事件。",
        "incident.diagnose": "结合问题描述、日志和代码证据诊断根因。",
    }
    return descriptions.get(capability, capability)


def _dedupe(values: list[str]) -> list[str]:
    """对字符串列表去重并保留原始顺序。

    作用：
        去掉多路映射产生的重复 capability。

    输入：
        values: 字符串列表。

    输出：
        去重后的字符串列表。

    核心计算逻辑：
        顺序扫描，只有第一次出现的值会进入结果。

    设计意图：
        保留信号发现顺序，同时避免同一个 capability 生成多个重复节点。
    """
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _order_capabilities(capabilities: list[str]) -> list[str]:
    """按粗粒度数据流排序 capability。

    作用：
        把无序 capability 集合整理成更合理的默认执行顺序。

    输入：
        capabilities: 已去重的 capability 列表。

    输出：
        按 priority 排序后的 capability 列表。

    核心计算逻辑：
        使用本地 priority 表排序：读取/查询类能力优先，抽取/分析/总结居中，
        写文件、多媒体生成和代码执行靠后。未知 capability 放最后。

    设计意图：
        在还没有完整数据依赖推断前，用简单优先级模拟常见数据流：
        先拿数据，再加工，再输出。
    """
    priority = {
        "database.read": 10,
        "file.read": 10,
        "directory.list": 10,
        "code.scan_tree": 12,
        "code.extract_symbols": 14,
        "code.summarize_files": 16,
        "code.architecture.summarize": 18,
        "search.query": 20,
        "code.search": 22,
        "log.parse_timeline": 24,
        "text.extract": 30,
        "text.analyze": 40,
        "text.summarize": 50,
        "incident.diagnose": 55,
        "text.answer": 60,
        "plan.create": 60,
        "file.write": 70,
        "file.replace": 70,
        "file.delete": 70,
        "image.generate": 80,
        "audio.text_to_speech": 80,
        "code.execute": 90,
    }
    return sorted(
        capabilities,
        key=lambda capability: priority.get(capability, 100),
    )


if StateGraph is not None:
    graph = StateGraph(State)
    graph.add_node("planner", build_plan)
    graph.set_entry_point("planner")
    planner_app = graph.compile()
else:
    graph = None
    planner_app = None

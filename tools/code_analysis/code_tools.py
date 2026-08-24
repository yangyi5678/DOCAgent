from __future__ import annotations

import ast
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from langchain.tools import tool
except ImportError:  # pragma: no cover
    tool = None


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".idea",
    ".review",
    ".vscode",
    "__pycache__",
    "build",
    "cmake-build-debug",
    "cmake-build-release",
    "dist",
    "node_modules",
    "outputs",
}
DEFAULT_CODE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".py",
    ".mfl",
    ".yaml",
    ".yml",
    ".json",
    ".md",
    ".cmake",
    ".txt",
}
SYMBOL_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".py"}
TEXT_EXTENSIONS = DEFAULT_CODE_EXTENSIONS | {".sh", ".bash", ".csv", ".properties", ".lst"}


def _to_tool_result(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)


def _resolve_allowed_roots(allowed_roots: list[str | Path] | None = None) -> list[Path]:
    roots = allowed_roots or [ROOT_DIR]
    return [Path(root).expanduser().resolve() for root in roots]


def _ensure_path_allowed(
    path: str | Path,
    *,
    allowed_roots: list[str | Path] | None = None,
) -> Path:
    target = Path(path).expanduser().resolve()
    for root in _resolve_allowed_roots(allowed_roots):
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue
    allowed_text = ", ".join(str(root) for root in _resolve_allowed_roots(allowed_roots))
    raise PermissionError(f"路径不在允许范围内: {target}。允许目录: {allowed_text}")


def _normalize_extensions(extensions: list[str] | None) -> set[str]:
    values = extensions or sorted(DEFAULT_CODE_EXTENSIONS)
    return {ext if ext.startswith(".") else f".{ext}" for ext in values}


def _iter_project_files(
    project_path: str | Path,
    *,
    include_ext: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
    max_files: int = 2000,
    allowed_roots: list[str | Path] | None = None,
) -> tuple[Path, list[Path], Counter[str], list[str]]:
    root = _ensure_path_allowed(project_path, allowed_roots=allowed_roots)
    if not root.exists():
        raise FileNotFoundError(f"找不到工程目录: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"目标不是工程目录: {root}")

    include = _normalize_extensions(include_ext)
    excluded = set(exclude_dirs or DEFAULT_EXCLUDE_DIRS)
    files: list[Path] = []
    skipped_dirs: list[str] = []
    ext_counter: Counter[str] = Counter()

    for current_text, dirs, names in os.walk(root):
        current = Path(current_text)
        dirs[:] = [d for d in dirs if d not in excluded and not d.startswith(".cache")]
        for name in sorted(names):
            path = current / name
            if not path.is_file():
                continue
            if path.name == ".DS_Store":
                continue
            ext = path.suffix
            ext_counter[ext or "<none>"] += 1
            if ext in include:
                files.append(path)
                if len(files) >= max_files:
                    return root, files, ext_counter, skipped_dirs

    return root, files, ext_counter, skipped_dirs


def _safe_read_text(path: Path, *, max_chars: int = 120_000) -> str:
    data = path.read_bytes()[: max_chars * 2]
    return data.decode("utf-8", errors="ignore")[:max_chars]


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def scan_code_tree_core(
    project_path: str,
    *,
    include_ext: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
    max_files: int = 2000,
    max_depth: int = 4,
    allowed_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    root, files, ext_counter, skipped_dirs = _iter_project_files(
        project_path,
        include_ext=include_ext,
        exclude_dirs=exclude_dirs,
        max_files=max_files,
        allowed_roots=allowed_roots,
    )
    dir_counter: Counter[str] = Counter()
    tree_entries: list[dict[str, Any]] = []
    for path in files:
        rel = Path(_relative(path, root))
        parts = rel.parts
        for depth in range(1, min(len(parts), max_depth) + 1):
            dir_counter["/".join(parts[:depth])] += 1
        tree_entries.append(
            {
                "path": str(rel),
                "extension": path.suffix or "<none>",
                "size_bytes": path.stat().st_size,
            }
        )

    top_dirs = [
        {"path": path, "file_count": count}
        for path, count in dir_counter.most_common(80)
        if "/" not in path or path.count("/") < max_depth
    ]
    return {
        "status": "success",
        "action": "scan_code_tree",
        "project_path": str(root),
        "file_count": len(files),
        "extension_counts": dict(ext_counter.most_common()),
        "top_dirs": top_dirs,
        "files": tree_entries[:max_files],
        "truncated": len(files) >= max_files,
        "skipped_dirs": sorted(set(skipped_dirs))[:100],
    }


def _extract_python_symbols(path: Path, root: Path) -> dict[str, Any]:
    text = _safe_read_text(path)
    symbols: list[dict[str, Any]] = []
    imports: list[str] = []
    try:
        module = ast.parse(text)
    except SyntaxError as exc:
        return {
            "path": _relative(path, root),
            "language": "python",
            "parse_error": str(exc),
            "imports": [],
            "symbols": [],
        }

    parent_stack: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_Import(self, node: ast.Import) -> None:
            imports.extend(alias.name for alias in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            module_name = node.module or ""
            imports.append(module_name)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            symbols.append(
                {
                    "name": node.name,
                    "qualified_name": ".".join([*parent_stack, node.name]),
                    "kind": "class",
                    "line": node.lineno,
                    "signature": f"class {node.name}",
                    "doc": ast.get_docstring(node) or "",
                }
            )
            parent_stack.append(node.name)
            self.generic_visit(node)
            parent_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            args = [arg.arg for arg in node.args.args]
            symbols.append(
                {
                    "name": node.name,
                    "qualified_name": ".".join([*parent_stack, node.name]),
                    "kind": "method" if parent_stack else "function",
                    "line": node.lineno,
                    "signature": f"{node.name}({', '.join(args)})",
                    "doc": ast.get_docstring(node) or "",
                }
            )
            parent_stack.append(node.name)
            self.generic_visit(node)
            parent_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

    Visitor().visit(module)
    return {
        "path": _relative(path, root),
        "language": "python",
        "imports": sorted(set(imports)),
        "symbols": symbols,
    }


CPP_SYMBOL_RE = re.compile(
    r"^\s*(?P<prefix>(?:[\w:<>~*&,\s]+\s+)?)"
    r"(?P<name>[A-Za-z_~][\w:~]*)\s*"
    r"\((?P<args>[^;{}()]*)\)\s*"
    r"(?P<suffix>const\s*)?(?:\{|;)\s*$"
)
CPP_CLASS_RE = re.compile(r"^\s*(?:class|struct)\s+(?P<name>[A-Za-z_]\w*)")
CPP_INCLUDE_RE = re.compile(r"^\s*#\s*include\s+[<\"](?P<include>[^>\"]+)[>\"]")
CPP_CONTROL_NAMES = {"if", "for", "while", "switch", "catch", "return"}


def _extract_cpp_symbols(path: Path, root: Path) -> dict[str, Any]:
    text = _safe_read_text(path)
    symbols: list[dict[str, Any]] = []
    includes: list[str] = []
    for index, line in enumerate(text.splitlines(), start=1):
        include_match = CPP_INCLUDE_RE.match(line)
        if include_match:
            includes.append(include_match.group("include"))
            continue
        class_match = CPP_CLASS_RE.match(line)
        if class_match:
            name = class_match.group("name")
            symbols.append(
                {
                    "name": name,
                    "qualified_name": name,
                    "kind": "class",
                    "line": index,
                    "signature": line.strip(),
                    "doc": "",
                }
            )
            continue
        symbol_match = CPP_SYMBOL_RE.match(line)
        if not symbol_match:
            continue
        name = symbol_match.group("name").split("::")[-1]
        if name in CPP_CONTROL_NAMES:
            continue
        signature = line.strip().rstrip("{").rstrip(";").strip()
        kind = "method" if "::" in symbol_match.group("name") else "function"
        symbols.append(
            {
                "name": name,
                "qualified_name": symbol_match.group("name"),
                "kind": kind,
                "line": index,
                "signature": signature,
                "doc": "",
            }
        )
    return {
        "path": _relative(path, root),
        "language": "cpp",
        "imports": sorted(set(includes)),
        "symbols": symbols,
    }


def extract_code_symbols_core(
    project_path: str,
    *,
    include_ext: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
    max_files: int = 1200,
    max_symbols_per_file: int = 200,
    allowed_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    include = include_ext or sorted(SYMBOL_EXTENSIONS)
    root, files, _, _ = _iter_project_files(
        project_path,
        include_ext=include,
        exclude_dirs=exclude_dirs,
        max_files=max_files,
        allowed_roots=allowed_roots,
    )
    file_results: list[dict[str, Any]] = []
    symbol_count = 0
    for path in files:
        if path.suffix == ".py":
            result = _extract_python_symbols(path, root)
        else:
            result = _extract_cpp_symbols(path, root)
        result["symbols"] = result.get("symbols", [])[:max_symbols_per_file]
        symbol_count += len(result["symbols"])
        file_results.append(result)

    return {
        "status": "success",
        "action": "extract_code_symbols",
        "project_path": str(root),
        "file_count": len(file_results),
        "symbol_count": symbol_count,
        "files": file_results,
        "truncated": len(files) >= max_files,
    }


def summarize_code_files_core(
    project_path: str,
    *,
    include_ext: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
    max_files: int = 300,
    max_chars_per_file: int = 60_000,
    allowed_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    root, files, _, _ = _iter_project_files(
        project_path,
        include_ext=include_ext,
        exclude_dirs=exclude_dirs,
        max_files=max_files,
        allowed_roots=allowed_roots,
    )
    summaries: list[dict[str, Any]] = []
    for path in files:
        text = _safe_read_text(path, max_chars=max_chars_per_file)
        lines = text.splitlines()
        non_empty = [line for line in lines if line.strip()]
        includes = []
        if path.suffix == ".py":
            symbol_info = _extract_python_symbols(path, root)
            includes = symbol_info.get("imports", [])
        elif path.suffix in SYMBOL_EXTENSIONS:
            symbol_info = _extract_cpp_symbols(path, root)
            includes = symbol_info.get("imports", [])
        else:
            symbol_info = {"symbols": []}
        symbols = symbol_info.get("symbols", [])
        summaries.append(
            {
                "path": _relative(path, root),
                "extension": path.suffix or "<none>",
                "line_count": len(lines),
                "non_empty_line_count": len(non_empty),
                "symbol_count": len(symbols),
                "imports": includes[:40],
                "key_symbols": symbols[:30],
                "content_preview": "\n".join(non_empty[:12])[:1600],
                "summary": _build_file_summary(path, symbols, includes, len(lines)),
            }
        )

    return {
        "status": "success",
        "action": "summarize_code_files",
        "project_path": str(root),
        "file_count": len(summaries),
        "files": summaries,
        "truncated": len(files) >= max_files,
    }


def _build_file_summary(path: Path, symbols: list[dict[str, Any]], imports: list[str], line_count: int) -> str:
    kinds = Counter(str(symbol.get("kind")) for symbol in symbols)
    parts = [f"{path.name} 包含 {line_count} 行"]
    if symbols:
        parts.append(
            "定义 "
            + ", ".join(f"{count} 个 {kind}" for kind, count in kinds.items())
            + "，核心符号包括 "
            + ", ".join(str(symbol.get("qualified_name") or symbol.get("name")) for symbol in symbols[:8])
        )
    if imports:
        parts.append("依赖 " + ", ".join(imports[:8]))
    return "；".join(parts) + "。"


def summarize_code_architecture_core(
    project_path: str,
    *,
    include_ext: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
    max_files: int = 1200,
    allowed_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    tree = scan_code_tree_core(
        project_path,
        include_ext=include_ext,
        exclude_dirs=exclude_dirs,
        max_files=max_files,
        allowed_roots=allowed_roots,
    )
    symbols = extract_code_symbols_core(
        project_path,
        include_ext=list(SYMBOL_EXTENSIONS),
        exclude_dirs=exclude_dirs,
        max_files=max_files,
        allowed_roots=allowed_roots,
    )
    modules: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"file_count": 0, "symbol_count": 0, "files": [], "key_symbols": []}
    )
    for file_info in tree.get("files", []):
        module = str(file_info["path"]).split("/", 1)[0]
        modules[module]["file_count"] += 1
        modules[module]["files"].append(file_info["path"])
    for file_info in symbols.get("files", []):
        module = str(file_info["path"]).split("/", 1)[0]
        file_symbols = file_info.get("symbols", [])
        modules[module]["symbol_count"] += len(file_symbols)
        modules[module]["key_symbols"].extend(file_symbols[:15])

    module_list = []
    for name, data in sorted(modules.items(), key=lambda item: item[1]["file_count"], reverse=True):
        module_list.append(
            {
                "name": name,
                "file_count": data["file_count"],
                "symbol_count": data["symbol_count"],
                "sample_files": data["files"][:20],
                "key_symbols": data["key_symbols"][:30],
                "responsibility_guess": _guess_module_responsibility(name, data["files"]),
            }
        )

    return {
        "status": "success",
        "action": "summarize_code_architecture",
        "project_path": tree["project_path"],
        "overview": {
            "file_count": tree["file_count"],
            "symbol_count": symbols["symbol_count"],
            "extension_counts": tree["extension_counts"],
        },
        "modules": module_list[:80],
        "architecture_summary": _build_architecture_summary(module_list),
        "recommended_next_steps": [
            "按用户关注的功能域选择模块做二次深读。",
            "结合日志关键字用 code_search 定位相关符号和配置。",
            "对高风险调用链补充 code.flow.trace 或人工标注入口函数。",
        ],
    }


def _guess_module_responsibility(name: str, files: list[str]) -> str:
    lowered = name.lower()
    if "core" in lowered:
        return "核心算法或核心功能实现。"
    if "adaptor" in lowered or "adapter" in lowered:
        return "外部接口适配、消息转换或平台集成。"
    if "generated" in lowered:
        return "自动生成代码或配置产物。"
    if "framework" in lowered:
        return "基础框架、公共运行时或通用能力。"
    if "test" in lowered:
        return "测试用例和测试支撑。"
    if any(file.endswith("CMakeLists.txt") for file in files):
        return "包含构建配置和模块组织信息。"
    return "需要结合文件摘要进一步确认职责。"


def _build_architecture_summary(modules: list[dict[str, Any]]) -> str:
    top = modules[:10]
    lines = ["该工程按顶层目录组织模块，主要模块包括："]
    for module in top:
        lines.append(
            f"- {module['name']}: {module['file_count']} 个文件，"
            f"{module['symbol_count']} 个符号，{module['responsibility_guess']}"
        )
    return "\n".join(lines)


def code_search_core(
    project_path: str,
    query: str,
    *,
    include_ext: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
    max_files: int = 2000,
    max_matches: int = 100,
    context_lines: int = 2,
    allowed_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    if not query.strip():
        raise ValueError("query 不能为空。")
    terms = [term.lower() for term in re.split(r"\s+", query.strip()) if term]
    root, files, _, _ = _iter_project_files(
        project_path,
        include_ext=include_ext or sorted(TEXT_EXTENSIONS),
        exclude_dirs=exclude_dirs,
        max_files=max_files,
        allowed_roots=allowed_roots,
    )
    matches: list[dict[str, Any]] = []
    for path in files:
        text = _safe_read_text(path)
        lines = text.splitlines()
        for index, line in enumerate(lines, start=1):
            lowered = line.lower()
            score = sum(1 for term in terms if term in lowered)
            if score == 0:
                continue
            start = max(1, index - context_lines)
            end = min(len(lines), index + context_lines)
            matches.append(
                {
                    "path": _relative(path, root),
                    "line": index,
                    "score": score,
                    "text": line.strip()[:500],
                    "context": "\n".join(lines[start - 1 : end])[:1800],
                }
            )
            if len(matches) >= max_matches:
                return {
                    "status": "success",
                    "action": "code_search",
                    "project_path": str(root),
                    "query": query,
                    "count": len(matches),
                    "matches": matches,
                    "truncated": True,
                }

    matches.sort(key=lambda item: item["score"], reverse=True)
    return {
        "status": "success",
        "action": "code_search",
        "project_path": str(root),
        "query": query,
        "count": len(matches),
        "matches": matches[:max_matches],
        "truncated": False,
    }


LOG_TIME_RE = re.compile(
    r"(?P<time>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?|\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"
)
LOG_LEVEL_RE = re.compile(r"\b(?P<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\b", re.I)


def parse_log_timeline_core(
    *,
    log_text: str | None = None,
    log_path: str | None = None,
    event_time: str | None = None,
    max_events: int = 300,
    allowed_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    if not log_text and not log_path:
        raise ValueError("必须提供 log_text 或 log_path。")
    if log_path:
        path = _ensure_path_allowed(log_path, allowed_roots=allowed_roots)
        log_text = _safe_read_text(path, max_chars=600_000)
    assert log_text is not None

    events: list[dict[str, Any]] = []
    level_counts: Counter[str] = Counter()
    for line_no, line in enumerate(log_text.splitlines(), start=1):
        time_match = LOG_TIME_RE.search(line)
        level_match = LOG_LEVEL_RE.search(line)
        level = level_match.group("level").upper() if level_match else "UNKNOWN"
        if level == "WARNING":
            level = "WARN"
        if not time_match and level == "UNKNOWN":
            continue
        level_counts[level] += 1
        events.append(
            {
                "line": line_no,
                "timestamp": time_match.group("time") if time_match else None,
                "level": level,
                "message": line.strip()[:1200],
                "near_event_time": bool(event_time and time_match and event_time in time_match.group("time")),
            }
        )
        if len(events) >= max_events:
            break

    anomalies = [
        event
        for event in events
        if event["level"] in {"WARN", "ERROR", "FATAL", "CRITICAL"}
        or any(word in event["message"].lower() for word in ["fail", "abort", "timeout", "exception", "error"])
    ]
    return {
        "status": "success",
        "action": "parse_log_timeline",
        "event_time": event_time,
        "event_count": len(events),
        "level_counts": dict(level_counts),
        "timeline": events,
        "anomalies": anomalies[:120],
        "truncated": len(events) >= max_events,
    }


def diagnose_incident_core(
    problem_description: str,
    *,
    event_time: str | None = None,
    project_path: str | None = None,
    log_text: str | None = None,
    log_path: str | None = None,
    architecture_summary: str | None = None,
    search_query: str | None = None,
    max_code_matches: int = 40,
    allowed_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    if not problem_description.strip():
        raise ValueError("problem_description 不能为空。")

    timeline = None
    if log_text or log_path:
        timeline = parse_log_timeline_core(
            log_text=log_text,
            log_path=log_path,
            event_time=event_time,
            allowed_roots=allowed_roots,
        )

    code_matches = None
    if project_path:
        query = search_query or _build_incident_search_query(problem_description, timeline)
        code_matches = code_search_core(
            project_path,
            query,
            max_matches=max_code_matches,
            allowed_roots=allowed_roots,
        )

    evidence: list[dict[str, Any]] = []
    if timeline:
        for event in timeline.get("anomalies", [])[:15]:
            evidence.append({"type": "log", **event})
    if code_matches:
        for match in code_matches.get("matches", [])[:15]:
            evidence.append({"type": "code", **match})

    causes = _infer_incident_causes(problem_description, evidence)
    return {
        "status": "success",
        "action": "diagnose_incident",
        "problem_description": problem_description,
        "event_time": event_time,
        "architecture_summary": architecture_summary,
        "root_cause_candidates": causes,
        "evidence": evidence,
        "verification_steps": [
            "确认异常日志发生时间是否与用户描述的现象时间一致。",
            "检查证据中的代码片段是否位于实际功能调用链上。",
            "复核关键阈值、状态机跳转条件、输入信号有效性和超时处理。",
        ],
        "fix_suggestions": [
            "优先补充复现用例或离线回放，锁定触发条件。",
            "对命中的关键函数增加边界条件检查和关键变量日志。",
            "如果是配置或阈值问题，先以最小范围调整并补充回归验证。",
        ],
        "confidence_note": "当前为规则检索和证据整理版诊断；接入 LLM 后可基于证据链生成更完整因果推理。",
    }


def _build_incident_search_query(problem_description: str, timeline: dict[str, Any] | None) -> str:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", problem_description)
    log_words: list[str] = []
    if timeline:
        for event in timeline.get("anomalies", [])[:20]:
            log_words.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", event.get("message", "")))
    candidates = [*words, *log_words]
    seen = []
    for word in candidates:
        lowered = word.lower()
        if lowered not in {item.lower() for item in seen}:
            seen.append(word)
        if len(seen) >= 12:
            break
    return " ".join(seen)


def _infer_incident_causes(problem_description: str, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = " ".join([problem_description, *(str(item.get("message") or item.get("text") or "") for item in evidence)]).lower()
    candidates: list[dict[str, Any]] = []
    rules = [
        ("timeout" in text or "超时" in text, "疑似超时或等待条件未满足，重点检查异步结果、状态机等待条件和超时阈值。"),
        ("abort" in text or "fail" in text or "失败" in text, "疑似功能主动失败或保护性退出，重点检查 abort/fail 分支和错误码映射。"),
        ("invalid" in text or "无效" in text, "疑似输入信号无效或数据校验失败，重点检查上游信号有效位和默认值。"),
        ("warn" in text or "error" in text, "日志存在 WARN/ERROR 证据，应沿对应模块和关键字回查代码路径。"),
    ]
    for matched, cause in rules:
        if matched:
            candidates.append(
                {
                    "cause": cause,
                    "confidence": 0.55,
                    "evidence_refs": list(range(min(5, len(evidence)))),
                }
            )
    if not candidates:
        candidates.append(
            {
                "cause": "现有证据不足以定位单一根因，需要更多日志、触发时间和相关模块线索。",
                "confidence": 0.25,
                "evidence_refs": list(range(min(3, len(evidence)))),
            }
        )
    return candidates


def scan_code_tree_tool_payload(project_path: str, max_files: int = 2000) -> str:
    return _to_tool_result(scan_code_tree_core(project_path=project_path, max_files=max_files))


def extract_code_symbols_tool_payload(project_path: str, max_files: int = 1200) -> str:
    return _to_tool_result(extract_code_symbols_core(project_path=project_path, max_files=max_files))


def summarize_code_files_tool_payload(project_path: str, max_files: int = 300) -> str:
    return _to_tool_result(summarize_code_files_core(project_path=project_path, max_files=max_files))


def summarize_code_architecture_tool_payload(project_path: str, max_files: int = 1200) -> str:
    return _to_tool_result(summarize_code_architecture_core(project_path=project_path, max_files=max_files))


def code_search_tool_payload(project_path: str, query: str, max_matches: int = 100) -> str:
    return _to_tool_result(code_search_core(project_path=project_path, query=query, max_matches=max_matches))


def parse_log_timeline_tool_payload(log_text: str | None = None, log_path: str | None = None) -> str:
    return _to_tool_result(parse_log_timeline_core(log_text=log_text, log_path=log_path))


def diagnose_incident_tool_payload(
    problem_description: str,
    project_path: str | None = None,
    log_text: str | None = None,
    log_path: str | None = None,
) -> str:
    return _to_tool_result(
        diagnose_incident_core(
            problem_description=problem_description,
            project_path=project_path,
            log_text=log_text,
            log_path=log_path,
        )
    )


if tool is not None:

    @tool
    def scan_code_tree_tool(project_path: str, max_files: int = 2000) -> str:
        """扫描工程目录，输出代码文件树、文件类型分布和核心目录。"""
        return scan_code_tree_tool_payload(project_path=project_path, max_files=max_files)

    @tool
    def extract_code_symbols_tool(project_path: str, max_files: int = 1200) -> str:
        """提取工程中的类、函数、方法、include/import 等符号信息。"""
        return extract_code_symbols_tool_payload(project_path=project_path, max_files=max_files)

    @tool
    def summarize_code_files_tool(project_path: str, max_files: int = 300) -> str:
        """按文件生成代码职责、关键符号和依赖摘要。"""
        return summarize_code_files_tool_payload(project_path=project_path, max_files=max_files)

    @tool
    def summarize_code_architecture_tool(project_path: str, max_files: int = 1200) -> str:
        """根据目录树和符号表生成工程架构摘要。"""
        return summarize_code_architecture_tool_payload(project_path=project_path, max_files=max_files)

    @tool
    def code_search_tool(project_path: str, query: str, max_matches: int = 100) -> str:
        """在工程代码和配置中搜索问题、日志关键字或符号名。"""
        return code_search_tool_payload(project_path=project_path, query=query, max_matches=max_matches)

    @tool
    def parse_log_timeline_tool(log_text: str | None = None, log_path: str | None = None) -> str:
        """解析日志文本或日志文件，提取时间线、等级统计和异常事件。"""
        return parse_log_timeline_tool_payload(log_text=log_text, log_path=log_path)

    @tool
    def diagnose_incident_tool(
        problem_description: str,
        project_path: str | None = None,
        log_text: str | None = None,
        log_path: str | None = None,
    ) -> str:
        """结合问题描述、日志和代码搜索证据，输出 incident 初步诊断。"""
        return diagnose_incident_tool_payload(
            problem_description=problem_description,
            project_path=project_path,
            log_text=log_text,
            log_path=log_path,
        )

else:  # pragma: no cover
    scan_code_tree_tool = scan_code_tree_tool_payload
    extract_code_symbols_tool = extract_code_symbols_tool_payload
    summarize_code_files_tool = summarize_code_files_tool_payload
    summarize_code_architecture_tool = summarize_code_architecture_tool_payload
    code_search_tool = code_search_tool_payload
    parse_log_timeline_tool = parse_log_timeline_tool_payload
    diagnose_incident_tool = diagnose_incident_tool_payload

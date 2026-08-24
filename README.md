# DOCAgent

DOCAgent is a plan-execute agent runtime. It turns a user request into a structured goal, compiles the goal into an executable DAG, dispatches tools through a registry, and supports interruption, retry, and replanning.

## What Is Included

- Goal parsing and intent detection
- Capability-first planning
- Executable DAG construction
- Tool registry and tool dispatcher
- Redis-backed scheduler and worker runtime
- Sandbox policy checks for filesystem, database, network, process, quota, and permissions
- Skill discovery, loading, reference routing, and planner context injection
- Optional web server entrypoint

## Repository Layout

```text
app.py                    CLI/runtime entrypoint
agent_runtime.py           resume, cancel, and replan orchestration
planner.py                 goal-to-DAG planner
goalparser.py              local goal parser
scheduler.py               DAG scheduler
worker.py                  worker process
tool_dispatcher.py         tool invocation layer
tools/                     built-in tools and registry
skills/                    skill registry, loader, selector, router
sandbox/                   execution policy checks
capability_acquisition/    capability acquisition helpers
web_server.py              FastAPI server
web_static/                minimal web UI
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy the example environment file if you need local services or LLM-backed planning:

```bash
cp .env.example .env
```

Do not commit `.env`.

## Quick Local Planner Check

This checks goal parsing, planning, and tool dispatch without starting deployment infrastructure:

```bash
python - <<'PY'
from app import compile_user_input_to_dag

state = compile_user_input_to_dag(
    "读取 planner.py",
    task_id="smoke-task",
    session_id="smoke-session",
)

print(state["goal"])
print(state["dag"]["nodes"])
PY
```

## CLI Usage

Submit a task:

```bash
python app.py "读取 planner.py" --task-id demo-task --session-id demo-session --no-start-workers
```

Run with workers and scheduler polling when Redis/Postgres are available:

```bash
python app.py "读取 planner.py" --task-id demo-task --session-id demo-session --wait
```

## Skills

Skills are optional local packages that provide:

- trigger metadata
- capability hints
- recommended step kinds
- default resources
- reference snippets for planner context

Set skill roots explicitly with:

```bash
export AGENT_SKILL_ROOTS=/path/to/skill_root
```

Private domain skills, logs, and target source trees should stay outside Git or be ignored locally.

## Git Hygiene

The repository intentionally ignores:

```text
.env
.venv/
logs/
data/
outputs/
ep33l_mff/
test_problem_analiz/
*.zip
```

This keeps secrets, bulky logs, generated artifacts, and private analysis material out of GitHub.

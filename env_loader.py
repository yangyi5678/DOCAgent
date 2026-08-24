"""Agent process environment loader.

这个模块只给 agent/worker 启动入口使用。普通工具不要直接读取 .env，
工具只应该通过 os.environ.get(...) 获取已经加载好的环境变量。
"""

from __future__ import annotations

import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = ROOT_DIR / ".env"


def load_agent_env(env_file: str | Path = DEFAULT_ENV_FILE) -> None:
    """Load key=value pairs from .env into os.environ if they are not set."""
    path = Path(env_file).expanduser()
    if not path.is_absolute():
        path = ROOT_DIR / path
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

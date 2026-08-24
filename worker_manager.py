"""Worker process lifecycle management for the shared worker pool."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_WORKER_COUNT = 4
DEFAULT_IDLE_TIMEOUT_SECONDS = 30.0


class WorkerManager:
    """Start and track a shared pool of worker processes."""

    def __init__(
        self,
        *,
        worker_script: Path | None = None,
        default_worker_count: int | None = None,
        default_idle_timeout_seconds: float | None = None,
    ):
        self.worker_script = worker_script or BASE_DIR / "worker.py"
        self.default_worker_count = (
            default_worker_count
            if default_worker_count is not None
            else int(
                os.getenv(
                    "WORKER_POOL_SIZE",
                    os.getenv("WORKER_COUNT_PER_TASK", str(DEFAULT_WORKER_COUNT)),
                )
            )
        )
        self.default_idle_timeout_seconds = (
            default_idle_timeout_seconds
            if default_idle_timeout_seconds is not None
            else float(
                os.getenv(
                    "WORKER_IDLE_TIMEOUT_SECONDS",
                    str(DEFAULT_IDLE_TIMEOUT_SECONDS),
                )
            )
        )
        self._pool_processes: list[subprocess.Popen[Any]] = []

    def ensure_pool_started(
        self,
        *,
        worker_count: int | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Start enough global pool workers and return debug metadata."""
        count = self._normalize_worker_count(worker_count, self.default_worker_count)
        idle_timeout = (
            self.default_idle_timeout_seconds
            if idle_timeout_seconds is None
            else idle_timeout_seconds
        )
        if count <= 0:
            return {
                "worker_count": 0,
                "pids": [],
                "started": False,
                "mode": "global_pool",
            }

        self.cleanup_finished()
        existing = self._pool_processes
        started: list[subprocess.Popen[Any]] = []
        for index in range(len(existing), count):
            worker_id = f"pool-worker-{index + 1}"
            command = [
                sys.executable,
                str(self.worker_script),
                "--pool",
                "--worker-id",
                worker_id,
                "--idle-timeout-seconds",
                str(idle_timeout),
            ]
            process = subprocess.Popen(  # noqa: S603 - command is constructed from local paths
                command,
                cwd=str(BASE_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            started.append(process)

        if started:
            self._pool_processes.extend(started)

        return {
            "worker_count": len(self._pool_processes),
            "pids": [process.pid for process in self._pool_processes],
            "started": bool(started),
            "idle_timeout_seconds": idle_timeout,
            "mode": "global_pool",
        }

    def start_workers(
        self,
        task_id: str,
        *,
        worker_count: int | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Compatibility wrapper: start the shared pool, not task-bound workers."""
        runtime = self.ensure_pool_started(
            worker_count=worker_count,
            idle_timeout_seconds=idle_timeout_seconds,
        )
        runtime["task_id"] = task_id
        return runtime

    def stop_pool(self) -> dict[str, Any]:
        """Terminate all tracked global pool workers."""
        processes = self._pool_processes
        self._pool_processes = []
        stopped: list[int] = []
        for process in processes:
            if process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            stopped.append(process.pid)

        return {
            "stopped_pids": stopped,
            "stopped_count": len(stopped),
            "mode": "global_pool",
        }

    def stop_workers(self, task_id: str) -> dict[str, Any]:
        """Compatibility wrapper: task completion does not stop the global pool."""
        self.cleanup_finished()
        return {
            "task_id": task_id,
            "worker_count": len(self._pool_processes),
            "pids": [process.pid for process in self._pool_processes],
            "stopped_count": 0,
            "mode": "global_pool",
        }

    def cleanup_finished(self, task_id: str | None = None) -> None:
        """Forget already exited worker processes."""
        self._pool_processes = [
            process
            for process in self._pool_processes
            if process.poll() is None
        ]

    @staticmethod
    def _normalize_worker_count(worker_count: int | None, default_worker_count: int) -> int:
        if worker_count is None:
            worker_count = default_worker_count
        return max(0, int(worker_count))


worker_manager = WorkerManager()

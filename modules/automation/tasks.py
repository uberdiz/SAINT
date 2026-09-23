"""Cancellable background task manager for SAINT.

Tasks run independently of the Qt/UI thread. This is intentionally separate
from foreground keyboard/mouse automation so future agents can execute work
without hijacking the user's active window.
"""

import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class AgentTask:
    id: str
    description: str
    status: TaskStatus = TaskStatus.QUEUED
    started_at: Optional[float] = None
    updated_at: float = field(default_factory=time.time)
    current_action: str = ""
    result: Optional[dict] = None
    error: Optional[str] = None
    process: Optional[subprocess.Popen] = field(default=None, repr=False)


class BackgroundTaskManager:
    def __init__(self):
        self._tasks: Dict[str, AgentTask] = {}
        self._lock = threading.RLock()

    def create_command_task(self, description: str, command: str,
                            cwd: Optional[str] = None, timeout: int = 300) -> AgentTask:
        task = AgentTask(id=uuid.uuid4().hex[:12], description=description)
        with self._lock:
            self._tasks[task.id] = task
        threading.Thread(
            target=self._run_command,
            args=(task.id, command, cwd, timeout),
            daemon=True,
            name=f"saint-task-{task.id}",
        ).start()
        return task

    def _run_command(self, task_id, command, cwd, timeout):
        with self._lock:
            task = self._tasks[task_id]
            task.status = TaskStatus.RUNNING
            task.started_at = time.time()
            task.updated_at = time.time()
            task.current_action = "running command"
        try:
            process = subprocess.Popen(
                command, cwd=cwd, shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            with self._lock:
                task.process = process
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                raise TimeoutError(f"Task timed out after {timeout}s")
            with self._lock:
                if task.status == TaskStatus.CANCELLED:
                    return
                task.result = {
                    "exit_code": process.returncode,
                    "stdout": stdout[-10000:],
                    "stderr": stderr[-10000:],
                }
                task.status = TaskStatus.COMPLETED if process.returncode == 0 else TaskStatus.FAILED
                task.error = None if process.returncode == 0 else stderr[-2000:]
                task.current_action = "finished"
                task.updated_at = time.time()
        except Exception as exc:
            with self._lock:
                if task.status != TaskStatus.CANCELLED:
                    task.status = TaskStatus.FAILED
                    task.error = str(exc)
                    task.current_action = "failed"
                    task.updated_at = time.time()

    def get(self, task_id: str) -> Optional[AgentTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                return False
            task.status = TaskStatus.CANCELLED
            task.current_action = "cancelled"
            task.updated_at = time.time()
            if task.process and task.process.poll() is None:
                task.process.terminate()
            return True

    def status(self, task_id: Optional[str] = None):
        with self._lock:
            if task_id:
                task = self._tasks.get(task_id)
                return self._serialize(task) if task else None
            return [self._serialize(t) for t in self._tasks.values()]

    @staticmethod
    def _serialize(task):
        return {
            "id": task.id,
            "description": task.description,
            "status": task.status.value,
            "started_at": task.started_at,
            "updated_at": task.updated_at,
            "current_action": task.current_action,
            "result": task.result,
            "error": task.error,
        }


task_manager = BackgroundTaskManager()

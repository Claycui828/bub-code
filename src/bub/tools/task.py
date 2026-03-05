"""Task management tools."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from bub.tools.registry import ToolRegistry

TASK_FILE_NAME = "tasks.json"


class TaskCreateInput(BaseModel):
    title: str = Field(..., description="Task title")
    description: str = Field(default="", description="Task description")


class TaskGetInput(BaseModel):
    task_id: str = Field(..., description="Task ID")


class TaskListInput(BaseModel):
    status: str | None = Field(default=None, description="Filter by status: pending, in_progress, completed, blocked")


class TaskUpdateInput(BaseModel):
    task_id: str = Field(..., description="Task ID")
    status: str | None = Field(default=None, description="New status: pending, in_progress, completed, blocked")
    title: str | None = Field(default=None, description="New title")
    description: str | None = Field(default=None, description="New description")


class TaskDeleteInput(BaseModel):
    task_id: str = Field(..., description="Task ID")


VALID_STATUSES = {"pending", "in_progress", "completed", "blocked"}


def _tasks_path(workspace: Path) -> Path:
    return workspace / ".bub" / TASK_FILE_NAME


def _load_tasks(workspace: Path) -> list[dict[str, Any]]:
    path = _tasks_path(workspace)
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_tasks(workspace: Path, tasks: list[dict[str, Any]]) -> None:
    path = _tasks_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")


def register_task_tools(registry: ToolRegistry, *, workspace: Path) -> None:
    """Register task management tools."""

    register = registry.register

    @register(name="task.create", short_description="Create a new task", model=TaskCreateInput)
    def task_create(params: TaskCreateInput) -> str:
        """Create a task with title and optional description. Returns the new task ID."""
        tasks = _load_tasks(workspace)
        task_id = str(uuid.uuid4())[:8]
        task: dict[str, Any] = {
            "id": task_id,
            "title": params.title,
            "description": params.description,
            "status": "pending",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        tasks.append(task)
        _save_tasks(workspace, tasks)
        return f"created: {task_id} title={params.title}"

    @register(name="task.get", short_description="Get task detail", model=TaskGetInput)
    def task_get(params: TaskGetInput) -> str:
        """Get details of a single task by ID."""
        tasks = _load_tasks(workspace)
        for task in tasks:
            if task["id"] == params.task_id:
                return json.dumps(task, ensure_ascii=False)
        raise RuntimeError(f"task not found: {params.task_id}")

    @register(name="task.list", short_description="List tasks", model=TaskListInput)
    def task_list(params: TaskListInput) -> str:
        """List tasks, optionally filtered by status."""
        tasks = _load_tasks(workspace)
        if params.status:
            if params.status not in VALID_STATUSES:
                raise RuntimeError(f"invalid status: {params.status}, must be one of {VALID_STATUSES}")
            tasks = [t for t in tasks if t.get("status") == params.status]
        if not tasks:
            return "(no tasks)"
        rows: list[str] = []
        for task in tasks:
            rows.append(f"{task['id']} [{task.get('status', '?')}] {task['title']}")
        return "\n".join(rows)

    @register(name="task.update", short_description="Update a task", model=TaskUpdateInput)
    def task_update(params: TaskUpdateInput) -> str:
        """Update task status, title, or description."""
        tasks = _load_tasks(workspace)
        for task in tasks:
            if task["id"] != params.task_id:
                continue
            if params.status is not None:
                if params.status not in VALID_STATUSES:
                    raise RuntimeError(f"invalid status: {params.status}, must be one of {VALID_STATUSES}")
                task["status"] = params.status
            if params.title is not None:
                task["title"] = params.title
            if params.description is not None:
                task["description"] = params.description
            task["updated_at"] = time.time()
            _save_tasks(workspace, tasks)
            return f"updated: {params.task_id} status={task['status']}"
        raise RuntimeError(f"task not found: {params.task_id}")

    @register(name="task.delete", short_description="Delete a task", model=TaskDeleteInput)
    def task_delete(params: TaskDeleteInput) -> str:
        """Delete a task by ID."""
        tasks = _load_tasks(workspace)
        new_tasks = [t for t in tasks if t["id"] != params.task_id]
        if len(new_tasks) == len(tasks):
            raise RuntimeError(f"task not found: {params.task_id}")
        _save_tasks(workspace, new_tasks)
        return f"deleted: {params.task_id}"

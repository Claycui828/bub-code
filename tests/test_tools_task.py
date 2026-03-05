import inspect
import json
from pathlib import Path

import pytest

from bub.tools.registry import ToolRegistry
from bub.tools.task import register_task_tools


def _build_registry(workspace: Path) -> ToolRegistry:
    registry = ToolRegistry()
    register_task_tools(registry, workspace=workspace)
    return registry


async def _run(registry: ToolRegistry, name: str, **kwargs: object) -> str:
    descriptor = registry.get(name)
    assert descriptor is not None, f"tool {name} not found"
    result = descriptor.tool.run(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


class TestTaskTools:
    @pytest.mark.asyncio
    async def test_create_and_list(self, tmp_path: Path) -> None:
        registry = _build_registry(tmp_path)
        result = await _run(registry, "task.create", title="Fix bug", description="segfault on startup")
        assert "created:" in result

        task_id = result.split(":")[1].strip().split()[0]

        listing = await _run(registry, "task.list")
        assert task_id in listing
        assert "[pending]" in listing
        assert "Fix bug" in listing

    @pytest.mark.asyncio
    async def test_get(self, tmp_path: Path) -> None:
        registry = _build_registry(tmp_path)
        result = await _run(registry, "task.create", title="Write docs")
        task_id = result.split(":")[1].strip().split()[0]

        detail = await _run(registry, "task.get", task_id=task_id)
        data = json.loads(detail)
        assert data["id"] == task_id
        assert data["title"] == "Write docs"
        assert data["status"] == "pending"

    @pytest.mark.asyncio
    async def test_update_status(self, tmp_path: Path) -> None:
        registry = _build_registry(tmp_path)
        result = await _run(registry, "task.create", title="Deploy")
        task_id = result.split(":")[1].strip().split()[0]

        update_result = await _run(registry, "task.update", task_id=task_id, status="in_progress")
        assert "updated:" in update_result
        assert "status=in_progress" in update_result

        detail = json.loads(await _run(registry, "task.get", task_id=task_id))
        assert detail["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_update_title(self, tmp_path: Path) -> None:
        registry = _build_registry(tmp_path)
        result = await _run(registry, "task.create", title="Old title")
        task_id = result.split(":")[1].strip().split()[0]

        await _run(registry, "task.update", task_id=task_id, title="New title")
        detail = json.loads(await _run(registry, "task.get", task_id=task_id))
        assert detail["title"] == "New title"

    @pytest.mark.asyncio
    async def test_delete(self, tmp_path: Path) -> None:
        registry = _build_registry(tmp_path)
        result = await _run(registry, "task.create", title="Temp task")
        task_id = result.split(":")[1].strip().split()[0]

        delete_result = await _run(registry, "task.delete", task_id=task_id)
        assert "deleted:" in delete_result

        listing = await _run(registry, "task.list")
        assert listing == "(no tasks)"

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self, tmp_path: Path) -> None:
        registry = _build_registry(tmp_path)
        r1 = await _run(registry, "task.create", title="Task A")
        id_a = r1.split(":")[1].strip().split()[0]
        await _run(registry, "task.create", title="Task B")

        await _run(registry, "task.update", task_id=id_a, status="completed")

        completed = await _run(registry, "task.list", status="completed")
        assert "Task A" in completed
        assert "Task B" not in completed

        pending = await _run(registry, "task.list", status="pending")
        assert "Task B" in pending
        assert "Task A" not in pending

    @pytest.mark.asyncio
    async def test_get_not_found(self, tmp_path: Path) -> None:
        registry = _build_registry(tmp_path)
        with pytest.raises(RuntimeError, match="task not found"):
            await _run(registry, "task.get", task_id="nonexistent")

    @pytest.mark.asyncio
    async def test_update_invalid_status(self, tmp_path: Path) -> None:
        registry = _build_registry(tmp_path)
        result = await _run(registry, "task.create", title="X")
        task_id = result.split(":")[1].strip().split()[0]
        with pytest.raises(RuntimeError, match="invalid status"):
            await _run(registry, "task.update", task_id=task_id, status="bad")

    @pytest.mark.asyncio
    async def test_delete_not_found(self, tmp_path: Path) -> None:
        registry = _build_registry(tmp_path)
        with pytest.raises(RuntimeError, match="task not found"):
            await _run(registry, "task.delete", task_id="nonexistent")

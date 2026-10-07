"""PostgreSQL-backed task store — tasks survive API restarts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.db.models import ExecutorRegistration, Project, Task, ToolCall
from app.db.session import SessionLocal


@dataclass
class TaskRecord:
    id: str
    project_id: str
    type: str
    payload: dict[str, Any]
    status: str = "pending"
    message: str = ""
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectRecord:
    id: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallRecord:
    id: str
    task_id: str
    tool: str
    args: dict[str, Any]
    executor_id: str | None = None
    status: str = "pending"
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class TaskStore:
    async def create_project(self, name: str, config: dict[str, Any]) -> ProjectRecord:
        async with SessionLocal() as session:
            existing = await session.get(Project, name)
            if existing:
                existing.config = config
            else:
                session.add(Project(id=name, name=name, config=config))
            await session.commit()
        return ProjectRecord(id=name, name=name, config=config)

    async def create(self, project_id: str, task_type: str, payload: dict[str, Any]) -> str:
        async with SessionLocal() as session:
            project = await session.get(Project, project_id)
            if not project:
                session.add(Project(id=project_id, name=project_id, config={}))
            task = Task(project_id=project_id, type=task_type, payload=payload)
            session.add(task)
            await session.commit()
            await session.refresh(task)
            return task.id

    async def get(self, task_id: str) -> TaskRecord | None:
        async with SessionLocal() as session:
            task = await session.get(Task, task_id)
            if not task:
                return None
            return _task_to_record(task)

    async def set_status(
        self,
        task_id: str,
        status: str,
        message: str = "",
        result: dict[str, Any] | None = None,
    ) -> None:
        async with SessionLocal() as session:
            task = await session.get(Task, task_id)
            if not task:
                return
            task.status = status
            if message:
                task.message = message
            if result is not None:
                task.result = result
            task.updated_at = datetime.now(timezone.utc)
            await session.commit()

    async def register_executor(self, executor_id: str, project_root: str) -> None:
        async with SessionLocal() as session:
            row = await session.get(ExecutorRegistration, executor_id)
            if row:
                row.project_root = project_root
                row.last_seen_at = datetime.now(timezone.utc)
            else:
                session.add(
                    ExecutorRegistration(executor_id=executor_id, project_root=project_root)
                )
            await session.commit()

    async def enqueue_tool_call(self, task_id: str, tool: str, args: dict[str, Any]) -> ToolCallRecord:
        async with SessionLocal() as session:
            call = ToolCall(task_id=task_id, tool=tool, args=args, status="pending")
            session.add(call)
            await session.commit()
            await session.refresh(call)
            return _call_to_record(call)

    async def poll_tool_call(self, executor_id: str) -> ToolCallRecord | None:
        async with SessionLocal() as session, session.begin():
            stmt = (
                select(ToolCall)
                .where(ToolCall.status == "pending")
                .order_by(ToolCall.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            result = await session.execute(stmt)
            call = result.scalar_one_or_none()
            if not call:
                return None
            call.status = "dispatched"
            call.executor_id = executor_id
            call.updated_at = datetime.now(timezone.utc)
            await session.flush()
            return _call_to_record(call)

    async def get_tool_call(self, call_id: str) -> ToolCallRecord | None:
        async with SessionLocal() as session:
            call = await session.get(ToolCall, call_id)
            if not call:
                return None
            return _call_to_record(call)

    async def complete_tool_call(
        self,
        call_id: str,
        success: bool,
        result: dict[str, Any],
        error: str | None,
    ) -> None:
        async with SessionLocal() as session:
            call = await session.get(ToolCall, call_id)
            if not call:
                return
            call.status = "done" if success else "failed"
            call.result = result
            call.error = error
            call.updated_at = datetime.now(timezone.utc)
            await session.commit()


def _task_to_record(task: Task) -> TaskRecord:
    return TaskRecord(
        id=task.id,
        project_id=task.project_id,
        type=task.type,
        payload=task.payload or {},
        status=task.status,
        message=task.message or "",
        result=task.result or {},
    )


def _call_to_record(call: ToolCall) -> ToolCallRecord:
    return ToolCallRecord(
        id=call.id,
        task_id=call.task_id,
        tool=call.tool,
        args=call.args or {},
        executor_id=call.executor_id,
        status=call.status,
        result=call.result or {},
        error=call.error,
    )


task_store = TaskStore()

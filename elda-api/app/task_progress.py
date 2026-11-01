"""Pipeline helpers for task stage progress."""

from __future__ import annotations

from elda.task_progress import format_stage_log

from app.store import task_store

__all__ = ["format_stage_log", "set_task_stage"]


async def set_task_stage(task_id: str, task_type: str, stage: str, label: str) -> None:
    await task_store.update_progress(task_id, stage, label, task_type=task_type)

"""HTTP client for elda-api."""

from __future__ import annotations

import time
from typing import Any

import httpx
from rich.console import Console

console = Console()


class EldaApiClient:
    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def health(self) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(self._url("/health"))
            r.raise_for_status()
            return r.json()

    def create_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(self._url("/v1/projects"), json=payload)
            r.raise_for_status()
            return r.json()

    def submit_task(self, task_type: str, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = {"type": task_type, "project_id": project_id, "payload": payload}
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(self._url("/v1/tasks"), json=body)
            r.raise_for_status()
            return r.json()

    def get_task(self, task_id: str) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(self._url(f"/v1/tasks/{task_id}"))
            r.raise_for_status()
            return r.json()

    def get_task_logs(self, task_id: str) -> list[str]:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(self._url(f"/v1/tasks/{task_id}/logs"))
            r.raise_for_status()
            return r.json().get("logs", [])

    @staticmethod
    def _task_progress(task: dict[str, Any]) -> dict[str, Any] | None:
        progress = task.get("progress")
        if progress:
            return progress
        result = task.get("result")
        if isinstance(result, dict):
            p = result.get("progress")
            if isinstance(p, dict):
                return p
        return None

    @classmethod
    def print_progress_update(
        cls,
        task: dict[str, Any],
        *,
        last_stage: str | None,
        new_log_lines: list[str],
    ) -> str | None:
        from elda.task_progress import parse_stage_line

        progress = cls._task_progress(task)
        current_stage = None
        if progress:
            current_stage = progress.get("current_stage")
            if current_stage and current_stage != last_stage:
                label = progress.get("label") or task.get("message", "")
                console.print(f"[bold cyan]{label}[/bold cyan]")
                for step in progress.get("stages") or []:
                    if step.get("status") == "running":
                        console.print(f"  [yellow]▸[/yellow] {step.get('label', '')}")
                    elif step.get("status") == "done":
                        console.print(f"  [green]✓[/green] {step.get('label', '')}")
        elif task.get("message") and task.get("message") != last_stage:
            console.print(f"[dim]{task.get('status', '')} — {task.get('message', '')}[/dim]")
            current_stage = task.get("message")

        for line in new_log_lines:
            if parse_stage_line(line)[0] is not None:
                continue
            text = line.strip()
            if text:
                console.print(f"[dim]  {text}[/dim]")
        if progress:
            return progress.get("current_stage") or last_stage
        return current_stage if current_stage is not None else last_stage

    def wait_task(
        self,
        task_id: str,
        poll_interval: float = 2.0,
        timeout: float = 3600.0,
        show_progress: bool = True,
    ) -> dict[str, Any]:
        deadline = time.time() + timeout
        log_cursor = 0
        last_stage: str | None = None
        while time.time() < deadline:
            task = self.get_task(task_id)
            status = task.get("status", "")
            if show_progress:
                logs = self.get_task_logs(task_id)
                new_lines = logs[log_cursor:]
                log_cursor = len(logs)
                last_stage = self.print_progress_update(
                    task, last_stage=last_stage, new_log_lines=new_lines
                )
            elif status not in ("done", "failed", "waiting_verify"):
                console.print(f"[dim]Task {task_id}: {status} — {task.get('message', '')}[/dim]")
            if status in ("done", "failed", "waiting_verify"):
                if show_progress and status in ("done", "failed"):
                    label = "完成" if status == "done" else "失败"
                    console.print(f"[bold]{label}[/bold] — {task.get('message', status)}")
                return task
            time.sleep(poll_interval)
        raise TimeoutError(f"Task {task_id} timed out after {timeout}s")

    def poll_tool_call(self, executor_id: str) -> dict[str, Any] | None:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(
                self._url("/v1/executor/poll"),
                params={"executor_id": executor_id},
            )
            r.raise_for_status()
            data = r.json()
            return data.get("tool_call")

    def submit_tool_result(
        self,
        executor_id: str,
        call_id: str,
        success: bool,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        body = {
            "executor_id": executor_id,
            "call_id": call_id,
            "success": success,
            "result": result or {},
            "error": error,
        }
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(self._url("/v1/executor/result"), json=body)
            r.raise_for_status()

    def register_executor(self, executor_id: str, project_root: str) -> None:
        body = {"executor_id": executor_id, "project_root": project_root}
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(self._url("/v1/executor/register"), json=body)
            r.raise_for_status()

    def chat(
        self,
        project_id: str,
        message: str,
        history: list[dict[str, str]] | None = None,
        model_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = {
            "project_id": project_id,
            "message": message,
            "history": history or [],
        }
        if model_config:
            body["model_keys"] = model_config.get("model_keys", {})
            body["project_root"] = model_config.get("project_root", "")
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(self._url("/v1/chat"), json=body)
            r.raise_for_status()
            return r.json()

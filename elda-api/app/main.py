"""FastAPI application — v0.3.0."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.config import settings
from app.db.session import init_db
from app.logging_setup import setup_logging
from app.orchestrator.pipeline import orchestrator
from app.secrets_loader import merge_model_keys
from app.storage.redis_queue import (
    enqueue_background,
    get_task_logs,
    set_executor_heartbeat,
)
from app.store import task_store
from app.worker import run_task_worker

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Migrations run in docker-entrypoint.sh (sync, no asyncio thread pool).
    if os.getenv("ELDA_INIT_DB_IN_LIFESPAN") == "1":
        await init_db()
    stop_event = asyncio.Event()
    delay = float(os.getenv("ELDA_WORKER_START_DELAY", "0"))

    async def _worker_after_delay() -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        await run_task_worker(stop_event)

    worker = asyncio.create_task(_worker_after_delay())
    logger.info("ELDA API %s started (worker delay=%ss)", settings.app_version, delay)
    try:
        yield
    finally:
        stop_event.set()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


app = FastAPI(title="ELDA API", version=settings.app_version, lifespan=lifespan)


class ProjectCreate(BaseModel):
    name: str
    config: dict[str, Any] = Field(default_factory=dict)


class TaskCreate(BaseModel):
    type: str
    project_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ExecutorRegister(BaseModel):
    executor_id: str
    project_root: str


class ExecutorResult(BaseModel):
    executor_id: str
    call_id: str
    success: bool
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class ChatRequest(BaseModel):
    project_id: str
    message: str
    history: list[dict[str, str]] = Field(default_factory=list)
    model_keys: dict[str, str] = Field(default_factory=dict)
    project_root: str = ""


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "elda-api", "version": settings.app_version}


@app.get("/health/ready")
async def health_ready() -> dict[str, str]:
    """DB + Redis probe — surfaces errors register/executor would hit."""
    from sqlalchemy import text

    from app.db.session import SessionLocal
    from app.storage.redis_queue import get_redis

    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        return {"status": "fail", "component": "postgres", "error": str(exc)[:200]}
    try:
        r = get_redis()
        await r.ping()
    except Exception as exc:
        return {"status": "fail", "component": "redis", "error": str(exc)[:200]}
    return {"status": "ok", "postgres": "ok", "redis": "ok"}


@app.post("/v1/projects")
async def create_project(body: ProjectCreate) -> dict[str, Any]:
    rec = await task_store.create_project(body.name, body.config)
    return {"id": rec.id, "name": rec.name}


@app.post("/v1/tasks")
async def create_task(body: TaskCreate) -> dict[str, Any]:
    body.payload = merge_model_keys(body.payload)
    task_id = await task_store.create(body.project_id, body.type, body.payload)
    await enqueue_background(task_id)
    return {"id": task_id, "type": body.type, "status": "pending"}


@app.get("/v1/tasks/{task_id}")
async def get_task(task_id: str) -> dict[str, Any]:
    task = await task_store.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return {
        "id": task.id,
        "type": task.type,
        "project_id": task.project_id,
        "status": task.status,
        "message": task.message,
        "result": task.result,
    }


@app.get("/v1/tasks/{task_id}/logs")
async def task_logs(task_id: str) -> dict[str, Any]:
    logs = await get_task_logs(task_id)
    return {"task_id": task_id, "logs": logs}


@app.post("/v1/executor/register")
async def register_executor(body: ExecutorRegister) -> dict[str, str]:
    try:
        await task_store.register_executor(body.executor_id, body.project_root)
    except Exception as exc:
        logger.exception("register_executor failed")
        raise HTTPException(
            503,
            f"executor register failed: {exc}. Verify postgres is up and elda-api DB schema is initialized.",
        ) from exc
    return {"status": "registered", "executor_id": body.executor_id}


@app.get("/v1/executor/poll")
async def poll_executor(executor_id: str) -> dict[str, Any]:
    call = await task_store.poll_tool_call(executor_id)
    if not call:
        return {"tool_call": None}
    return {"tool_call": {"id": call.id, "tool": call.tool, "args": call.args}}


@app.post("/v1/executor/result")
async def executor_result(body: ExecutorResult) -> dict[str, str]:
    await task_store.complete_tool_call(body.call_id, body.success, body.result, body.error)
    return {"status": "ok"}


@app.websocket("/v1/executor/ws")
async def executor_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    executor_id: str | None = None
    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if msg.get("type") == "register":
                executor_id = msg["executor_id"]
                await task_store.register_executor(executor_id, msg.get("project_root", ""))
                await set_executor_heartbeat(executor_id, msg)
                await websocket.send_json({"type": "registered", "executor_id": executor_id})
            elif msg.get("type") == "heartbeat" and executor_id:
                await set_executor_heartbeat(executor_id, msg)
                call = await task_store.poll_tool_call(executor_id)
                if call:
                    await websocket.send_json(
                        {"type": "tool_call", "id": call.id, "tool": call.tool, "args": call.args}
                    )
                else:
                    await websocket.send_json({"type": "heartbeat_ack"})
            elif msg.get("type") == "result" and executor_id:
                await task_store.complete_tool_call(
                    msg["call_id"], msg.get("success", False), msg.get("result", {}), msg.get("error")
                )
                await websocket.send_json({"type": "result_ack", "call_id": msg["call_id"]})
    except WebSocketDisconnect:
        logger.info("Executor WS disconnected: %s", executor_id)
    except Exception:
        logger.exception("Executor WS error (executor_id=%s)", executor_id)
        raise


@app.post("/v1/chat")
async def chat(body: ChatRequest) -> dict[str, Any]:
    payload = merge_model_keys(
        {
            "model_keys": body.model_keys,
            "project_root": body.project_root,
        }
    )
    return await orchestrator.chat(body.project_id, body.message, body.history, payload)

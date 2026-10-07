"""WebSocket-based executor client."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from websockets.sync.client import connect as ws_connect

logger = logging.getLogger(__name__)


def ws_url_from_api(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/v1/executor/ws"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/v1/executor/ws"
    return f"ws://{base}/v1/executor/ws"


class ExecutorWebSocketClient:
    def __init__(self, api_url: str, executor_id: str, project_root: str) -> None:
        self.executor_id = executor_id
        self.project_root = project_root
        self._ws_url = ws_url_from_api(api_url)

    def run_forever(
        self,
        on_tool_call: Callable[[dict[str, Any]], tuple[bool, dict[str, Any] | None, str | None]],
    ) -> None:
        with ws_connect(self._ws_url, open_timeout=30) as ws:
            ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "executor_id": self.executor_id,
                        "project_root": self.project_root,
                    }
                )
            )
            try:
                raw = ws.recv()
            except Exception as exc:
                raise RuntimeError(
                    f"Executor WebSocket closed during register ({self._ws_url}). "
                    "Check: bash scripts/compose.sh logs --tail=50 elda-api"
                ) from exc
            reg = json.loads(raw)
            if reg.get("type") != "registered":
                raise RuntimeError(f"Executor registration failed: {reg}")

            while True:
                ws.send(json.dumps({"type": "heartbeat", "executor_id": self.executor_id}))
                msg = json.loads(ws.recv())
                if msg.get("type") == "tool_call":
                    call_id = msg["id"]
                    try:
                        ok, result, error = on_tool_call(
                            {"id": call_id, "tool": msg["tool"], "args": msg.get("args", {})}
                        )
                    except Exception as exc:
                        ok, result, error = False, {}, str(exc)
                    ws.send(
                        json.dumps(
                            {
                                "type": "result",
                                "call_id": call_id,
                                "success": ok,
                                "result": result or {},
                                "error": error,
                            }
                        )
                    )
                    ack = json.loads(ws.recv())
                    if ack.get("type") != "result_ack":
                        logger.warning("Unexpected WS ack: %s", ack)
                elif msg.get("type") != "heartbeat_ack":
                    logger.debug("WS message: %s", msg)

"""DiagnosticianAgent — analyze dmesg and test app output."""

from __future__ import annotations

from typing import Any

from app.agents.base import BaseAgent


class DiagnosticianAgent(BaseAgent):
    async def analyze(
        self,
        payload: dict[str, Any],
        dmesg: str,
        app_output: str = "",
        register_map: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.bind(payload)
        who_am_i = _expected_chip_id(register_map)
        messages = [
            {
                "role": "user",
                "content": (
                    "Analyze embedded Linux bring-up test results. Output JSON:\n"
                    "{probe_ok, chip_id_ok, chip_id_value, iio_nodes_ok, irq_ok, data_ready_ok, "
                    "errors[], suggestions[]}\n"
                    "Set irq_ok false if interrupt registration failed or no IRQ events in logs. "
                    "Set data_ready_ok false if sensor data never updates.\n"
                    f"Expected WHO_AM_I: {who_am_i}\n\n"
                    f"--- DMESG ---\n{dmesg[-15000:]}\n\n"
                    f"--- APP OUTPUT ---\n{app_output[-5000:]}"
                ),
            }
        ]
        return await self.reasoner.chat_json(messages)


def _expected_chip_id(register_map: dict[str, Any] | None) -> str:
    if not register_map:
        return "unknown"
    for reg in register_map.get("registers", []):
        if reg.get("name", "").upper() in ("WHO_AM_I", "WHOAMI"):
            return str(reg.get("default", "see datasheet"))
    return "see register_map.json"

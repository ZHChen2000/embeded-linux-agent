"""PlannerAgent — driver architecture planning."""

from __future__ import annotations

from typing import Any

from app.agents.base import BaseAgent
from app.rag.service import rag_service
from app.validation.schemas import validate_driver_plan


class PlannerAgent(BaseAgent):
    async def plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.bind(payload)
        spec = payload.get("peripheral_spec") or {}
        target = payload.get("target", "peripheral")
        framework = payload.get("framework", spec.get("driver_framework", "auto"))
        bus = spec.get("bus", "spi")
        rag = await rag_service.search_all(
            f"Linux driver {target} {framework} {bus} device tree binding interrupts",
            payload,
            top_k=6,
        )
        messages = [
            {
                "role": "user",
                "content": (
                    f"Plan Linux in-tree driver for {target}, bus={bus}, framework={framework}.\n"
                    "Pick subsystem matching bus and chip (IIO, hwmon, input, misc, i2c, spi). "
                    "Plan must cover device tree interrupt-parent/interrupts/pinctrl when IRQ is required. "
                    "Output JSON DriverPlan schema:\n"
                    "subsystem, framework, rationale, compatible, probe_flow, remove_flow, "
                    "userspace_interface, main_functions, risks, references[{path,reason}].\n"
                    "Use only facts from RAG and hardware context.\n\n"
                    f"--- RAG ---\n{rag}\n\n"
                    f"--- HARDWARE ---\n{payload.get('hardware_context', '')[:6000]}"
                ),
            }
        ]
        raw = await self.reasoner.chat_json(messages)
        return validate_driver_plan(raw)

"""CoderAgent — 3-phase generate with schema validation."""

from __future__ import annotations

from typing import Any

from app.agents.base import BaseAgent
from app.rag.service import rag_service
from app.validation.schemas import infer_module_paths_from_patches, validate_patch_envelope


class CoderAgent(BaseAgent):
    PHASES = ("driver", "dts", "kbuild")

    async def generate_phase(self, payload: dict[str, Any], phase: str) -> list[dict[str, str]]:
        self.bind(payload)
        if phase not in self.PHASES:
            raise ValueError(f"Unknown generate phase: {phase}")
        spec = payload.get("peripheral_spec") or {}
        peripheral = payload.get("current_peripheral", payload.get("target", "device"))
        bus = spec.get("bus", "spi")
        framework = spec.get("framework", spec.get("driver_framework", "auto"))
        hw = payload.get("hardware_context", "")
        rag = await rag_service.search_all(
            f"Linux {framework} {bus} driver {peripheral} device tree binding",
            payload,
            top_k=8,
        )
        driver_hint = (
            f"Generate ONLY in-tree C driver for bus={bus}, framework={framework}. "
            "Use IIO for ADC/IMU sensors, hwmon for environmental sensors, input for touch, "
            "misc/platform/char for QSPI or custom IP, i2c/spi core helpers when needed. "
            "Include of_device_id, devm_*, MODULE_* as appropriate."
        )
        dts_hint = (
            "Generate ONLY device tree nodes for this peripheral. "
            "If IRQ GPIO is in hardware context, set interrupt-parent, interrupts, and pinctrl. "
            "I2C nodes must match reg address; SPI/QSPI nodes need reg or cs-gpios and bus frequency."
        )
        instructions = {
            "driver": driver_hint,
            "dts": dts_hint,
            "kbuild": (
                "Generate Kconfig fragment, Makefile fragment, and userspace test app (.c)."
            ),
        }
        messages = [
            {
                "role": "user",
                "content": (
                    f"Phase: {phase} for {peripheral} (bus={bus}, framework={framework}).\n"
                    f"{instructions[phase]}\n"
                    'Output JSON PatchEnvelope: {"version":"1","patches":[{"id","unified_diff","rationale"}]}\n'
                    f"--- RAG ---\n{rag}\n--- HARDWARE ---\n{hw[:12000]}"
                ),
            }
        ]
        raw = await self.coder.chat_json(messages)
        patches = validate_patch_envelope(raw)
        for p in patches:
            p["phase"] = phase
        return patches

    async def generate_all(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        patches: list[dict[str, str]] = []
        for phase in self.PHASES:
            patches.extend(await self.generate_phase(payload, phase))
        return patches

    @staticmethod
    def build_manifest(patches: list[dict[str, str]]) -> dict[str, Any]:
        module_paths = infer_module_paths_from_patches(patches)
        return {
            "module_paths": module_paths,
            "patch_ids": [p["id"] for p in patches],
            "phases": [p.get("phase") for p in patches],
        }

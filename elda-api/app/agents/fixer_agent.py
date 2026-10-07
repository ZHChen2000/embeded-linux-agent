"""FixerAgent — structured build error repair with RAG."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.agents.base import BaseAgent
from app.build.log_parser import parse_build_log
from app.rag.service import rag_service
from app.validation.schemas import validate_fix_patch

logger = logging.getLogger(__name__)


class FixerAgent(BaseAgent):
    async def fix(self, payload: dict[str, Any], build_log: str, round_num: int) -> dict[str, str]:
        self.bind(payload)
        parsed = parse_build_log(build_log)
        if not parsed["errors"]:
            raise ValueError("No parseable errors in build log")

        rag = await rag_service.search_all(
            " ".join(e["message"][:80] for e in parsed["errors"][:5]),
            payload,
            top_k=5,
        )
        messages = [
            {
                "role": "user",
                "content": (
                    f"Fix Linux kernel build errors (round {round_num}).\n"
                    f"Structured errors JSON:\n{parsed}\n"
                    f"RAG hints:\n{rag}\n"
                    "Output JSON: {id, unified_diff, rationale}. Fix C/Makefile/Kconfig/DTS."
                ),
            }
        ]
        raw = await self.coder.chat_json(messages)
        patch = validate_fix_patch(raw)
        return {
            "id": patch.get("id", f"fix-r{round_num}-{uuid.uuid4().hex[:6]}"),
            "unified_diff": patch["unified_diff"],
            "rationale": patch.get("rationale", ""),
            "errors_fixed": [e["message"] for e in parsed["errors"][:5]],
        }

    async def fix_test(
        self,
        payload: dict[str, Any],
        diag: dict[str, Any],
        round_num: int,
    ) -> dict[str, str]:
        self.bind(payload)
        rag = await rag_service.search_all(
            " ".join(str(s) for s in diag.get("suggestions", [])[:5]),
            payload,
            top_k=6,
        )
        messages = [
            {
                "role": "user",
                "content": (
                    f"Fix bring-up failures from board test (round {round_num}).\n"
                    f"Diagnosis JSON:\n{diag}\n"
                    f"RAG hints:\n{rag}\n"
                    "Output JSON: {id, unified_diff, rationale}. "
                    "Prefer DTS interrupt/pinctrl fixes, then driver IRQ handler, then probe order."
                ),
            }
        ]
        raw = await self.coder.chat_json(messages)
        patch = validate_fix_patch(raw)
        return {
            "id": patch.get("id", f"testfix-r{round_num}-{uuid.uuid4().hex[:6]}"),
            "unified_diff": patch["unified_diff"],
            "rationale": patch.get("rationale", ""),
        }

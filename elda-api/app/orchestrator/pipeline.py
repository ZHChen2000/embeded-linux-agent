"""Pipeline orchestration."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.agents.chat_agent import ChatAgent
from app.agents.coder_agent import CoderAgent
from app.agents.diagnostician_agent import DiagnosticianAgent
from app.agents.extractor_agent import ExtractorAgent
from app.agents.fixer_agent import FixerAgent
from app.agents.planner_agent import PlannerAgent
from app.agents.report_agent import ReportAgent, write_reports
from app.executor_bridge import ExecutorBridge
from app.logging_setup import bind_context
from app.storage.minio_store import upload_bytes, upload_file
from app.storage.redis_queue import publish_task_log
from app.store import TaskRecord, task_store
from app.validation.schemas import infer_module_paths_from_patches

logger = logging.getLogger(__name__)

WORKSPACE_FILES = ("register_map.json", "init_sequence.yaml", "pin_requirements.yaml")
DEFAULT_PROJECT_ROOT = "."
DEFAULT_MODULE_PATHS = ("drivers/iio",)
REPORTS_DIR = "reports"
OUTPUT_LOG_DIR = Path("output") / "logs"

DTS_PATH_RE = re.compile(r"^[+-]{3}\s+[ab]/(?P<path>.*\.(?:dts|dtsi))$", re.MULTILINE)
DRIVER_IRQ_RE = re.compile(
    r"\b(devm_request(?:_threaded)?_irq|request_irq|gpio_to_irq|"
    r"platform_get_irq|of_irq_get|irq_of_parse_and_map)\b",
    re.IGNORECASE,
)
DTS_IRQ_RE = re.compile(r"\b(interrupt-parent|interrupts(?:-extended)?)\b", re.IGNORECASE)


class PipelineOrchestrator:
    def __init__(self) -> None:
        self.extractor = ExtractorAgent()
        self.planner = PlannerAgent()
        self.coder = CoderAgent()
        self.fixer = FixerAgent()
        self.diagnostician = DiagnosticianAgent()
        self.reporter = ReportAgent()
        self.chat_agent = ChatAgent()
        self.executor = ExecutorBridge()

    async def _log(self, task_id: str, msg: str) -> None:
        logger.info(msg)
        await publish_task_log(task_id, msg)

    async def run_task(self, task_id: str) -> None:
        task = await task_store.get(task_id)
        if not task:
            return
        bind_context(task_id=task_id)
        await task_store.set_status(task_id, "running")
        try:
            handlers = {
                "ingest": self._run_ingest,
                "board_validate": self._run_board_validate,
                "plan": self._run_plan,
                "generate_driver": self._run_generate_driver,
                "generate_dts": self._run_generate_dts,
                "generate_kbuild": self._run_generate_kbuild,
                "generate_all": self._run_generate_all,
                "build": self._run_build,
                "deploy": self._run_deploy,
                "test": self._run_test,
                "report": self._run_report,
                "index_kernel": self._run_index_kernel,
                "import_vendor": self._run_import_vendor,
            }
            handler = handlers.get(task.type)
            if not handler:
                raise ValueError(f"Unknown task type: {task.type}")
            await handler(task)
        except Exception as exc:
            logger.exception("Task %s failed", task_id)
            await self._log(task_id, f"FAILED: {exc}")
            await task_store.set_status(task_id, "failed", message=str(exc))

    def _enabled_peripherals(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Return enabled peripherals with stable id/name fields.

        The old implementation silently returned an empty list and later code could
        report a successful generation with zero patches. For board bring-up tasks
        that is usually a configuration error, not a successful pipeline run.
        """
        raw = payload.get("peripherals_enabled") or []
        if not isinstance(raw, list):
            raise ValueError("peripherals_enabled must be a list of peripheral objects")

        peripherals: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                raise ValueError(f"peripherals_enabled[{index}] must be an object")
            name = item.get("name") or item.get("id")
            if not name:
                raise ValueError(f"peripherals_enabled[{index}] requires either 'name' or 'id'")
            pid = item.get("id") or name
            peripherals.append({**dict(item), "id": str(pid), "name": str(name)})
        return peripherals

    def _project_root_path(self, payload: dict[str, Any]) -> Path:
        return Path(str(payload.get("project_root") or DEFAULT_PROJECT_ROOT))

    def _peripheral_payload(
        self, payload: dict[str, Any], peripheral: dict[str, Any]
    ) -> dict[str, Any]:
        name = peripheral["name"]
        return {
            **payload,
            "target": name,
            "current_peripheral": name,
            "current_peripheral_id": peripheral["id"],
            "current_peripheral_config": peripheral,
        }

    def _normalise_generated_patch(
        self,
        patch: Mapping[str, Any],
        peripheral: dict[str, Any],
        phase: str,
        index: int,
    ) -> dict[str, str]:
        if not isinstance(patch, Mapping):
            raise ValueError(
                f"Coder returned non-object patch for {peripheral['name']} phase={phase}"
            )

        unified_diff = patch.get("unified_diff")
        if not isinstance(unified_diff, str) or not unified_diff.strip():
            raise ValueError(
                f"Coder returned empty unified_diff for {peripheral['name']} phase={phase}"
            )

        patch_id = patch.get("id") or f"{peripheral['id']}-{phase}-{index}"
        return {
            "id": str(patch_id),
            "unified_diff": unified_diff,
            "rationale": str(patch.get("rationale") or ""),
        }

    async def _apply_generated_patches(
        self,
        task: TaskRecord,
        raw_patches: list[Mapping[str, Any]],
        peripheral: dict[str, Any],
        phase: str,
    ) -> list[dict[str, str]]:
        applied: list[dict[str, str]] = []
        for index, raw_patch in enumerate(raw_patches, start=1):
            patch = self._normalise_generated_patch(raw_patch, peripheral, phase, index)
            result = await self.executor.tool_call(
                task.id,
                "git.apply_patch",
                {
                    "id": patch["id"],
                    "unified_diff": patch["unified_diff"],
                    "rationale": patch["rationale"],
                },
                wait=True,
            )
            if isinstance(result, Mapping) and result.get("success") is False:
                raise RuntimeError(f"Patch apply failed: {patch['id']}")
            applied.append(patch)
            await self._log(
                task.id,
                f"Applied patch {patch['id']} ({peripheral['name']} / {phase})",
            )
        return applied

    def _write_json_report(self, root: Path, name: str, data: Any) -> Path:
        path = root / REPORTS_DIR / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def _build_generation_validation(
        self,
        patches: list[dict[str, str]],
        peripherals: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Detect likely DTS/IRQ mismatches before the user reaches runtime debug.

        This is intentionally conservative. It reports warnings instead of hard
        failures because an existing board DTS may already provide IRQ resources
        that are not visible in the generated diff.
        """
        combined_diff = "\n".join(patch["unified_diff"] for patch in patches)
        changed_paths = sorted(
            {
                path
                for patch in patches
                for path in _extract_paths_from_diff(patch["unified_diff"])
            }
        )
        touches_dts = any(path.endswith((".dts", ".dtsi")) for path in changed_paths)
        driver_uses_irq = bool(DRIVER_IRQ_RE.search(combined_diff))
        dts_declares_irq = bool(DTS_IRQ_RE.search(combined_diff))
        config_mentions_irq = any(_peripheral_mentions_irq(p) for p in peripherals)

        warnings: list[str] = []
        if (driver_uses_irq or config_mentions_irq) and not touches_dts:
            warnings.append(
                "IRQ-capable peripheral or driver code detected, but no .dts/.dtsi patch was generated."
            )
        if touches_dts and (driver_uses_irq or config_mentions_irq) and not dts_declares_irq:
            warnings.append(
                "Generated DTS touches device-tree files but does not add interrupt-parent/interruption properties."
            )
        if driver_uses_irq and not dts_declares_irq:
            warnings.append(
                "Driver appears to request IRQ resources; generated diff does not show matching DTS IRQ resources."
            )

        return {
            "changed_paths": changed_paths,
            "driver_uses_irq": driver_uses_irq,
            "dts_declares_irq": dts_declares_irq,
            "config_mentions_irq": config_mentions_irq,
            "warnings": warnings,
        }

    async def _run_ingest(self, task: TaskRecord) -> None:
        payload = dict(task.payload)
        root = payload.get("project_root", ".")

        soc_pdf = payload.get("soc_datasheet")
        if soc_pdf:
            await self._log(task.id, f"PDF extract SOC: {soc_pdf}")
            soc_mineru = await self.executor.tool_call(
                task.id, "pdf.mineru_extract", {"pdf_path": soc_pdf}, wait=True
            )
            soc_drafts = await self.extractor.extract({**payload, **soc_mineru}, doc_type="soc")
            await self.executor.write_workspace_files(root, soc_drafts, subdir="soc")
            if soc_mineru.get("markdown"):
                try:
                    await rag_service_index(task, soc_pdf, soc_mineru["markdown"], payload)
                except Exception as exc:
                    await self._log(task.id, f"Milvus index skipped (SOC): {exc}")
                upload_bytes(
                    f"{task.project_id}/soc/{Path(soc_pdf).name}",
                    soc_mineru["markdown"].encode(),
                )

        peripherals = payload.get("peripherals_datasheets") or []
        if not peripherals and payload.get("peripheral_datasheet"):
            peripherals = [{"id": "default", "path": payload["peripheral_datasheet"]}]
        if not peripherals:
            raise ValueError("No peripheral datasheets — configure elda.yaml peripherals[].datasheet")

        for item in peripherals:
            pid = item.get("id", "default")
            pdf_path = item["path"]
            await self._log(task.id, f"PDF extract peripheral {pid}: {pdf_path}")
            mineru = await self.executor.tool_call(
                task.id, "pdf.mineru_extract", {"pdf_path": pdf_path}, wait=True
            )
            pp = {**payload, **mineru}
            drafts = await self.extractor.extract(pp, doc_type="peripheral")
            await self.executor.write_workspace_files(root, drafts, subdir=f"peripherals/{pid}")
            if mineru.get("markdown"):
                try:
                    n = await rag_service_index(task, pdf_path, mineru["markdown"], payload)
                    await self._log(task.id, f"Indexed {n} chunks for {pid}")
                except Exception as exc:
                    await self._log(task.id, f"Milvus index skipped ({pid}): {exc}")
                p = Path(pdf_path)
                if p.is_file():
                    upload_bytes(f"{task.project_id}/datasheets/{pid}/{p.name}", p.read_bytes())

        await task_store.set_status(
            task.id,
            "waiting_verify",
            message="Review workspace/soc and workspace/peripherals/* then: elda verify workspace",
        )

    async def _run_board_validate(self, task: TaskRecord) -> None:
        result = await self.executor.tool_call(task.id, "board.conflict_check", {}, wait=True)
        if result.get("has_hard_errors"):
            raise RuntimeError("Board has hard conflicts — fix elda.yaml (duplicate CS/I2C address)")
        await task_store.set_status(task.id, "done", message="Board validation complete", result=result)

    async def _load_hardware_context(self, project_root: str) -> str:
        root = Path(project_root)
        parts: list[str] = []
        soc_dir = root / "workspace" / "soc"
        if soc_dir.is_dir():
            parts.append(_read_workspace_dir(soc_dir, "soc"))
        periph_root = root / "workspace" / "peripherals"
        if periph_root.is_dir():
            for sub in sorted(periph_root.iterdir()):
                if sub.is_dir():
                    parts.append(_read_workspace_dir(sub, f"peripheral:{sub.name}"))
        else:
            parts.append(_read_workspace_dir(root / "workspace", "workspace"))
        return "\n\n".join(p for p in parts if p)

    async def _run_plan(self, task: TaskRecord) -> None:
        payload = dict(task.payload)
        payload["hardware_context"] = await self._load_hardware_context(payload.get("project_root", "."))
        plans = []
        for p in self._enabled_peripherals(payload):
            pp = {**payload, "target": p.get("name", "device"), "current_peripheral": p.get("name")}
            plan = await self.planner.plan(pp)
            plans.append({"peripheral_id": p.get("id"), "plan": plan})
        root = self._project_root_path(payload)
        plan_path = root / REPORTS_DIR / "driver_plan.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plans, indent=2, ensure_ascii=False), encoding="utf-8")
        await task_store.set_status(task.id, "done", message="Plan generated", result={"plans": plans})

    async def _run_generate_driver(self, task: TaskRecord) -> None:
        await self._run_generate_phases(task, ("driver",))

    async def _run_generate_dts(self, task: TaskRecord) -> None:
        await self._run_generate_phases(task, ("dts",))

    async def _run_generate_kbuild(self, task: TaskRecord) -> None:
        await self._run_generate_phases(task, ("kbuild",))

    async def _run_generate_all(self, task: TaskRecord) -> None:
        await self._run_generate_phases(task, CoderAgent.PHASES)

    async def _run_generate_phases(self, task: TaskRecord, phases: tuple[str, ...]) -> None:
        payload = dict(task.payload)
        root = self._project_root_path(payload)
        payload["hardware_context"] = await self._load_hardware_context(str(root))
        peripherals = self._enabled_peripherals(payload)
        if not peripherals:
            raise ValueError("No enabled peripherals — configure elda.yaml peripherals_enabled")

        all_patches: list[dict[str, str]] = []
        phase_results: list[dict[str, Any]] = []

        for peripheral in peripherals:
            pp = self._peripheral_payload(payload, peripheral)
            for phase in phases:
                await self._log(task.id, f"Generating {phase} for {peripheral['name']}")
                raw_patches = await self.coder.generate_phase(pp, phase)
                applied = await self._apply_generated_patches(
                    task, raw_patches, peripheral, phase
                )
                all_patches.extend(applied)
                phase_results.append(
                    {
                        "peripheral_id": peripheral["id"],
                        "peripheral": peripheral["name"],
                        "phase": phase,
                        "patch_count": len(applied),
                        "patch_ids": [patch["id"] for patch in applied],
                    }
                )

        manifest = CoderAgent.build_manifest(all_patches)
        manifest["module_paths"] = _normalise_module_paths(
            manifest.get("module_paths")
            or payload.get("driver_module_paths")
            or list(DEFAULT_MODULE_PATHS)
        )
        manifest_path = self._write_json_report(root, "driver_manifest.json", manifest)

        validation = self._build_generation_validation(all_patches, peripherals)
        validation_path = self._write_json_report(root, "generation_validation.json", validation)

        warning_count = len(validation["warnings"])
        message = f"Applied {len(all_patches)} patches"
        if warning_count:
            message += f"; {warning_count} generation warning(s)"
            for warning in validation["warnings"]:
                await self._log(task.id, f"GENERATION WARNING: {warning}")

        await task_store.set_status(
            task.id,
            "done",
            message=message,
            result={
                "count": len(all_patches),
                "manifest": str(manifest_path),
                "validation": str(validation_path),
                "phase_results": phase_results,
                "warnings": validation["warnings"],
            },
        )

    async def _run_build(self, task: TaskRecord) -> None:
        payload = dict(task.payload)
        max_rounds = int(payload.get("max_fix_rounds", 10))
        root = self._project_root_path(payload)
        module_paths = _resolve_module_paths(root, payload)
        build_history: list[dict[str, Any]] = []

        for round_i in range(max_rounds + 1):
            await self._log(
                task.id,
                f"Build round {round_i}: modules={module_paths}, board_dts={payload.get('board_dts')}",
            )
            logs: list[str] = []
            component_results: list[dict[str, Any]] = []
            all_ok = True

            for module_path in module_paths:
                mod = await self.executor.tool_call(
                    task.id, "build.make_module", {"module_path": module_path}, wait=True
                )
                success = bool(mod.get("success", False))
                component_results.append(
                    {"type": "module", "path": module_path, "success": success}
                )
                logs.append(mod.get("log", ""))
                all_ok = all_ok and success

            if payload.get("board_dts"):
                dtc = await self.executor.tool_call(
                    task.id, "build.dtc", {"dts_path": payload["board_dts"]}, wait=True
                )
                success = bool(dtc.get("success", False))
                component_results.append(
                    {"type": "dtc", "path": payload["board_dts"], "success": success}
                )
                logs.append(dtc.get("log", ""))
                all_ok = all_ok and success

            if payload.get("build_zimage", True):
                zimg = await self.executor.tool_call(task.id, "build.make_zimage", {}, wait=True)
                success = bool(zimg.get("success", False))
                component_results.append({"type": "zimage", "success": success})
                logs.append(zimg.get("log", ""))
                all_ok = all_ok and success

            full_log = "\n".join(logs)
            log_file = root / OUTPUT_LOG_DIR / f"build_round_{round_i}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(full_log, encoding="utf-8")
            upload_file(f"{task.project_id}/logs/build_round_{round_i}.log", log_file)

            parsed = await self.executor.tool_call(
                task.id, "build.parse_log", {"log": full_log}, wait=True
            )
            round_report = {
                "round": round_i,
                "components": component_results,
                "all_ok": all_ok,
                "parsed": parsed,
                "log_file": str(log_file),
            }
            build_history.append(round_report)
            self._write_json_report(root, f"build_round_{round_i}.json", round_report)

            if all_ok and parsed.get("error_count", 0) == 0:
                await task_store.set_status(
                    task.id,
                    "done",
                    message="Build succeeded",
                    result={"rounds": build_history, "module_paths": module_paths},
                )
                return

            if round_i >= max_rounds:
                break

            patch = await self.fixer.fix(payload, full_log, round_i + 1)
            applied = await self._apply_generated_patches(
                task,
                [patch],
                {"id": "fixer", "name": "fixer"},
                f"fix_round_{round_i + 1}",
            )
            new_paths = infer_module_paths_from_patches(applied)
            if new_paths:
                module_paths = list(dict.fromkeys(module_paths + new_paths))

        await task_store.set_status(
            task.id,
            "failed",
            message=f"Build failed after {max_rounds} fix round(s)",
            result={"rounds": build_history, "module_paths": module_paths},
        )

    async def _run_deploy(self, task: TaskRecord) -> None:
        await self.executor.tool_call(task.id, "deploy.tftp_copy", {}, wait=True)
        checklist = await self.executor.tool_call(task.id, "deploy.manual_checklist", {}, wait=True)
        await task_store.set_status(
            task.id, "done", message="Deployed + manual checklist", result=checklist
        )

    async def _run_test(self, task: TaskRecord) -> None:
        payload = dict(task.payload)
        dmesg = payload.get("log_content", "")
        app_out = payload.get("app_output", "")
        reg_map = _load_register_map(Path(payload.get("project_root", ".")))
        result = await self.diagnostician.analyze(payload, dmesg, app_out, reg_map)
        ok = _runtime_test_passed(result)
        root = self._project_root_path(payload)
        out = self._write_json_report(root, "test_result.json", result)
        status = "done" if ok else "failed"
        await task_store.set_status(
            task.id,
            status,
            message=f"Test analyzed ({'passed' if ok else 'failed'})",
            result={**result, "report": str(out)},
        )

    async def _run_report(self, task: TaskRecord) -> None:
        payload = dict(task.payload)
        root = self._project_root_path(payload)
        artifacts = {
            "project": task.project_id,
            "hardware": await self._load_hardware_context(str(root)),
            "test": _read_json(root / REPORTS_DIR / "test_result.json"),
            "plan": _read_text(root / REPORTS_DIR / "driver_plan.md"),
            "manifest": _read_json(root / REPORTS_DIR / "driver_manifest.json"),
        }
        payload["hardware_context"] = artifacts["hardware"]
        gen = await self.reporter.generate(payload, artifacts)
        paths = write_reports(str(root), gen["markdown"])
        upload_file(f"{task.project_id}/reports/final_report.md", Path(paths["markdown"]))
        await task_store.set_status(task.id, "done", message="Report written", result=paths)

    async def _run_index_kernel(self, task: TaskRecord) -> None:
        result = await self.executor.tool_call(
            task.id, "milvus.index_kernel", {"paths": task.payload.get("paths")}, wait=True
        )
        await task_store.set_status(task.id, "done", message="Kernel indexed", result=result)

    async def _run_import_vendor(self, task: TaskRecord) -> None:
        source = task.payload.get("source_path")
        if not source:
            raise ValueError("source_path required")
        result = await self.executor.tool_call(
            task.id, "milvus.index_vendor", {"source_path": source}, wait=True
        )
        await task_store.set_status(task.id, "done", message="Vendor driver indexed", result=result)

    async def chat(
        self,
        project_id: str,
        message: str,
        history: list[dict[str, str]],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        if payload.get("project_root"):
            payload["hardware_context"] = await self._load_hardware_context(payload["project_root"])
        return await self.chat_agent.reply(project_id, message, history, payload)


async def rag_service_index(
    task: TaskRecord, pdf_path: str, markdown: str, payload: dict[str, Any]
) -> int:
    from app.rag.service import rag_service

    return await rag_service.index_markdown(
        "hardware_docs", str(pdf_path), str(pdf_path), markdown, payload
    )


def _extract_paths_from_diff(unified_diff: str) -> list[str]:
    paths: list[str] = []
    for match in DTS_PATH_RE.finditer(unified_diff):
        paths.append(match.group("path"))
    for line in unified_diff.splitlines():
        if line.startswith(("+++ b/", "--- a/")):
            paths.append(line[6:])
    return list(dict.fromkeys(paths))


def _peripheral_mentions_irq(peripheral: dict[str, Any]) -> bool:
    irq_keys = {
        "irq",
        "irq_gpio",
        "irq_pin",
        "interrupt",
        "interrupts",
        "interrupt_gpio",
        "interrupt_pin",
    }
    if any(key in peripheral and peripheral.get(key) not in (None, "", False) for key in irq_keys):
        return True
    text = json.dumps(peripheral, ensure_ascii=False, default=str).lower()
    return "irq" in text or "interrupt" in text


def _normalise_module_paths(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [str(item) for item in value if item]
    else:
        candidates = []
    return list(dict.fromkeys(path.strip() for path in candidates if path.strip()))


def _runtime_test_passed(result: dict[str, Any]) -> bool:
    required_checks = ("probe_ok", "chip_id_ok")
    optional_runtime_checks = ("irq_ok", "data_ready_ok", "read_ok")

    if not all(bool(result.get(check)) for check in required_checks):
        return False
    for check in optional_runtime_checks:
        if check in result and not bool(result.get(check)):
            return False
    return True


def _read_workspace_dir(ws_dir: Path, label: str) -> str:
    if not ws_dir.is_dir():
        return ""
    parts = [f"=== {label} ==="]
    for name in WORKSPACE_FILES:
        p = ws_dir / name
        if p.is_file():
            parts.append(f"--- {name} ---\n{p.read_text(encoding='utf-8', errors='replace')[:8000]}")
    return "\n".join(parts)


def _load_register_map(root: Path) -> dict[str, Any] | None:
    candidates = [
        root / "workspace" / "peripherals",
        root / "workspace",
    ]
    for base in candidates:
        if base.name == "peripherals" and base.is_dir():
            for sub in sorted(base.iterdir()):
                p = sub / "register_map.json"
                if p.is_file():
                    return json.loads(p.read_text(encoding="utf-8"))
        p = base / "register_map.json"
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


def _resolve_module_paths(root: Path, payload: dict[str, Any]) -> list[str]:
    manifest = root / REPORTS_DIR / "driver_manifest.json"
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        paths = _normalise_module_paths(data.get("module_paths"))
        if paths:
            return paths

    paths = _normalise_module_paths(payload.get("driver_module_paths"))
    return paths or list(DEFAULT_MODULE_PATHS)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text()) if path.is_file() else {}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


orchestrator = PipelineOrchestrator()

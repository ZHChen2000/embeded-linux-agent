"""Pipeline orchestration."""

from __future__ import annotations

import json
import logging
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
from app.task_progress import format_stage_log, set_task_stage
from app.validation.schemas import infer_module_paths_from_patches

logger = logging.getLogger(__name__)

WORKSPACE_FILES = ("register_map.json", "init_sequence.yaml", "pin_requirements.yaml")


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

    async def _stage(self, task: TaskRecord, stage: str, label: str) -> None:
        await self._log(task.id, format_stage_log(stage, label))
        await set_task_stage(task.id, task.type, stage, label)

    async def run_task(self, task_id: str) -> None:
        task = await task_store.get(task_id)
        if not task:
            return
        bind_context(task_id=task_id)
        await task_store.set_status(task_id, "running")
        await self._stage(task, "start", f"开始执行：{task.type}")
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
        return payload.get("peripherals_enabled", [])

    async def _run_ingest(self, task: TaskRecord) -> None:
        payload = dict(task.payload)
        root = payload.get("project_root", ".")

        soc_pdf = payload.get("soc_datasheet")
        if soc_pdf:
            await self._stage(task, "pdf_extract_soc", f"解析 SoC 数据手册：{soc_pdf}")
            soc_mineru = await self.executor.tool_call(
                task.id, "pdf.mineru_extract", {"pdf_path": soc_pdf}, wait=True
            )
            await self._stage(task, "extract_soc", "抽取 SoC 硬件信息")
            soc_drafts = await self.extractor.extract({**payload, **soc_mineru}, doc_type="soc")
            await self.executor.write_workspace_files(root, soc_drafts, subdir="soc")
            if soc_mineru.get("markdown"):
                try:
                    await self._stage(task, "index_soc", "写入 SoC 向量索引")
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
            await self._stage(task, f"pdf_extract_{pid}", f"解析外设 PDF（{pid}）")
            mineru = await self.executor.tool_call(
                task.id, "pdf.mineru_extract", {"pdf_path": pdf_path}, wait=True
            )
            await self._stage(task, f"extract_{pid}", f"抽取外设硬件信息（{pid}）")
            pp = {**payload, **mineru}
            drafts = await self.extractor.extract(pp, doc_type="peripheral")
            await self.executor.write_workspace_files(root, drafts, subdir=f"peripherals/{pid}")
            if mineru.get("markdown"):
                try:
                    await self._stage(task, f"index_{pid}", f"写入向量索引（{pid}）")
                    n = await rag_service_index(task, pdf_path, mineru["markdown"], payload)
                    await self._log(task.id, f"Indexed {n} chunks for {pid}")
                except Exception as exc:
                    await self._log(task.id, f"Milvus index skipped ({pid}): {exc}")
                p = Path(pdf_path)
                if p.is_file():
                    upload_bytes(f"{task.project_id}/datasheets/{pid}/{p.name}", p.read_bytes())

        await self._stage(
            task,
            "wait_verify",
            "等待人工审核 workspace，完成后执行：elda verify workspace",
        )
        await task_store.set_status(
            task.id,
            "waiting_verify",
            message="Review workspace/soc and workspace/peripherals/* then: elda verify workspace",
        )

    async def _run_board_validate(self, task: TaskRecord) -> None:
        await self._stage(task, "board_check", "板级资源冲突检查")
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
        await self._stage(task, "load_context", "加载 workspace 硬件描述")
        payload["hardware_context"] = await self._load_hardware_context(payload.get("project_root", "."))
        plans = []
        for p in self._enabled_peripherals(payload):
            await self._stage(
                task,
                "plan_agent",
                f"规划驱动方案：{p.get('name', 'device')}",
            )
            pp = {
                **payload,
                "target": p.get("name", "device"),
                "current_peripheral": p.get("name"),
                "peripheral_spec": p,
                "framework": p.get("framework", p.get("driver_framework", "auto")),
            }
            plan = await self.planner.plan(pp)
            plans.append({"peripheral_id": p.get("id"), "plan": plan})
        root = Path(payload.get("project_root", "."))
        plan_path = root / "reports" / "driver_plan.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        await self._stage(task, "write_report", "写入 reports/driver_plan.md")
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
        await self._stage(task, "load_context", "加载 workspace 硬件描述")
        payload["hardware_context"] = await self._load_hardware_context(payload.get("project_root", "."))
        root = Path(payload.get("project_root", "."))
        all_patches: list[dict[str, str]] = []
        for p in self._enabled_peripherals(payload):
            pp = {
                **payload,
                "target": p["name"],
                "current_peripheral": p["name"],
                "peripheral_spec": p,
                "framework": p.get("framework", p.get("driver_framework", "auto")),
            }
            for phase in phases:
                await self._stage(
                    task,
                    f"generate_{phase}",
                    f"生成 {p.get('name', 'device')} / {phase}",
                )
                patches = await self.coder.generate_phase(pp, phase)
                for patch in patches:
                    await self.executor.tool_call(
                        task.id,
                        "git.apply_patch",
                        {
                            "id": patch["id"],
                            "unified_diff": patch["unified_diff"],
                            "rationale": patch.get("rationale", ""),
                        },
                        wait=True,
                    )
                all_patches.extend(patches)
        manifest_path = root / "reports" / "driver_manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest = CoderAgent.build_manifest(all_patches)
        if not manifest["module_paths"]:
            manifest["module_paths"] = payload.get("driver_module_paths", ["drivers/iio"])
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        await self._stage(task, "apply_patches", f"已应用 {len(all_patches)} 个补丁，写入 manifest")
        if "dts" in phases and payload.get("board_dts"):
            await self._stage(task, "dts_validate", "校验设备树（dtc）")
            dts_res = await self.executor.tool_call(
                task.id,
                "dts.validate",
                {"dts_path": payload["board_dts"]},
                wait=True,
            )
            if dts_res.get("has_errors"):
                raise RuntimeError("DTS validation failed after generate")
        await task_store.set_status(
            task.id,
            "done",
            message=f"Applied {len(all_patches)} patches",
            result={"count": len(all_patches), "manifest": str(manifest_path)},
        )

    async def _run_build(self, task: TaskRecord) -> None:
        payload = dict(task.payload)
        max_rounds = int(payload.get("max_fix_rounds", 10))
        fix_enabled = payload.get("fix", True)
        targets = payload.get("build_targets") or ["module", "dtc", "zimage", "dtb"]
        root = Path(payload.get("project_root", "."))
        module_paths = _resolve_module_paths(root, payload)

        for round_i in range(max_rounds + 1):
            await self._stage(
                task,
                f"build_round_{round_i}",
                f"编译第 {round_i + 1} 轮：模块与 DTS",
            )
            logs: list[str] = []
            all_ok = True
            if "module" in targets:
                for mp in module_paths:
                    mod = await self.executor.tool_call(
                        task.id, "build.make_module", {"module_path": mp}, wait=True
                    )
                    logs.append(mod.get("log", ""))
                    if not mod.get("success", False):
                        all_ok = False
            if "dtc" in targets and payload.get("board_dts"):
                dtc = await self.executor.tool_call(
                    task.id, "build.dtc", {"dts_path": payload["board_dts"]}, wait=True
                )
                logs.append(dtc.get("log", ""))
                if not dtc.get("success", False):
                    all_ok = False

            full_log = "\n".join(logs)
            log_file = root / "output" / "logs" / f"build_round_{round_i}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(full_log, encoding="utf-8")
            upload_file(f"{task.project_id}/logs/build_round_{round_i}.log", log_file)

            parsed = await self.executor.tool_call(
                task.id, "build.parse_log", {"log": full_log}, wait=True
            )
            compile_ok = all_ok and parsed.get("error_count", 0) == 0
            if compile_ok:
                if "zimage" in targets:
                    await self._stage(task, "zimage", "构建内核 zImage")
                    zimg = await self.executor.tool_call(
                        task.id, "build.make_zimage", {}, wait=True
                    )
                    zlog = zimg.get("log", "")
                    log_file.write_text(full_log + "\n" + zlog, encoding="utf-8")
                    upload_file(f"{task.project_id}/logs/build_round_{round_i}.log", log_file)
                    zparsed = await self.executor.tool_call(
                        task.id, "build.parse_log", {"log": zlog}, wait=True
                    )
                    if not zimg.get("success", False) or zparsed.get("error_count", 0):
                        compile_ok = False
                        full_log = full_log + "\n" + zlog
                    else:
                        await task_store.set_status(task.id, "done", message="Build succeeded")
                        return
                else:
                    await task_store.set_status(task.id, "done", message="Build succeeded")
                    return
            if not fix_enabled or round_i >= max_rounds:
                break
            await self._stage(
                task,
                f"build_round_{round_i}_fix",
                f"编译失败，自动修复第 {round_i + 1} 轮",
            )
            patch = await self.fixer.fix(payload, full_log, round_i + 1)
            await self.executor.tool_call(
                task.id,
                "git.apply_patch",
                {"id": patch["id"], "unified_diff": patch["unified_diff"], "rationale": patch.get("rationale")},
                wait=True,
            )
            new_paths = infer_module_paths_from_patches([patch])
            if new_paths:
                module_paths = list(dict.fromkeys(module_paths + new_paths))
        await task_store.set_status(task.id, "failed", message="Build failed after fix rounds")

    async def _run_deploy(self, task: TaskRecord) -> None:
        payload = dict(task.payload)
        root = Path(payload.get("project_root", "."))
        await self._stage(task, "tftp", "拷贝 zImage/dtb 到 TFTP 目录")
        await self.executor.tool_call(task.id, "deploy.tftp_copy", {}, wait=True)
        module_paths = _resolve_module_paths(root, payload)
        await self._stage(task, "nfs", "同步驱动模块到 NFS")
        nfs_res = await self.executor.tool_call(
            task.id,
            "deploy.nfs_modules",
            {"module_paths": module_paths},
            wait=True,
        )
        await self._stage(task, "checklist", "生成上板检查清单")
        checklist = await self.executor.tool_call(task.id, "deploy.manual_checklist", {}, wait=True)
        await task_store.set_status(
            task.id,
            "done",
            message="Deployed + manual checklist",
            result={**checklist, "nfs_copied": nfs_res.get("copied", [])},
        )

    async def _run_test(self, task: TaskRecord) -> None:
        payload = dict(task.payload)
        root = Path(payload.get("project_root", "."))
        dmesg = payload.get("log_content", "")
        app_out = payload.get("app_output", "")
        if not dmesg and payload.get("capture_serial", True):
            await self._stage(task, "capture", "从串口采集运行日志")
            cap = await self.executor.tool_call(task.id, "serial.capture", {}, wait=True)
            extracted = await self.executor.tool_call(
                task.id,
                "serial.extract_log",
                {"text": cap.get("log", "")},
                wait=True,
            )
            dmesg = extracted.get("dmesg") or cap.get("log", "")
            if not app_out:
                app_out = extracted.get("app_output", "")
            serial_path = root / "output" / "logs" / "serial_capture.log"
            serial_path.parent.mkdir(parents=True, exist_ok=True)
            serial_path.write_text(cap.get("log", ""), encoding="utf-8")
        reg_map = _load_register_map(root)
        max_test_rounds = int(payload.get("max_test_fix_rounds", 2))
        auto_fix = payload.get("auto_fix_test", True)
        result: dict[str, Any] = {}
        for round_i in range(max_test_rounds + 1):
            await self._stage(task, "analyze", f"分析测试结果（第 {round_i + 1} 轮）")
            result = await self.diagnostician.analyze(payload, dmesg, app_out, reg_map)
            ok = (
                result.get("probe_ok")
                and result.get("chip_id_ok")
                and result.get("irq_ok", True)
                and result.get("data_ready_ok", True)
            )
            out = root / "reports" / "test_result.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, indent=2), encoding="utf-8")
            if ok:
                await task_store.set_status(task.id, "done", message="Test passed", result=result)
                return
            if not auto_fix or round_i >= max_test_rounds:
                break
            await self._stage(
                task,
                f"test_round_{round_i}_fix",
                f"测试未通过，自动修复并重编（第 {round_i + 1} 轮）",
            )
            patch = await self.fixer.fix_test(payload, result, round_i + 1)
            await self.executor.tool_call(
                task.id,
                "git.apply_patch",
                {
                    "id": patch["id"],
                    "unified_diff": patch["unified_diff"],
                    "rationale": patch.get("rationale", ""),
                },
                wait=True,
            )
            module_paths = _resolve_module_paths(root, payload)
            for mp in module_paths:
                await self.executor.tool_call(
                    task.id, "build.make_module", {"module_path": mp}, wait=True
                )
            if payload.get("board_dts"):
                await self.executor.tool_call(
                    task.id, "build.dtc", {"dts_path": payload["board_dts"]}, wait=True
                )
        await task_store.set_status(task.id, "failed", message="Test failed", result=result)

    async def _run_report(self, task: TaskRecord) -> None:
        payload = dict(task.payload)
        root = Path(payload.get("project_root", "."))
        await self._stage(task, "gather", "汇总工程产物与测试结果")
        artifacts = {
            "project": task.project_id,
            "hardware": await self._load_hardware_context(str(root)),
            "test": _read_json(root / "reports" / "test_result.json"),
            "plan": _read_text(root / "reports" / "driver_plan.md"),
            "manifest": _read_json(root / "reports" / "driver_manifest.json"),
        }
        payload["hardware_context"] = artifacts["hardware"]
        await self._stage(task, "write", "生成最终报告")
        gen = await self.reporter.generate(payload, artifacts)
        paths = write_reports(str(root), gen["markdown"])
        upload_file(f"{task.project_id}/reports/final_report.md", Path(paths["markdown"]))
        await task_store.set_status(task.id, "done", message="Report written", result=paths)

    async def _run_index_kernel(self, task: TaskRecord) -> None:
        await self._stage(task, "index", "索引内核源码到 Milvus")
        result = await self.executor.tool_call(
            task.id, "milvus.index_kernel", {"paths": task.payload.get("paths")}, wait=True
        )
        await task_store.set_status(task.id, "done", message="Kernel indexed", result=result)

    async def _run_import_vendor(self, task: TaskRecord) -> None:
        source = task.payload.get("source_path")
        if not source:
            raise ValueError("source_path required")
        await self._stage(task, "index", f"索引厂商驱动：{source}")
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
    manifest = root / "reports" / "driver_manifest.json"
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        paths = data.get("module_paths") or []
        if paths:
            return list(dict.fromkeys(paths))
    return payload.get("driver_module_paths") or ["drivers/iio"]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text()) if path.is_file() else {}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


orchestrator = PipelineOrchestrator()

"""Task stage markers shared by elda-api and CLI."""

from __future__ import annotations

import re
from typing import Any

STAGE_LINE_RE = re.compile(r"^\[stage:([^\]]+)\]\s*(.*)$")

# (stage_id, short label) — order defines progress bar steps when applicable
TASK_STAGE_TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "ingest": [
        ("start", "启动任务"),
        ("pdf_extract", "解析 PDF"),
        ("extract_agent", "抽取硬件信息"),
        ("index_rag", "向量索引"),
        ("wait_verify", "等待审核"),
    ],
    "board_validate": [
        ("start", "启动任务"),
        ("board_check", "板级冲突检查"),
        ("done", "完成"),
    ],
    "plan": [
        ("start", "启动任务"),
        ("load_context", "加载 workspace"),
        ("plan_agent", "生成驱动方案"),
        ("write_report", "写入报告"),
    ],
    "generate_driver": [
        ("start", "启动任务"),
        ("load_context", "加载 workspace"),
        ("generate", "生成驱动代码"),
        ("apply_patches", "应用补丁"),
    ],
    "generate_dts": [
        ("start", "启动任务"),
        ("load_context", "加载 workspace"),
        ("generate", "生成设备树"),
        ("dts_validate", "校验 DTS"),
        ("apply_patches", "应用补丁"),
    ],
    "generate_kbuild": [
        ("start", "启动任务"),
        ("load_context", "加载 workspace"),
        ("generate", "生成 Kbuild"),
        ("apply_patches", "应用补丁"),
    ],
    "generate_all": [
        ("start", "启动任务"),
        ("load_context", "加载 workspace"),
        ("generate", "生成代码与 DTS"),
        ("dts_validate", "校验 DTS"),
        ("apply_patches", "应用补丁"),
    ],
    "build": [
        ("start", "启动任务"),
        ("compile", "编译模块与 DTS"),
        ("fix", "自动修复"),
        ("zimage", "构建 zImage"),
        ("done", "完成"),
    ],
    "deploy": [
        ("start", "启动任务"),
        ("tftp", "TFTP 部署"),
        ("nfs", "NFS 同步模块"),
        ("checklist", "生成检查清单"),
    ],
    "test": [
        ("start", "启动任务"),
        ("capture", "采集串口日志"),
        ("analyze", "分析测试结果"),
        ("fix", "自动修复并重测"),
    ],
    "report": [
        ("start", "启动任务"),
        ("gather", "汇总产物"),
        ("write", "生成报告"),
    ],
    "index_kernel": [
        ("start", "启动任务"),
        ("index", "索引内核源码"),
    ],
    "import_vendor": [
        ("start", "启动任务"),
        ("index", "索引厂商驱动"),
    ],
}


def format_stage_log(stage: str, message: str) -> str:
    return f"[stage:{stage}] {message}"


def parse_stage_line(line: str) -> tuple[str | None, str]:
    m = STAGE_LINE_RE.match(line.strip())
    if not m:
        return None, line
    return m.group(1), m.group(2)


def _map_dynamic_stage(task_type: str, stage: str) -> str:
    """Map fine-grained stage ids onto template step ids."""
    if task_type == "build":
        if stage.startswith("build_round_") and stage.endswith("_fix"):
            return "fix"
        if stage.startswith("build_round_"):
            return "compile"
        if stage == "zimage":
            return "zimage"
        if stage == "done":
            return "done"
    if task_type == "test" and stage.startswith("test_round_"):
        return "fix"
    if task_type in ("generate_driver", "generate_dts", "generate_kbuild", "generate_all"):
        if stage.startswith("generate_"):
            return "generate"
        if stage == "apply_patches":
            return "apply_patches"
    if task_type == "ingest" and stage.startswith("pdf_extract"):
        return "pdf_extract"
    if task_type == "ingest" and stage.startswith("extract_"):
        return "extract_agent"
    if task_type == "ingest" and stage.startswith("index_"):
        return "index_rag"
    return stage


def compute_progress(
    task_type: str,
    stage: str,
    label: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ = previous
    template = TASK_STAGE_TEMPLATES.get(task_type, [])
    mapped = _map_dynamic_stage(task_type, stage)
    stages: list[dict[str, str]] = []
    if template:
        stage_ids = [s[0] for s in template]
        idx = stage_ids.index(mapped) if mapped in stage_ids else min(len(stage_ids) - 1, 0)
        for i, (sid, slabel) in enumerate(template):
            if i < idx:
                status = "done"
            elif i == idx:
                status = "running"
            else:
                status = "pending"
            stages.append({"id": sid, "label": slabel, "status": status})
    else:
        stages = [{"id": stage, "label": label, "status": "running"}]

    return {
        "current_stage": stage,
        "label": label,
        "stages": stages,
    }

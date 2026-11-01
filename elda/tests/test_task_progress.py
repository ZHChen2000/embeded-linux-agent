"""Tests for task stage progress helpers."""

from elda.task_progress import (
    compute_progress,
    format_stage_log,
    parse_stage_line,
)


def test_format_and_parse_stage_log() -> None:
    line = format_stage_log("pdf_extract", "解析 PDF")
    assert line == "[stage:pdf_extract] 解析 PDF"
    stage, msg = parse_stage_line(line)
    assert stage == "pdf_extract"
    assert msg == "解析 PDF"


def test_compute_progress_build_round_maps_to_compile() -> None:
    p = compute_progress("build", "build_round_2", "编译第 3 轮")
    assert p["current_stage"] == "build_round_2"
    running = [s for s in p["stages"] if s["status"] == "running"]
    assert running and running[0]["id"] == "compile"


def test_compute_progress_ingest_pdf_step() -> None:
    p = compute_progress("ingest", "pdf_extract_icm", "解析外设 PDF")
    running = [s for s in p["stages"] if s["status"] == "running"]
    assert running[0]["id"] == "pdf_extract"

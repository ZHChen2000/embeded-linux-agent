"""elda doctor — environment self-check."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from elda.config import load_project_config
from elda.secrets_loader import load_api_secrets
from elda.ingest.pdf_extract import available_pdf_backends
from elda.validation import validate_project_config


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def run_doctor(project: bool = True) -> list[CheckResult]:
    results: list[CheckResult] = []
    secrets = load_api_secrets()
    results.append(_check("bailian_api_key", bool(secrets.bailian.api_key), "secrets/api_keys.yaml"))
    results.append(_check("deepseek_api_key", bool(secrets.deepseek.api_key), "secrets/api_keys.yaml"))
    results.append(_check_tool("git", "git"))
    results.append(_check_tool("dtc", "dtc"))
    results.append(_check_tool("rg", "ripgrep"))
    results.append(_check_pdf_extract())
    results.append(_check_docker())
    results.append(_check_api())

    if project:
        yaml_path = Path.cwd() / "elda.yaml"
        if yaml_path.is_file():
            try:
                cfg, _ = load_project_config()
                issues = validate_project_config(cfg)
                results.append(_check("elda.yaml", not issues, "; ".join(issues) or "ok"))
                if cfg.target.cross_compile:
                    cc = f"{cfg.target.cross_compile}gcc"
                    results.append(_check_tool(cc, f"cross compiler {cc}"))
            except Exception as exc:
                results.append(CheckResult("elda.yaml", False, str(exc)))
        else:
            results.append(
                CheckResult(
                    "elda.yaml",
                    True,
                    "skipped (no elda.yaml in cwd — run from demo/<project>/ for Demo)",
                )
            )

    return results


def _check(name: str, ok: bool, detail: str) -> CheckResult:
    return CheckResult(name=name, ok=ok, detail=detail)


def _check_tool(cmd: str, label: str) -> CheckResult:
    path = shutil.which(cmd.split()[0])
    return CheckResult(label, path is not None, path or "not in PATH")


def _check_pdf_extract() -> CheckResult:
    backends = available_pdf_backends()
    if backends:
        return CheckResult("pdf_extract", True, ", ".join(backends))
    return CheckResult(
        "pdf_extract",
        False,
        "need pymupdf (pip install pymupdf) or pdftotext (apt install poppler-utils)",
    )


def _check_docker() -> CheckResult:
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=10)
        return CheckResult("docker", True, "daemon running")
    except Exception as exc:
        return CheckResult("docker", False, str(exc))


def _check_api() -> CheckResult:
    try:
        cfg, _ = load_project_config()
        url = cfg.project.api_url
    except FileNotFoundError:
        url = "http://localhost:8000"
    try:
        r = httpx.get(f"{url.rstrip('/')}/health", timeout=5.0)
        return CheckResult("elda-api", r.status_code == 200, r.text[:100])
    except Exception as exc:
        return CheckResult("elda-api", False, str(exc))

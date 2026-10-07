"""PDF → markdown/text for ingest (MinerU optional; PyMuPDF / pdftotext fallback)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from elda.ingest.mineru import MinerUError, parse_pdf_mineru


class PDFExtractError(RuntimeError):
    pass


def _result(
    markdown: str,
    md_path: Path,
    output_dir: Path,
    backend: str,
) -> dict[str, str | list[str]]:
    return {
        "markdown": markdown,
        "markdown_paths": [str(md_path)],
        "output_dir": str(output_dir),
        "extract_backend": backend,
    }


def _parse_pymupdf(pdf_path: Path, output_dir: Path) -> dict[str, str | list[str]]:
    try:
        import fitz  # pymupdf
    except ImportError as exc:
        raise PDFExtractError("pymupdf not installed (pip install pymupdf)") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / f"{pdf_path.stem}.md"
    parts: list[str] = []
    with fitz.open(pdf_path) as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text") or ""
            parts.append(f"<!-- page {i} -->\n{text.rstrip()}\n")
    body = "\n".join(parts).strip()
    if not body:
        raise PDFExtractError("pymupdf extracted no text")
    md_path.write_text(body + "\n", encoding="utf-8")
    return _result(body, md_path, output_dir, "pymupdf")


def _parse_pdftotext(pdf_path: Path, output_dir: Path) -> dict[str, str | list[str]]:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        raise PDFExtractError("pdftotext not found (apt install poppler-utils)")

    output_dir.mkdir(parents=True, exist_ok=True)
    txt_path = output_dir / f"{pdf_path.stem}.txt"
    md_path = output_dir / f"{pdf_path.stem}.md"
    proc = subprocess.run(
        [pdftotext, "-layout", str(pdf_path), str(txt_path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise PDFExtractError(f"pdftotext failed: {proc.stderr or proc.stdout}")
    if not txt_path.is_file() or txt_path.stat().st_size == 0:
        raise PDFExtractError("pdftotext produced empty output")

    body = txt_path.read_text(encoding="utf-8", errors="replace").strip()
    md_path.write_text(body + "\n", encoding="utf-8")
    return _result(body, md_path, output_dir, "pdftotext")


def available_pdf_backends() -> list[str]:
    names: list[str] = []
    if shutil.which("mineru"):
        names.append("mineru")
    try:
        import fitz  # noqa: F401

        names.append("pymupdf")
    except ImportError:
        pass
    if shutil.which("pdftotext"):
        names.append("pdftotext")
    return names


def parse_pdf(pdf_path: Path, output_dir: Path) -> dict[str, str | list[str]]:
    """Try MinerU → PyMuPDF → pdftotext; return markdown bundle for Extractor/RAG."""
    if not pdf_path.is_file():
        raise PDFExtractError(f"PDF not found: {pdf_path}")

    backends: list[tuple[str, Callable[[Path, Path], dict[str, str | list[str]]]]] = []
    if shutil.which("mineru"):
        backends.append(("mineru", parse_pdf_mineru))
    backends.append(("pymupdf", _parse_pymupdf))
    if shutil.which("pdftotext"):
        backends.append(("pdftotext", _parse_pdftotext))

    errors: list[str] = []
    for name, fn in backends:
        try:
            return fn(pdf_path, output_dir)
        except (MinerUError, PDFExtractError) as exc:
            errors.append(f"{name}: {exc}")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    raise PDFExtractError(
        "All PDF backends failed. "
        + "; ".join(errors)
        + " — on Ubuntu 16.04: pip install pymupdf && sudo apt install poppler-utils"
    )

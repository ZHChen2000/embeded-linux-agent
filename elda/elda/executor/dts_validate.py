from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from elda.config import EldaConfig, PeripheralConfig


class DtsIssue(BaseModel):
    severity: str
    peripheral_id: str
    message: str


class DtsValidationReport(BaseModel):
    issues: list[DtsIssue] = Field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

    def to_markdown(self) -> str:
        lines = ["# DTS validation", ""]
        if not self.issues:
            lines.append("No issues.")
            return "\n".join(lines)
        for item in self.issues:
            tag = "ERROR" if item.severity == "error" else "WARNING"
            lines.append(f"## [{tag}] {item.peripheral_id}")
            lines.append(item.message)
            lines.append("")
        return "\n".join(lines)


def validate_board_dts(cfg: EldaConfig, dts_path: Path) -> DtsValidationReport:
    report = DtsValidationReport()
    if not dts_path.is_file():
        report.issues.append(
            DtsIssue(
                severity="error",
                peripheral_id="board",
                message=f"DTS file missing: {dts_path}",
            )
        )
        return report
    text = dts_path.read_text(encoding="utf-8", errors="replace")
    for p in cfg.enabled_peripherals():
        _check_peripheral_dts(report, p, text)
    _check_global_interrupt_syntax(report, text)
    return report


def _check_global_interrupt_syntax(report: DtsValidationReport, text: str) -> None:
    if "interrupts" in text and "interrupt-parent" not in text:
        report.issues.append(
            DtsIssue(
                severity="error",
                peripheral_id="board",
                message="DTS uses interrupts without interrupt-parent.",
            )
        )


def _check_peripheral_dts(report: DtsValidationReport, p: PeripheralConfig, text: str) -> None:
    slug = re.sub(r"[^a-z0-9]", "", p.name.lower())
    block = _find_node_block(text, p.name, slug)
    scope = block if block else text
    if p.board.gpios and p.board.gpios.irq:
        if not re.search(r"interrupts\s*=", scope, re.IGNORECASE):
            report.issues.append(
                DtsIssue(
                    severity="error",
                    peripheral_id=p.id,
                    message="elda.yaml defines IRQ GPIO but DTS node lacks interrupts property.",
                )
            )
        if not re.search(r"interrupt-parent\s*=", scope, re.IGNORECASE):
            report.issues.append(
                DtsIssue(
                    severity="error",
                    peripheral_id=p.id,
                    message="elda.yaml defines IRQ GPIO but DTS node lacks interrupt-parent.",
                )
            )
        if not re.search(r"pinctrl-\d+\s*=", scope, re.IGNORECASE) and not re.search(
            r"pinctrl-names\s*=", scope, re.IGNORECASE
        ):
            report.issues.append(
                DtsIssue(
                    severity="warning",
                    peripheral_id=p.id,
                    message="No pinctrl binding found near peripheral node; verify GPIO mux for IRQ.",
                )
            )
    if p.bus == "i2c" and p.board.i2c:
        addr = p.board.i2c.address.lower().replace("0x", "")
        if block and not re.search(rf"reg\s*=\s*<[^>]*0x{addr}[^>]*>", block, re.IGNORECASE):
            if not re.search(rf"reg\s*=\s*<[^>]*{addr}[^>]*>", block, re.IGNORECASE):
                report.issues.append(
                    DtsIssue(
                        severity="error",
                        peripheral_id=p.id,
                        message="I2C address in elda.yaml does not match reg in DTS node.",
                    )
                )
    if p.bus in ("spi", "qspi") and p.board.spi:
        if block and not re.search(r"cs-gpios|reg\s*=", block, re.IGNORECASE):
            report.issues.append(
                DtsIssue(
                    severity="warning",
                    peripheral_id=p.id,
                    message="SPI/QSPI node should define reg or cs-gpios.",
                )
            )


def _find_node_block(text: str, name: str, slug: str) -> str | None:
    patterns = [
        rf"{re.escape(name)}\s*@",
        rf"{re.escape(slug)}\s*@",
        rf"compatible\s*=\s*\"[^\"]*{re.escape(slug)}",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        start = text.rfind("\n", 0, m.start())
        start = max(start, 0)
        depth = 0
        i = start
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0 and i > m.start():
                    return text[start : i + 1]
            i += 1
    return None

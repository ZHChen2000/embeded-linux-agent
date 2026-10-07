from __future__ import annotations

import re


def split_serial_capture(text: str) -> dict[str, str]:
    dmesg_lines: list[str] = []
    other_lines: list[str] = []
    for line in text.splitlines():
        if re.match(r"^\[\s*\d+\.\d+\]", line):
            dmesg_lines.append(line)
        elif line.strip():
            other_lines.append(line)
    return {
        "dmesg": "\n".join(dmesg_lines),
        "app_output": "\n".join(other_lines),
    }

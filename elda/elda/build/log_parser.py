from __future__ import annotations

import re
from typing import Any

ERROR_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("missing_header", re.compile(r"fatal error:\s*([^:]+):\s*No such file", re.IGNORECASE)),
    ("dts_syntax", re.compile(r"syntax error", re.IGNORECASE)),
    ("dtc", re.compile(r"FATAL ERROR|Error.*dts|dtc", re.IGNORECASE)),
    ("undefined_symbol", re.compile(r"undefined reference to", re.IGNORECASE)),
    ("struct_field", re.compile(r"has no member named", re.IGNORECASE)),
    ("signature_mismatch", re.compile(r"conflicting types for", re.IGNORECASE)),
    ("api_version", re.compile(r"implicit declaration of function", re.IGNORECASE)),
    ("makefile_path", re.compile(r"No rule to make target", re.IGNORECASE)),
]


def parse_build_log(log: str) -> dict[str, Any]:
    lines = log.splitlines()
    errors: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        lower = line.lower()
        if "error:" not in lower and "fatal" not in lower:
            continue
        category = "other"
        for cat, pat in ERROR_PATTERNS:
            if pat.search(line):
                category = cat
                break
        errors.append({"category": category, "message": line.strip(), "line_index": i})
    return {"errors": errors, "error_count": len(errors)}

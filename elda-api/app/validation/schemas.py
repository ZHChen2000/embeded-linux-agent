"""Load repo schemas and validate agent outputs."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _schema_dir() -> Path:
    for candidate in (
        Path("/app/schemas"),
        _REPO_ROOT / "schemas",
        Path.cwd() / "schemas",
    ):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("schemas/ directory not found")


_DIFF_PATH_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_DIFF_HEADER_RE = re.compile(r"^(---|\+\+\+|@@)", re.MULTILINE)


@lru_cache(maxsize=8)
def _load_schema(name: str) -> dict[str, Any]:
    path = _schema_dir() / name
    if not path.is_file():
        raise FileNotFoundError(f"Schema not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_patch_envelope(data: dict[str, Any]) -> list[dict[str, str]]:
    """Validate PatchEnvelope schema; return normalized patch list."""
    jsonschema.validate(data, _load_schema("patch_envelope.json"))
    patches: list[dict[str, str]] = []
    for p in data["patches"]:
        diff = p["unified_diff"]
        _validate_unified_diff(diff)
        patches.append(
            {
                "id": p["id"],
                "unified_diff": diff,
                "rationale": p.get("rationale", ""),
                "target_path": p.get("target_path", ""),
            }
        )
    return patches


def validate_fix_patch(data: dict[str, Any]) -> dict[str, str]:
    if not data.get("unified_diff"):
        raise jsonschema.ValidationError("fix patch missing unified_diff")
    _validate_unified_diff(data["unified_diff"])
    return {
        "id": data.get("id", "fix"),
        "unified_diff": data["unified_diff"],
        "rationale": data.get("rationale", ""),
    }


def validate_driver_plan(data: dict[str, Any]) -> dict[str, Any]:
    jsonschema.validate(data, _load_schema("driver_plan.json"))
    return data


def validate_extractor_output(data: dict[str, Any]) -> None:
    for key in ("register_map", "init_sequence", "pin_requirements"):
        if key not in data or not data[key]:
            raise jsonschema.ValidationError(f"extractor missing required field: {key}")
    reg = data["register_map"]
    if not isinstance(reg.get("registers"), list) or not reg["registers"]:
        raise jsonschema.ValidationError("register_map.registers must be a non-empty list")


def _validate_unified_diff(diff: str) -> None:
    if not diff or not diff.strip():
        raise jsonschema.ValidationError("unified_diff is empty")
    if not _DIFF_HEADER_RE.search(diff):
        raise jsonschema.ValidationError("unified_diff is not a valid unified diff")


def extract_kernel_paths_from_diff(diff: str) -> list[str]:
    paths = _DIFF_PATH_RE.findall(diff)
    return list(dict.fromkeys(paths))


def infer_module_paths_from_patches(patches: list[dict[str, str]]) -> list[str]:
    """Derive make M= paths from patch target files."""
    module_paths: list[str] = []
    for p in patches:
        for path in extract_kernel_paths_from_diff(p["unified_diff"]):
            if path.startswith("drivers/"):
                parts = path.split("/")
                if len(parts) >= 2:
                    module_paths.append(f"drivers/{parts[1]}")
            elif path.startswith("arch/"):
                continue
    return list(dict.fromkeys(module_paths))


def validate_dts_irq_conflict(dts: str, irqs: list[int]) -> None:
    if not dts or not dts.strip():
        return
    pattern = re.compile(r"interrupts(?:-extended)?\s*=\s*<([^>]+)>", re.IGNORECASE)
    found: set[int] = set()
    for m in pattern.finditer(dts):
        content = m.group(1)
        nums = re.findall(r"0x[0-9a-fA-F]+|\d+", content)
        for n in nums:
            try:
                val = int(n, 0)
            except Exception:
                continue
            found.add(val)
    conflicts = found.intersection(set(irqs))
    if conflicts:
        raise jsonschema.ValidationError(f"IRQ conflict detected: {sorted(list(conflicts))}")

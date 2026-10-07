"""Resolve kernel module build paths from elda.yaml and driver manifest."""

from __future__ import annotations

import json
from pathlib import Path

from elda.config import EldaConfig, PeripheralConfig

FRAMEWORK_MODULE_PATHS: dict[str, str] = {
    "iio": "drivers/iio",
    "input": "drivers/input",
    "hwmon": "drivers/hwmon",
    "spi": "drivers/spi",
    "i2c": "drivers/i2c",
    "rtc": "drivers/rtc",
    "led": "drivers/leds",
    "misc": "drivers/misc",
    "qspi": "drivers/misc",
    "platform": "drivers/misc",
    "char": "drivers/char",
    "auto": "drivers",
}


def resolve_peripheral_module_path(peripheral: PeripheralConfig) -> str:
    if peripheral.driver_module_path:
        return peripheral.driver_module_path.strip().rstrip("/")
    fw = peripheral.driver_framework.lower()
    if fw in FRAMEWORK_MODULE_PATHS:
        return FRAMEWORK_MODULE_PATHS[fw]
    return "drivers"


def driver_module_paths_from_config(cfg: EldaConfig) -> list[str]:
    paths = [resolve_peripheral_module_path(p) for p in cfg.enabled_peripherals()]
    return list(dict.fromkeys(paths)) or ["drivers"]


def driver_module_paths_from_manifest(project_root: Path) -> list[str] | None:
    manifest = project_root / "reports" / "driver_manifest.json"
    if not manifest.is_file():
        return None
    data = json.loads(manifest.read_text(encoding="utf-8"))
    paths = data.get("module_paths") or []
    return list(dict.fromkeys(paths)) if paths else None


def resolve_driver_module_paths(cfg: EldaConfig, project_root: Path) -> list[str]:
    from_manifest = driver_module_paths_from_manifest(project_root)
    if from_manifest:
        return from_manifest
    return driver_module_paths_from_config(cfg)

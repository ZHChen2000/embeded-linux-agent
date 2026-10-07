"""Pydantic models for elda.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class TargetConfig(BaseModel):
    soc: str
    arch: str = "arm"
    kernel_version: str
    kernel_source: str
    git_branch: str = "elda/default"
    cross_compile: str


class ModelConfig(BaseModel):
    bailian_api_key: str = ""
    deepseek_api_key: str = ""
    code_model: str = "qwen3-coder-plus"
    reasoning_model: str = "deepseek-v4-pro"
    bailian_reasoning_model: str = "qwen3-max"
    embedding_model: str = "text-embedding-v4"


class MilvusConfig(BaseModel):
    host: str = "localhost"
    port: int = 19530


class SpiBoardConfig(BaseModel):
    bus_id: int
    cs_gpio: str
    mode: int = 0
    max_frequency: int = 1_000_000


class I2cBoardConfig(BaseModel):
    bus_id: int
    address: str


class GpioBoardConfig(BaseModel):
    reset: str | None = None
    irq: str | None = None


class PowerBoardConfig(BaseModel):
    vcc: str = "3.3V"


class PeripheralBoardConfig(BaseModel):
    spi: SpiBoardConfig | None = None
    i2c: I2cBoardConfig | None = None
    gpios: GpioBoardConfig | None = None
    power: PowerBoardConfig | None = None


class PeripheralConfig(BaseModel):
    id: str
    name: str
    enabled: bool = True
    bus: str
    driver_framework: str = "auto"
    driver_module_path: str = ""
    datasheet: str = ""
    function_requirements: list[str] = Field(default_factory=list)
    board: PeripheralBoardConfig = Field(default_factory=PeripheralBoardConfig)


class TftpDeployConfig(BaseModel):
    directory: str
    zimage_name: str = "zImage"
    dtb_name: str = "board.dtb"


class NfsDeployConfig(BaseModel):
    rootfs: str
    ko_path: str = "root/drivers"
    app_path: str = "root/tests"


class SerialDeployConfig(BaseModel):
    port: str = "/dev/ttyUSB0"
    baudrate: int = 115200


class DeployConfig(BaseModel):
    mode: str = "tftp_nfs_serial"
    tftp: TftpDeployConfig | None = None
    nfs: NfsDeployConfig | None = None
    serial: SerialDeployConfig = Field(default_factory=SerialDeployConfig)


class BuildConfig(BaseModel):
    max_fix_rounds: int = 10
    max_test_fix_rounds: int = 2
    auto_apply_patches: bool = True
    auto_fix_test: bool = True
    targets: list[str] = Field(default_factory=lambda: ["module", "dtc", "zimage", "dtb"])


class ProjectMeta(BaseModel):
    name: str
    output_dir: str = "output"
    api_url: str = "http://localhost:8000"


class BoardConfig(BaseModel):
    dts: str = ""


class EldaConfig(BaseModel):
    project: ProjectMeta
    target: TargetConfig
    model: ModelConfig = Field(default_factory=ModelConfig)
    milvus: MilvusConfig = Field(default_factory=MilvusConfig)
    peripherals: list[PeripheralConfig] = Field(default_factory=list)
    board: BoardConfig = Field(default_factory=BoardConfig)
    deploy: DeployConfig = Field(default_factory=DeployConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)

    @classmethod
    def load(cls, path: Path | str) -> EldaConfig:
        path = Path(path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(_expand_env(raw))

    def save(self, path: Path | str) -> None:
        path = Path(path)
        data = self.model_dump(mode="json")
        path.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    @property
    def kernel_source_path(self) -> Path:
        return Path(self.target.kernel_source)

    def enabled_peripherals(self) -> list[PeripheralConfig]:
        return [p for p in self.peripherals if p.enabled]


def _expand_env(obj: Any) -> Any:
    import os
    import re

    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, str):
        pattern = re.compile(r"\$\{([^}]+)\}")

        def repl(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), m.group(0))

        return pattern.sub(repl, obj)
    return obj


def find_config(start: Path | None = None) -> Path:
    cur = (start or Path.cwd()).resolve()
    for parent in [cur, *cur.parents]:
        candidate = parent / "elda.yaml"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("elda.yaml not found; run from project dir or use elda init")


def load_project_config(start: Path | None = None) -> tuple[EldaConfig, Path]:
    cfg_path = find_config(start)
    return EldaConfig.load(cfg_path), cfg_path.parent

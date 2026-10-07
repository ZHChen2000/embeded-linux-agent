"""Local Tool Executor — runs MCP-style tools on Ubuntu VM."""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rich.console import Console

from elda.config import EldaConfig
from elda.executor.ws_client import ExecutorWebSocketClient

console = Console()

ToolHandler = Callable[[EldaConfig, Path, dict[str, Any]], dict[str, Any]]


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def log(self) -> str:
        return (self.stdout or "") + (self.stderr or "")

    @property
    def success(self) -> bool:
        return self.returncode == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "returncode": self.returncode,
            "log": self.log,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, name: str) -> Callable[[ToolHandler], ToolHandler]:
        def decorator(fn: ToolHandler) -> ToolHandler:
            self._handlers[name] = fn
            return fn

        return decorator

    def execute(
        self,
        name: str,
        config: EldaConfig,
        project_root: Path,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if name not in self._handlers:
            raise KeyError(f"Unknown tool: {name}")
        return self._handlers[name](config, project_root, args)


registry = ToolRegistry()


@registry.register("fs.read")
def fs_read(_cfg: EldaConfig, root: Path, args: dict[str, Any]) -> dict[str, Any]:
    rel = args["path"]
    path = _safe_path(root, rel)
    return {"content": path.read_text(encoding="utf-8", errors="replace")}


@registry.register("fs.write")
def fs_write(_cfg: EldaConfig, root: Path, args: dict[str, Any]) -> dict[str, Any]:
    rel = args["path"]
    path = _safe_path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"], encoding="utf-8")
    return {"written": str(path)}


@registry.register("git.status")
def git_status(cfg: EldaConfig, _root: Path, _args: dict[str, Any]) -> dict[str, Any]:
    kernel = cfg.kernel_source_path
    out = _run(["git", "status", "--porcelain"], cwd=kernel)
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=kernel).strip()
    return {"branch": branch, "porcelain": out, "dirty": bool(out.strip())}


@registry.register("git.branch")
def git_branch(cfg: EldaConfig, _root: Path, args: dict[str, Any]) -> dict[str, Any]:
    kernel = cfg.kernel_source_path
    branch = args.get("branch", cfg.target.git_branch)
    _run(["git", "checkout", "-B", branch], cwd=kernel)
    return {"branch": branch}


@registry.register("git.apply_patch")
def git_apply_patch(cfg: EldaConfig, root: Path, args: dict[str, Any]) -> dict[str, Any]:
    diff = args["unified_diff"]
    patch_dir = root / "output" / "patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_id = args.get("id", uuid.uuid4().hex[:8])
    patch_file = patch_dir / f"{patch_id}.patch"
    patch_file.write_text(diff, encoding="utf-8")
    kernel = cfg.kernel_source_path
    log_path = root / "output" / "logs" / "build_fix_log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run(["git", "apply", "--check", str(patch_file)], cwd=kernel)
        _run(["git", "apply", "--whitespace=fix", str(patch_file)], cwd=kernel)
    except RuntimeError as exc:
        _run(["git", "apply", "-R", "--whitespace=fix", str(patch_file)], cwd=kernel, check=False)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## FAILED apply {patch_id}\n{exc}\n")
        raise
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"\n## Applied {patch_id}\n{args.get('rationale', '')}\n")
    return {"applied": str(patch_file), "id": patch_id}


@registry.register("kernel.rg_search")
def kernel_rg_search(cfg: EldaConfig, _root: Path, args: dict[str, Any]) -> dict[str, Any]:
    pattern = args["pattern"]
    path = args.get("path", "drivers")
    kernel = cfg.kernel_source_path
    try:
        out = _run(
            ["rg", "--json", "-m", str(args.get("limit", 20)), pattern, path],
            cwd=kernel,
        )
        return {"matches": out}
    except RuntimeError:
        return {"matches": ""}


@registry.register("build.make_module")
def build_make_module(cfg: EldaConfig, _root: Path, args: dict[str, Any]) -> dict[str, Any]:
    kernel = cfg.kernel_source_path
    module_path = args.get("module_path", "drivers")
    env = {"ARCH": cfg.target.arch, "CROSS_COMPILE": cfg.target.cross_compile}
    cmd = ["make", f"M={module_path}", "modules"]
    result = _run_result(cmd, cwd=kernel, env=env)
    out = result.as_dict()
    out["module_path"] = module_path
    return out


@registry.register("build.make_zimage")
def build_make_zimage(cfg: EldaConfig, _root: Path, args: dict[str, Any]) -> dict[str, Any]:
    kernel = cfg.kernel_source_path
    env = {"ARCH": cfg.target.arch, "CROSS_COMPILE": cfg.target.cross_compile}
    cmd = ["make", "zImage", "dtbs", "-j", str(args.get("jobs", 4))]
    result = _run_result(cmd, cwd=kernel, env=env)
    return result.as_dict()


@registry.register("build.parse_log")
def build_parse_log(_cfg: EldaConfig, _root: Path, args: dict[str, Any]) -> dict[str, Any]:
    from elda.build.log_parser import parse_build_log

    return parse_build_log(args.get("log", ""))


@registry.register("build.dtc")
def build_dtc(cfg: EldaConfig, _root: Path, args: dict[str, Any]) -> dict[str, Any]:
    dts = Path(args["dts_path"])
    if not dts.is_absolute():
        dts = cfg.kernel_source_path / dts
    out = dts.with_suffix(".dtb")
    result = _run_result(["dtc", "-I", "dts", "-O", "dtb", "-o", str(out), str(dts)])
    data = result.as_dict()
    data["dtb"] = str(out)
    return data


@registry.register("deploy.tftp_copy")
def deploy_tftp_copy(cfg: EldaConfig, _root: Path, args: dict[str, Any]) -> dict[str, Any]:
    deploy = cfg.deploy
    if not deploy.tftp:
        raise ValueError("deploy.tftp not configured")
    kernel = cfg.kernel_source_path
    arch_boot = kernel / "arch" / cfg.target.arch / "boot"
    zimage_src = arch_boot / "zImage"
    dtb_src = args.get("dtb_src") or arch_boot / "dts" / deploy.tftp.dtb_name
    tftp_dir = Path(deploy.tftp.directory)
    tftp_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    if zimage_src.exists():
        dst = tftp_dir / deploy.tftp.zimage_name
        shutil.copy2(zimage_src, dst)
        copied.append(str(dst))
    if Path(dtb_src).exists():
        dst = tftp_dir / deploy.tftp.dtb_name
        shutil.copy2(dtb_src, dst)
        copied.append(str(dst))
    return {"copied": copied}


@registry.register("deploy.nfs_copy")
def deploy_nfs_copy(cfg: EldaConfig, root: Path, args: dict[str, Any]) -> dict[str, Any]:
    deploy = cfg.deploy
    if not deploy.nfs:
        raise ValueError("deploy.nfs not configured")
    nfs_root = Path(deploy.nfs.rootfs)
    copied = []
    for src in args.get("files", []):
        src_path = Path(src)
        if not src_path.is_absolute():
            src_path = root / src_path
        rel = args.get("dest_rel", Path(src).name)
        dest = nfs_root / deploy.nfs.ko_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest)
        copied.append(str(dest))
    return {"copied": copied}


@registry.register("pdf.mineru_extract")
def pdf_mineru_extract(_cfg: EldaConfig, root: Path, args: dict[str, Any]) -> dict[str, Any]:
    from elda.ingest.pdf_extract import parse_pdf

    pdf_arg = args["pdf_path"]
    pdf_path = Path(pdf_arg)
    if not pdf_path.is_absolute():
        pdf_path = (root / pdf_path).resolve()
    out_dir = root / "workspace" / "pdf_extract" / pdf_path.stem
    return parse_pdf(pdf_path, out_dir)


@registry.register("milvus.index_kernel")
def milvus_index_kernel(cfg: EldaConfig, root: Path, args: dict[str, Any]) -> dict[str, Any]:
    from elda.rag.milvus_client import MilvusIndexer

    kernel = cfg.kernel_source_path
    indexer = MilvusIndexer(cfg)
    paths = args.get(
        "paths",
        [
            "drivers/iio",
            "drivers/spi",
            "drivers/input",
            "drivers/hwmon",
            "Documentation/devicetree/bindings",
        ],
    )
    stats_drivers = indexer.index_paths("kernel_drivers", kernel, paths[:4])
    stats_bindings = indexer.index_paths("dt_bindings", kernel, paths[4:])
    return {"kernel_drivers": stats_drivers, "dt_bindings": stats_bindings}


@registry.register("kernel.vector_search")
def kernel_vector_search(cfg: EldaConfig, _root: Path, args: dict[str, Any]) -> dict[str, Any]:
    from elda.rag.milvus_client import MilvusIndexer

    indexer = MilvusIndexer(cfg)
    query = args["query"]
    collection = args.get("collection", "kernel_drivers")
    top_k = int(args.get("top_k", 8))
    hits = indexer.search(collection, query, top_k=top_k)
    return {"hits": hits}


@registry.register("milvus.index_vendor")
def milvus_index_vendor(cfg: EldaConfig, _root: Path, args: dict[str, Any]) -> dict[str, Any]:
    from elda.rag.milvus_client import COLLECTIONS, MilvusIndexer, chunk_text

    source = Path(args["source_path"])
    if not source.is_file():
        raise FileNotFoundError(source)
    indexer = MilvusIndexer(cfg)
    text = source.read_text(encoding="utf-8", errors="replace")
    chunks = chunk_text(text)
    col = indexer._ensure_collection(COLLECTIONS["vendor_drivers"])
    vectors = indexer.embedder.embed_sync(chunks)
    import hashlib

    ids, doc_ids, paths, indices, texts = [], [], [], [], []
    for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
        cid = hashlib.sha256(f"{source.name}:{i}".encode()).hexdigest()[:32]
        ids.append(cid)
        doc_ids.append(source.name)
        paths.append(str(source))
        indices.append(i)
        texts.append(chunk[:8000])
    col.insert([ids, doc_ids, paths, indices, texts, vectors])
    col.flush()
    return {"file": str(source), "chunks": len(chunks)}


@registry.register("deploy.manual_checklist")
def deploy_manual_checklist(cfg: EldaConfig, root: Path, _args: dict[str, Any]) -> dict[str, Any]:
    deploy = cfg.deploy
    lines = [
        "# Manual deploy checklist (TFTP/NFS/Serial)",
        "",
        "1. Reset board and enter U-Boot",
        "2. Load kernel from TFTP:",
        f"   tftp ${'{'}loadaddr{'}'} {deploy.tftp.zimage_name if deploy.tftp else 'zImage'}",
        f"   tftp ${'{'}fdt_addr{'}'} {deploy.tftp.dtb_name if deploy.tftp else 'board.dtb'}",
        "3. bootz ${loadaddr} - ${fdt_addr}",
        "4. On Linux shell:",
        "   insmod /path/to/driver.ko",
        "   run test app on NFS rootfs",
        "5. Capture dmesg: elda test --log dmesg.txt",
        "",
    ]
    path = root / "reports" / "deploy_checklist.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"checklist": str(path)}


@registry.register("board.conflict_check")
def board_conflict_check(cfg: EldaConfig, root: Path, _args: dict[str, Any]) -> dict[str, Any]:
    from elda.executor.board_conflicts import check_board_conflicts
    from elda.executor.dts_validate import validate_board_dts

    report = check_board_conflicts(cfg)
    report_path = root / "reports" / "board_conflict_log.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.to_markdown(), encoding="utf-8")
    data = report.model_dump()
    data["has_hard_errors"] = report.has_hard_errors
    if cfg.board.dts:
        dts_path = Path(cfg.board.dts)
        if not dts_path.is_absolute():
            dts_path = (root / dts_path).resolve()
            if not dts_path.is_file():
                dts_path = cfg.kernel_source_path / cfg.board.dts
        dts_report = validate_board_dts(cfg, dts_path)
        dts_path_out = root / "reports" / "dts_validation.md"
        dts_path_out.write_text(dts_report.to_markdown(), encoding="utf-8")
        data["dts_validation"] = dts_report.model_dump()
        if dts_report.has_errors:
            data["has_hard_errors"] = True
    return data


@registry.register("dts.validate")
def dts_validate_tool(cfg: EldaConfig, root: Path, args: dict[str, Any]) -> dict[str, Any]:
    from elda.executor.dts_validate import validate_board_dts

    rel = args.get("dts_path") or cfg.board.dts
    if not rel:
        raise ValueError("board.dts not configured")
    dts_path = Path(rel)
    if not dts_path.is_absolute():
        dts_path = (root / dts_path).resolve()
        if not dts_path.is_file():
            dts_path = cfg.kernel_source_path / rel
    report = validate_board_dts(cfg, dts_path)
    out = root / "reports" / "dts_validation.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.to_markdown(), encoding="utf-8")
    data = report.model_dump()
    data["has_errors"] = report.has_errors
    return data


@registry.register("serial.capture")
def serial_capture(cfg: EldaConfig, _root: Path, args: dict[str, Any]) -> dict[str, Any]:
    port = args.get("port") or cfg.deploy.serial.port
    baud = int(args.get("baudrate") or cfg.deploy.serial.baudrate)
    seconds = int(args.get("seconds", 10))
    script = (
        f"stty -F {port} {baud} raw -echo cs8 -cstopb -parenb 2>/dev/null; "
        f"timeout {seconds} cat {port} 2>/dev/null || true"
    )
    result = _run_result(["bash", "-lc", script])
    return {
        "log": result.stdout,
        "success": bool(result.stdout.strip()),
        "returncode": result.returncode,
    }


@registry.register("serial.extract_log")
def serial_extract_log(_cfg: EldaConfig, _root: Path, args: dict[str, Any]) -> dict[str, Any]:
    from elda.executor.serial_log import split_serial_capture

    text = args.get("text", "")
    if not text and args.get("path"):
        text = Path(args["path"]).read_text(encoding="utf-8", errors="replace")
    parts = split_serial_capture(text)
    return parts


@registry.register("deploy.nfs_modules")
def deploy_nfs_modules(cfg: EldaConfig, root: Path, args: dict[str, Any]) -> dict[str, Any]:
    if not cfg.deploy.nfs:
        return {"copied": []}
    kernel = cfg.kernel_source_path
    nfs_root = Path(cfg.deploy.nfs.rootfs)
    module_paths = args.get("module_paths") or []
    copied: list[str] = []
    seen: set[str] = set()
    for mp in module_paths:
        base = kernel / mp
        if not base.is_dir():
            continue
        for ko in base.rglob("*.ko"):
            if ko.name in seen:
                continue
            seen.add(ko.name)
            dest = nfs_root / cfg.deploy.nfs.ko_path / ko.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ko, dest)
            copied.append(str(dest))
    tests_src = root / "output" / "tests"
    if tests_src.is_dir():
        for binf in tests_src.rglob("*"):
            if binf.is_file() and binf.stat().st_mode & 0o111:
                dest = nfs_root / cfg.deploy.nfs.app_path / binf.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(binf, dest)
                copied.append(str(dest))
    return {"copied": copied}


def _safe_path(root: Path, rel: str) -> Path:
    path = (root / rel).resolve()
    if not str(path).startswith(str(root.resolve())):
        raise PermissionError(f"Path escapes project root: {rel}")
    return path


def _run_result(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    import os

    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
    )
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _run(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> str:
    result = _run_result(cmd, cwd=cwd, env=env)
    if check and not result.success:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(cmd)}\n{result.log}")
    return result.log


class ExecutorRunner:
    def __init__(self, config: EldaConfig, project_root: Path) -> None:
        self.config = config
        self.project_root = project_root
        self.executor_id = f"exec-{uuid.uuid4().hex[:12]}"

    def start(self) -> None:
        console.print(f"[green]Executor starting (WebSocket):[/green] {self.executor_id}")
        console.print("[dim]Waiting for tool calls… Ctrl+C to stop[/dim]")

        def _on_call(call: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, str | None]:
            call_id = call["id"]
            tool = call["tool"]
            args = call.get("args", {})
            console.print(f"[cyan]Tool call:[/cyan] {tool} ({call_id})")
            try:
                result = registry.execute(tool, self.config, self.project_root, args)
                console.print(f"[green]OK[/green] {json.dumps(result, ensure_ascii=False)[:200]}")
                return True, result, None
            except Exception as exc:
                console.print(f"[red]FAIL[/red] {exc}")
                return False, {}, str(exc)

        client = ExecutorWebSocketClient(
            self.config.project.api_url,
            self.executor_id,
            str(self.project_root),
        )
        client.run_forever(_on_call)

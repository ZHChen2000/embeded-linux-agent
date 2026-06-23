"""ELDA CLI entrypoint."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from elda.client.api_client import EldaApiClient
from elda.config import EldaConfig, load_project_config
from elda.driver_paths import resolve_driver_module_paths
from elda.executor.board_conflicts import check_board_conflicts
from elda.executor.runner import ExecutorRunner

app = typer.Typer(
    name="elda",
    help="Embedded Linux Driver Agent — CLI",
    no_args_is_help=True,
)
console = Console()

executor_app = typer.Typer(help="Local Tool Executor")
app.add_typer(executor_app, name="executor")

verify_app = typer.Typer(help="Verification gates")
app.add_typer(verify_app, name="verify")

board_app = typer.Typer(help="Board connection management")
app.add_typer(board_app, name="board")

generate_app = typer.Typer(help="Code generation")
app.add_typer(generate_app, name="generate")

WORKSPACE_FILES = (
    "register_map.json",
    "init_sequence.yaml",
    "pin_requirements.yaml",
)


def _api(cfg: EldaConfig) -> EldaApiClient:
    return EldaApiClient(cfg.project.api_url)


def _task_payload(cfg: EldaConfig, root: Path, extra: dict | None = None) -> dict:
    import os

    from elda.secrets_loader import load_api_secrets, merge_model_keys

    secrets = load_api_secrets()
    bailian = (
        cfg.model.bailian_api_key
        or os.environ.get("BAILIAN_API_KEY", "")
        or secrets.bailian.api_key
    )
    deepseek = (
        cfg.model.deepseek_api_key
        or os.environ.get("DEEPSEEK_API_KEY", "")
        or secrets.deepseek.api_key
    )
    payload = {
        "project_root": str(root),
        "model_keys": {"bailian": bailian, "deepseek": deepseek},
        "code_model": cfg.model.code_model or secrets.bailian.code_model,
        "bailian_reasoning_model": secrets.bailian.reasoning_model,
        "reasoning_model": cfg.model.reasoning_model or secrets.deepseek.reasoning_model,
        "embedding_model": cfg.model.embedding_model or secrets.bailian.embedding_model,
        "bailian_base_url": secrets.bailian.base_url,
        "deepseek_base_url": secrets.deepseek.base_url,
        "board_dts": cfg.board.dts,
        "max_fix_rounds": cfg.build.max_fix_rounds,
        "driver_module_paths": resolve_driver_module_paths(cfg, root),
        "peripherals_enabled": [
            {
                "id": p.id,
                "name": p.name,
                "bus": p.bus,
                "framework": p.driver_framework,
                "driver_module_path": p.driver_module_path,
            }
            for p in cfg.enabled_peripherals()
        ],
        "peripherals_datasheets": [
            {"id": p.id, "path": str((root / p.datasheet).resolve())}
            for p in cfg.enabled_peripherals()
            if p.datasheet
        ],
    }
    if extra:
        payload.update(extra)
    return merge_model_keys(payload)


def _submit_and_wait(cfg: EldaConfig, task_type: str, payload: dict | None = None) -> dict:
    client = _api(cfg)
    project_id = cfg.project.name
    task = client.submit_task(task_type, project_id, payload or {})
    console.print(f"[blue]Task submitted:[/blue] {task['id']} ({task_type})")
    return client.wait_task(task["id"])


def _require_verified(root: Path) -> None:
    if not (root / "workspace" / ".verified").is_file():
        console.print("[red]Workspace not verified — run:[/red] elda verify workspace")
        raise typer.Exit(1)


@app.command()
def init(
    name: str = typer.Argument(..., help="Project name"),
    directory: Optional[Path] = typer.Option(None, "--dir", "-d", help="Parent directory"),
) -> None:
    """Create a new ELDA project."""
    parent = directory or Path.cwd()
    project_dir = parent / name
    if project_dir.exists():
        console.print(f"[red]Directory already exists:[/red] {project_dir}")
        raise typer.Exit(1)

    example = Path(__file__).resolve().parents[3] / "examples" / "elda.yaml.example"
    if not example.exists():
        example = Path.cwd().parent / "examples" / "elda.yaml.example"

    dirs = [
        "docs",
        "board",
        "output/patches",
        "output/logs",
        "reports",
        "workspace/soc",
        "workspace/peripherals",
    ]
    for d in dirs:
        (project_dir / d).mkdir(parents=True)

    if example.is_file():
        content = example.read_text(encoding="utf-8").replace("my-board-project", name)
        (project_dir / "elda.yaml").write_text(content, encoding="utf-8")
    else:
        cfg = EldaConfig(
            project={"name": name, "output_dir": "output", "api_url": "http://localhost:8000"},
            target={
                "soc": "soc",
                "arch": "arm",
                "kernel_version": "4.1.15",
                "kernel_source": "/path/to/linux-bsp",
                "git_branch": f"elda/{name}",
                "cross_compile": "arm-linux-gnueabihf-",
            },
        )
        cfg.save(project_dir / "elda.yaml")

    (project_dir / "workspace" / ".verified").unlink(missing_ok=True)
    console.print(f"[green]Created project:[/green] {project_dir}")
    console.print("Edit elda.yaml, then: elda executor start")


@app.command()
def ingest(
    soc_datasheet: Optional[Path] = typer.Option(None, "--soc-datasheet"),
) -> None:
    """Parse datasheets via PDF extract (PyMuPDF / pdftotext / optional MinerU)."""
    cfg, root = load_project_config()
    enabled = cfg.enabled_peripherals()
    if not enabled or not any(p.datasheet for p in enabled):
        console.print("[red]No peripheral datasheets in elda.yaml[/red]")
        raise typer.Exit(1)
    extra: dict = {}
    if soc_datasheet:
        extra["soc_datasheet"] = str(soc_datasheet.resolve())
    result = _submit_and_wait(cfg, "ingest", _task_payload(cfg, root, extra))
    if result.get("status") == "waiting_verify":
        console.print("[yellow]Workspace drafts ready — run:[/yellow] elda verify workspace")
    else:
        console.print(f"[green]Ingest done:[/green] {result.get('message', '')}")


@board_app.command("add")
def board_add() -> None:
    """Validate board config and run conflict detection."""
    cfg, root = load_project_config()
    report = check_board_conflicts(cfg)
    path = root / "reports" / "board_conflict_log.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_markdown(), encoding="utf-8")
    console.print(f"[green]Board conflict report:[/green] {path}")
    errors = [c for c in report.conflicts if c.severity == "error"]
    if errors:
        console.print("[red]Hard board conflicts — fix elda.yaml before continue[/red]")
        raise typer.Exit(1)
    _submit_and_wait(cfg, "board_validate", _task_payload(cfg, root))


@app.command()
def plan(
    target: Optional[str] = typer.Option(None, "--target"),
    framework: str = typer.Option("auto", "--framework"),
) -> None:
    """Generate driver plan via PlannerAgent."""
    cfg, root = load_project_config()
    enabled = cfg.enabled_peripherals()
    target_name = target or (enabled[0].name if enabled else "peripheral")
    extra = {"target": target_name, "framework": framework}
    _submit_and_wait(cfg, "plan", _task_payload(cfg, root, extra))
    console.print("[green]Plan written to reports/driver_plan.md[/green]")


@generate_app.command("driver")
def generate_driver() -> None:
    """Generate in-tree C driver only."""
    cfg, root = load_project_config()
    _require_verified(root)
    _submit_and_wait(cfg, "generate_driver", _task_payload(cfg, root))


@generate_app.command("dts")
def generate_dts() -> None:
    """Generate device tree patches only."""
    cfg, root = load_project_config()
    _require_verified(root)
    _submit_and_wait(cfg, "generate_dts", _task_payload(cfg, root))


@generate_app.command("kbuild")
def generate_kbuild() -> None:
    """Generate Kbuild fragments and test app only."""
    cfg, root = load_project_config()
    _require_verified(root)
    _submit_and_wait(cfg, "generate_kbuild", _task_payload(cfg, root))


@generate_app.command("all")
def generate_all() -> None:
    """Generate driver, DTS, and Kbuild in one shot."""
    cfg, root = load_project_config()
    _require_verified(root)
    _submit_and_wait(cfg, "generate_all", _task_payload(cfg, root))


@app.command()
def build(
    fix: bool = typer.Option(True, "--fix/--no-fix", help="Auto-fix compile errors"),
) -> None:
    """Build module, dtc, zImage, and dtb."""
    cfg, root = load_project_config()
    _submit_and_wait(
        cfg,
        "build",
        _task_payload(
            cfg,
            root,
            {"fix": fix, "max_fix_rounds": cfg.build.max_fix_rounds},
        ),
    )


@app.command()
def deploy() -> None:
    """Deploy zImage/dtb to TFTP; write manual checklist."""
    cfg, root = load_project_config()
    _submit_and_wait(cfg, "deploy", _task_payload(cfg, root))


@app.command()
def test(
    log_file: Optional[Path] = typer.Option(None, "--log", help="dmesg or serial log file"),
    app_output: Optional[Path] = typer.Option(None, "--app-log", help="User-space test app output"),
) -> None:
    """Analyze board test logs."""
    cfg, root = load_project_config()
    extra: dict = {}
    if log_file:
        extra["log_content"] = log_file.read_text(encoding="utf-8", errors="replace")
    if app_output:
        extra["app_output"] = app_output.read_text(encoding="utf-8", errors="replace")
    _submit_and_wait(cfg, "test", _task_payload(cfg, root, extra))


@app.command()
def report() -> None:
    """Generate final HTML/Markdown report."""
    cfg, root = load_project_config()
    _submit_and_wait(cfg, "report", _task_payload(cfg, root))
    console.print("[green]Reports in reports/[/green]")


@app.command()
def chat(
    message: Optional[str] = typer.Argument(None, help="Message; omit for interactive mode"),
) -> None:
    """Read-only Q&A about hardware, drivers, and project state."""
    cfg, root = load_project_config()
    import os

    from elda.secrets_loader import load_api_secrets, merge_model_keys

    secrets = load_api_secrets()
    model_body = merge_model_keys(
        {
            "model_keys": {
                "bailian": cfg.model.bailian_api_key or os.environ.get("BAILIAN_API_KEY", "") or secrets.bailian.api_key,
                "deepseek": cfg.model.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY", "") or secrets.deepseek.api_key,
            },
            "project_root": str(root),
        }
    )
    client = _api(cfg)
    history: list[dict[str, str]] = []

    if message:
        resp = client.chat(cfg.project.name, message, history, model_body)
        console.print(resp.get("reply", ""))
        return
    console.print("[dim]Interactive chat (read-only) — empty line to exit[/dim]")
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user:
            break
        resp = client.chat(cfg.project.name, user, history, model_body)
        reply = resp.get("reply", "")
        history.append({"role": "user", "content": user})
        history.append({"role": "assistant", "content": reply})
        console.print(reply)


@verify_app.command("workspace")
def verify_workspace() -> None:
    """Confirm hardware workspace files have been human-reviewed."""
    cfg, root = load_project_config()
    missing: list[str] = []

    soc_dir = root / "workspace" / "soc"
    if soc_dir.is_dir() and any(soc_dir.iterdir()):
        for f in WORKSPACE_FILES:
            if not (soc_dir / f).is_file():
                missing.append(f"workspace/soc/{f}")

    periph_root = root / "workspace" / "peripherals"
    enabled = cfg.enabled_peripherals()
    if not periph_root.is_dir():
        missing.append("workspace/peripherals/ (directory)")
    else:
        for p in enabled:
            pdir = periph_root / p.id
            for f in WORKSPACE_FILES:
                if not (pdir / f).is_file():
                    missing.append(f"workspace/peripherals/{p.id}/{f}")

    if missing:
        console.print("[red]Missing workspace files:[/red]")
        for m in missing:
            console.print(f"  - {m}")
        console.print("Run elda ingest, review files, then retry.")
        raise typer.Exit(1)

    (root / "workspace" / ".verified").write_text("ok\n", encoding="utf-8")
    console.print("[green]Workspace verified — you may run elda generate[/green]")


@executor_app.command("start")
def executor_start() -> None:
    """Start local Tool Executor (WebSocket to cloud API)."""
    cfg, root = load_project_config()
    try:
        health = _api(cfg).health()
        console.print(f"[green]API OK[/green] {health}")
    except Exception as exc:
        console.print(f"[yellow]API not reachable ({exc}) — start docker compose first[/yellow]")
    runner = ExecutorRunner(cfg, root)
    runner.start()


@app.command()
def doctor() -> None:
    """Environment self-check."""
    from elda.doctor import run_doctor

    for r in run_doctor():
        mark = "[green]OK[/green]" if r.ok else "[red]FAIL[/red]"
        console.print(f"{mark} {r.name}: {r.detail}")


@app.command("import-vendor")
def import_vendor(
    source: Path = typer.Argument(..., help="Vendor driver source file (.c)"),
) -> None:
    """Index vendor driver into Milvus for RAG."""
    cfg, root = load_project_config()
    _submit_and_wait(
        cfg,
        "import_vendor",
        _task_payload(cfg, root, {"source_path": str(source.resolve())}),
    )
    console.print("[green]Vendor driver indexed[/green]")


@app.command()
def index(
    kernel: bool = typer.Option(False, "--kernel", help="Index kernel source into Milvus"),
) -> None:
    """Build vector indexes for RAG."""
    cfg, root = load_project_config()
    if kernel:
        _submit_and_wait(
            cfg,
            "index_kernel",
            _task_payload(cfg, root, {"kernel_source": cfg.target.kernel_source}),
        )
        console.print("[green]Kernel index task submitted[/green]")


@app.command()
def version() -> None:
    """Show ELDA version."""
    from elda import __version__

    console.print(f"elda {__version__}")


if __name__ == "__main__":
    app()

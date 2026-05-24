#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from core.config import GlobalConfig, VaultConfig
from core.vault import init_vault, vault_stats

console = Console()


@click.group()
def cli():
    """llm-wiki — LLM-powered Obsidian vault manager."""


# ---------------------------------------------------------------------------
# Vault management
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("path", default=".", type=click.Path())
@click.option("--name", "-n", default=None, help="Vault name (defaults to folder name)")
def init(path: str, name: str | None):
    """Initialize LLM-wiki structure in PATH (default: current directory)."""
    vault_path = Path(path).resolve()
    vault_name = name or vault_path.name

    config = GlobalConfig.load()
    if vault_name in config.vaults:
        console.print(
            f"[yellow]Vault '{vault_name}' already registered at {config.vaults[vault_name]}[/yellow]"
        )
        return

    init_vault(vault_path, vault_name)
    config.register_vault(vault_name, vault_path)

    console.print(f"\n[green]✓[/green] Initialized vault [bold]{vault_name}[/bold]")
    console.print(f"  Path:  {vault_path}")
    console.print("  [dim]raw/[/dim]         drop source files here for auto-ingest")
    console.print("  [dim]wiki/[/dim]        open this folder in Obsidian as a vault")
    console.print("  [dim].llm-wiki/[/dim]  internal index (wiki.db, gitignored)\n")


@cli.command("list")
def list_vaults():
    """List all registered vaults."""
    config = GlobalConfig.load()
    if not config.vaults:
        console.print("[dim]No vaults registered. Run `llm-wiki init <path>` to add one.[/dim]")
        return

    table = Table(title="Registered Vaults", show_lines=True)
    table.add_column("Name", style="bold cyan")
    table.add_column("Path")
    table.add_column("Default", justify="center")
    table.add_column("Model")

    for vname, vpath in config.vaults.items():
        vcfg = VaultConfig.load(Path(vpath))
        effective_model = vcfg.model or config.model
        is_default = "[green]✓[/green]" if vname == config.default_vault else ""
        table.add_row(vname, vpath, is_default, effective_model)

    console.print(table)


@cli.command()
@click.option("--vault", "-v", default=None, help="Vault name (uses default if unset)")
def status(vault: str | None):
    """Show stats for a vault."""
    config = GlobalConfig.load()
    try:
        vname, vpath = config.resolve_vault(vault)
    except (ValueError, KeyError) as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1) from None

    stats = vault_stats(vpath)
    console.print(f"\n[bold cyan]{vname}[/bold cyan]  {vpath}")
    console.print(f"  Total pages : {stats['total_pages']}")
    console.print(f"  Raw queued  : {stats['raw_queued']}")
    for cat, count in stats["categories"].items():
        console.print(f"  {cat:<12}: {count} pages")
    console.print()


@cli.command("use")
@click.argument("vault_name")
def use(vault_name: str):
    """Set the default vault."""
    config = GlobalConfig.load()
    if vault_name not in config.vaults:
        console.print(f"[red]Vault '{vault_name}' not found. Run `llm-wiki list`.[/red]")
        raise SystemExit(1) from None
    config.default_vault = vault_name
    config.save()
    console.print(f"[green]✓[/green] Default vault set to [bold]{vault_name}[/bold]")


@cli.command()
@click.argument("vault_name")
def unregister(vault_name: str):
    """Remove a vault from the registry (files on disk are left untouched)."""
    config = GlobalConfig.load()
    if vault_name not in config.vaults:
        console.print(
            f"[red]Vault '{vault_name}' is not registered. Run `llm-wiki list` to see vaults.[/red]"
        )
        raise SystemExit(1) from None
    del config.vaults[vault_name]
    if config.default_vault == vault_name:
        config.default_vault = next(iter(config.vaults), None)
    config.save()
    console.print(f"[green]✓[/green] Vault [bold]{vault_name}[/bold] unregistered.")
    if config.default_vault:
        console.print(f"  Default is now [bold]{config.default_vault}[/bold]")
    else:
        console.print("  [dim]No default vault set. Run `llm-wiki use <name>` to set one.[/dim]")


# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------


@cli.command("set-model")
@click.argument("model")
@click.option("--vault", "-v", default=None, help="Apply to a specific vault only")
def set_model(model: str, vault: str | None):
    """Set the LiteLLM model string (e.g. claude-sonnet-4-6, gpt-4o, ollama/llama3)."""
    config = GlobalConfig.load()
    if vault:
        try:
            _, vpath = config.resolve_vault(vault)
        except (ValueError, KeyError) as e:
            console.print(f"[red]{e}[/red]")
            raise SystemExit(1) from None
        vcfg = VaultConfig.load(vpath)
        vcfg.model = model
        vcfg.save(vpath)
        console.print(
            f"[green]✓[/green] Model for vault [bold]{vault}[/bold] → [bold]{model}[/bold]"
        )
    else:
        config.model = model
        config.save()
        console.print(f"[green]✓[/green] Global model → [bold]{model}[/bold]")


# ---------------------------------------------------------------------------
# LLM operations
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("source")
@click.option("--vault", "-v", default=None, help="Vault name (uses default if unset)")
@click.option("--dry-run", is_flag=True, help="Show what would be written without writing")
def ingest(source: str, vault: str | None, dry_run: bool):
    """Ingest a file or URL into the wiki."""
    from core.ingest import ingest_source

    config = GlobalConfig.load()
    try:
        vname, vpath = config.resolve_vault(vault)
    except (ValueError, KeyError) as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1) from None

    console.print(f"Ingesting [bold]{source}[/bold] into vault [bold]{vname}[/bold]...")
    try:
        result = ingest_source(vpath, source, vname, dry_run=dry_run)
    except Exception as e:
        console.print(f"[red]Ingest failed: {e}[/red]")
        raise SystemExit(1) from None

    written = result.get("pages_written", [])
    if dry_run:
        console.print("[yellow](dry-run — nothing written)[/yellow]")
        sp = result.get("source_page", {})
        console.print(f"Would create: {sp.get('file_path', '?')}")
        for u in result.get("page_updates", []):
            console.print(f"Would {u.get('action', 'update')}: {u.get('file_path', '?')}")
    else:
        console.print(f"[green]✓[/green] Wrote {len(written)} page(s):")
        for p in written:
            console.print(f"  {p}")


@cli.command()
@click.argument("question")
@click.option("--vault", "-v", default=None, help="Vault name")
@click.option("--save-as", default=None, help="Save answer as a wiki page at this path")
def query(question: str, vault: str | None, save_as: str | None):
    """Ask a question and get an answer grounded in wiki content."""
    from core.query import query_wiki

    config = GlobalConfig.load()
    try:
        vname, vpath = config.resolve_vault(vault)
    except (ValueError, KeyError) as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1) from None

    console.print(f"Querying vault [bold]{vname}[/bold]...\n")
    try:
        result = query_wiki(vpath, question, save_as=save_as)
    except Exception as e:
        console.print(f"[red]Query failed: {e}[/red]")
        raise SystemExit(1) from None

    console.print(result["answer"])
    if result["sources"]:
        console.print(f"\n[dim]Sources: {', '.join(result['sources'])}[/dim]")
    if result["saved_to"]:
        console.print(f"[green]✓[/green] Saved to {result['saved_to']}")


@cli.command()
@click.option("--vault", "-v", default=None, help="Vault name")
def lint(vault: str | None):
    """Run a lint pass: find orphans, broken links, and LLM-detected contradictions."""
    from core.lint import lint_vault

    config = GlobalConfig.load()
    try:
        vname, vpath = config.resolve_vault(vault)
    except (ValueError, KeyError) as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1) from None

    console.print(f"Linting vault [bold]{vname}[/bold]...")
    try:
        result = lint_vault(vpath)
    except Exception as e:
        console.print(f"[red]Lint failed: {e}[/red]")
        raise SystemExit(1) from None

    s = result["structural"]
    console.print(f"  Orphans      : {len(s['orphans'])}")
    console.print(f"  Broken links : {len(s['broken_links'])}")
    console.print(f"  No summary   : {len(s['missing_summaries'])}")
    console.print(f"\n[green]✓[/green] Report saved to: {result['saved_to']}")


@cli.command()
@click.option("--vault", "-v", default=None, help="Vault name")
def reconcile(vault: str | None):
    """Re-sync the search index with wiki files on disk."""
    from core.database import get_db
    from core.database import reconcile as do_reconcile

    config = GlobalConfig.load()
    try:
        vname, vpath = config.resolve_vault(vault)
    except (ValueError, KeyError) as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1) from None

    conn = get_db(vpath)
    try:
        stats = do_reconcile(conn, vpath / "wiki")
    finally:
        conn.close()
    console.print(f"[green]✓[/green] Reconciled [bold]{vname}[/bold]: {stats}")


@cli.command()
@click.option("--port", "-p", default=None, type=int, help="Port (default: from config, 8000)")
@click.option("--host", default="127.0.0.1")
def serve(port: int | None, host: str):
    """Start the llm-wiki web dashboard and vault watchers."""
    import subprocess
    import sys

    cmd = [sys.executable, "main_server.py", "--host", host]
    if port:
        cmd += ["--port", str(port)]
    console.print("Starting llm-wiki server… (Ctrl-C to stop)")
    subprocess.run(cmd)


if __name__ == "__main__":
    cli()

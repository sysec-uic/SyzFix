#!/usr/bin/env python3
"""
Syzbot Fixed Kernel Bug Dataset Builder

Collects fixed kernel bugs from syzbot/syzkaller and builds a dataset
containing crash reports, patch diffs, mailing list discussions, and
patch evolution history.

Usage:
    # Fetch and process 10 bugs (for testing)
    python main.py collect --limit 10

    # Resume full collection
    python main.py collect

    # Export to JSONL
    python main.py export --format jsonl

    # Export to HuggingFace Dataset
    python main.py export --format huggingface

    # Show dataset statistics
    python main.py stats
"""

import asyncio
import logging
import sys

import click

from . import config


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.DATA_DIR / "pipeline.log"),
        ],
    )
    # Suppress noisy loggers
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
def cli(verbose):
    """Syzbot Fixed Kernel Bug Dataset Builder."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(verbose)


@cli.command()
@click.option("--limit", default=0, help="Max number of bugs to process (0=all)")
@click.option("--no-resume", is_flag=True, help="Start fresh, don't resume")
@click.option("--skip-patchwork", is_flag=True, help="Skip patchwork fallback")
def collect(limit, no_resume, skip_patchwork):
    """Collect and process bug data from all sources."""
    from .pipeline import run_pipeline

    click.echo(f"Starting data collection (limit={limit or 'all'}, resume={not no_resume})")
    asyncio.run(run_pipeline(
        limit=limit,
        resume=not no_resume,
        skip_patchwork=skip_patchwork,
    ))


@cli.command()
@click.option(
    "--format", "fmt",
    type=click.Choice(["jsonl", "huggingface"]),
    default="jsonl",
    help="Export format",
)
@click.option("--output", type=click.Path(), default=None, help="Output path")
def export(fmt, output):
    """Export processed data to dataset format."""
    from .export import export_jsonl, export_huggingface
    from pathlib import Path

    if fmt == "jsonl":
        out_path = Path(output) if output else None
        count = export_jsonl(out_path)
        click.echo(f"Exported {count} entries as JSONL")
    elif fmt == "huggingface":
        out_dir = Path(output) if output else None
        export_huggingface(out_dir)
        click.echo("Export complete")


@cli.command()
def stats():
    """Show dataset statistics."""
    from .export import print_dataset_stats
    print_dataset_stats()


@cli.command()
@click.argument("bug_id")
def inspect(bug_id):
    """Inspect a single processed bug entry."""
    import json
    from .storage import DataStore

    store = DataStore()
    data = store.load_processed(bug_id)
    if data:
        # Print summary, not full data
        click.echo(f"Bug ID: {data.get('bug_id')}")
        click.echo(f"Title: {data.get('title')}")
        click.echo(f"Status: {data.get('status')}")
        click.echo(f"Fix commits: {len(data.get('fix_commits', []))}")
        click.echo(f"Discussions: {len(data.get('discussions', []))}")
        click.echo(f"Crashes: {len(data.get('crashes', []))}")
        click.echo(f"Patch versions: {len(data.get('patch_versions', []))}")
        click.echo(f"Errors: {data.get('processing_errors', [])}")

        if click.confirm("Show full JSON?"):
            click.echo(json.dumps(data, indent=2, ensure_ascii=False)[:5000])
    else:
        click.echo(f"No data found for bug {bug_id}")


if __name__ == "__main__":
    cli()

#!/usr/bin/env python3
"""
Retry fetching data that failed in the main pipeline.

Subcommands:
    patches   -- retry the 813 bugs that have a commit hash but no patch diff
    crashes   -- retry the 4 bugs missing crash reports
    stats     -- just show what's missing (no network requests)

Usage:
    python retry_missing.py stats
    python retry_missing.py patches --limit 50
    python retry_missing.py patches           # all missing
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import click
from tqdm import tqdm

from . import config
from .storage import DataStore, ProgressDB
from .utils import RateLimitedClient
from .pipeline import _dict_to_bug

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def classify_missing_patches() -> dict[str, list[str]]:
    """Classify processed bugs by why they are missing a patch diff."""
    no_commits: list[str] = []       # fix_commits is empty
    no_hash: list[str] = []          # has commits but no hash or link
    fetch_failed: list[str] = []     # has hash but diff is empty (fetch failed)
    ok: list[str] = []               # already have diff

    for p in sorted(Path(config.PROCESSED_DIR).glob("*.json")):
        d = json.loads(p.read_text())
        fcs = d.get("fix_commits", [])
        if not fcs:
            no_commits.append(d["bug_id"])
        elif any(fc.get("patch_diff") for fc in fcs):
            ok.append(d["bug_id"])
        elif all(not fc.get("hash") and not fc.get("link") for fc in fcs):
            no_hash.append(d["bug_id"])
        else:
            fetch_failed.append(d["bug_id"])

    return {
        "ok": ok,
        "no_commits": no_commits,
        "no_hash": no_hash,
        "fetch_failed": fetch_failed,
    }


@click.group()
def cli():
    """Retry missing data collection."""
    pass


@cli.command()
def stats():
    """Show a breakdown of what data is missing and why."""
    processed = list(Path(config.PROCESSED_DIR).glob("*.json"))
    n = len(processed)

    patch_classes = classify_missing_patches()
    missing_crash = []
    missing_c_repro = []
    missing_syz_repro = []

    for p in processed:
        d = json.loads(p.read_text())
        crashes = d.get("crashes", [])
        if not any(c.get("crash_report") for c in crashes):
            missing_crash.append(d["bug_id"])
        if not any(c.get("c_reproducer") for c in crashes):
            missing_c_repro.append(d["bug_id"])
        if not any(c.get("syz_reproducer") for c in crashes):
            missing_syz_repro.append(d["bug_id"])

    print(f"\n{'='*55}")
    print(f"  Data Completeness Report  ({n} processed bugs)")
    print(f"{'='*55}")

    print(f"\n  PATCH DIFFS:")
    print(f"    Have patch diff        : {len(patch_classes['ok']):5d} ({len(patch_classes['ok'])/n*100:.1f}%)")
    print(f"    No fix_commits at all  : {len(patch_classes['no_commits']):5d}  ← bug had no commit recorded")
    print(f"    Commit has no hash/URL : {len(patch_classes['no_hash']):5d}  ← syzbot didn't record hash yet")
    print(f"    Hash present, fetch ✗  : {len(patch_classes['fetch_failed']):5d}  ← can retry with: python retry_missing.py patches")

    print(f"\n  CRASH REPORTS:")
    print(f"    Missing crash report   : {len(missing_crash):5d}  ← can retry with: python retry_missing.py crashes")
    print(f"    Missing C reproducer   : {len(missing_c_repro):5d}")
    print(f"    Missing syz reproducer : {len(missing_syz_repro):5d}")

    # Pipeline-level pending
    db = ProgressDB()
    db_stats = db.get_stats()
    db.close()
    pending = db_stats.get("pending", 0)
    total = sum(db_stats.values())
    print(f"\n  PIPELINE PROGRESS:")
    for step, count in sorted(db_stats.items()):
        bar = "█" * int(count / total * 30)
        print(f"    {step:20s}: {count:5d}  {bar}")
    print(f"\n    Run 'python main.py collect' to process {pending} remaining bugs.")
    print()


@cli.command()
@click.option("--limit", default=0, type=int, help="Max bugs to retry (0 = all)")
@click.option("--repos", default="", help="Comma-separated extra git repos to try")
def patches(limit, repos):
    """Retry fetching patch diffs for bugs where fetch previously failed."""
    classes = classify_missing_patches()
    targets = classes["fetch_failed"]

    if not targets:
        click.echo("No bugs with failed patch fetches found.")
        return

    if limit:
        targets = targets[:limit]

    click.echo(f"Retrying patch fetch for {len(targets)} bugs...")

    extra_repos = [r.strip() for r in repos.split(",") if r.strip()]

    asyncio.run(_retry_patches(targets, extra_repos))


@cli.command()
@click.option("--limit", default=0, type=int)
def crashes(limit):
    """Retry fetching crash reports for bugs that are missing them."""
    missing = []
    for p in sorted(Path(config.PROCESSED_DIR).glob("*.json")):
        d = json.loads(p.read_text())
        if not any(c.get("crash_report") for c in d.get("crashes", [])):
            missing.append(d["bug_id"])

    if not missing:
        click.echo("No bugs with missing crash reports.")
        return

    if limit:
        missing = missing[:limit]

    click.echo(f"Retrying crash report fetch for {len(missing)} bugs...")
    asyncio.run(_retry_crashes(missing))


# ── async helpers ─────────────────────────────────────────────────────────────

async def _retry_patches(bug_ids: list[str], extra_repos: list[str]):
    from .scraper.git_kernel import _build_patch_url_from_hash, _looks_like_patch

    store = DataStore()
    fixed = 0
    still_missing = 0

    async with RateLimitedClient() as client:
        progress = tqdm(total=len(bug_ids), desc="Retrying patches")
        for bug_id in bug_ids:
            data = store.load_processed(bug_id)
            if not data:
                progress.update(1)
                continue

            bug = _dict_to_bug(data)
            changed = False

            for fc in bug.fix_commits:
                if fc.patch_diff:
                    continue
                if not fc.hash and not fc.link:
                    continue

                # Build candidate URLs including user-supplied extra repos
                from .scraper.git_kernel import _build_patch_url_from_commit_link
                candidates = []
                if fc.link:
                    url = _build_patch_url_from_commit_link(fc.link)
                    if url:
                        candidates.append(url)
                if fc.hash:
                    candidates.extend(_build_patch_url_from_hash(fc.hash, fc.repo))
                    for repo in extra_repos:
                        candidates.append(f"{repo.rstrip('/')}/patch/?id={fc.hash}")

                for url in candidates:
                    diff = await client.fetch(url, use_cache=False)
                    if diff and _looks_like_patch(diff):
                        fc.patch_diff = diff
                        changed = True
                        logger.info(f"{bug_id}: fetched patch for {fc.hash[:12]} from {url[:60]}")
                        break
                else:
                    logger.debug(f"{bug_id}: still can't fetch patch for {fc.hash}")

            if changed:
                store.save_processed(bug)
                fixed += 1
            else:
                still_missing += 1

            progress.update(1)
        progress.close()

    click.echo(f"\nResults: {fixed} fixed, {still_missing} still missing")
    if still_missing:
        click.echo("Remaining missing patches are likely in staging trees not yet")
        click.echo("mirrored to git.kernel.org, or the commit was merged without")
        click.echo("a patch going through the mailing list.")


async def _retry_crashes(bug_ids: list[str]):
    from .scraper.syzbot import _download_crash_artifacts, _make_syzbot_url
    from .models import Crash

    store = DataStore()
    fixed = 0

    async with RateLimitedClient() as client:
        progress = tqdm(total=len(bug_ids), desc="Retrying crashes")
        for bug_id in bug_ids:
            data = store.load_processed(bug_id)
            if not data:
                progress.update(1)
                continue

            bug = _dict_to_bug(data)
            for crash in bug.crashes:
                if crash.crash_report:
                    continue
                await _download_crash_artifacts(client, crash)
                if crash.crash_report:
                    fixed += 1

            store.save_processed(bug)
            progress.update(1)
        progress.close()

    click.echo(f"\nFixed {fixed} missing crash reports.")


if __name__ == "__main__":
    cli()

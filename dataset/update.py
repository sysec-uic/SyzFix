"""One-command incremental dataset update: syzbot → local prep → HuggingFace.

Pulls only bugs that are new since the last run (the collect pipeline is
resumable: the refreshed syzbot bug list is merged with INSERT OR IGNORE and
only bugs not yet at the 'processed' step are fetched), rebuilds the local
viewer index and analyzer results, then uploads to HuggingFace Hub.

Usage:
    # Local-only update (no upload)
    python -m dataset.update

    # Update and upload to HuggingFace
    python -m dataset.update --repo xiaoguangwang/syzfix-dataset

    # See what would happen without uploading anything
    python -m dataset.update --repo xiaoguangwang/syzfix-dataset --dry-run

    # Skip the analyzer refresh (faster; stats/cross-layer results go stale)
    python -m dataset.update --skip-analysis

The upload step is skipped automatically when no new bugs were processed
(repacking and re-uploading ~2 GB for nothing); pass --force-upload to
upload anyway (e.g. after retry_missing backfilled crash reports).
"""

import argparse
import asyncio
import subprocess
import sys

from . import config


def _count_processed() -> int:
    """Number of bugs at the 'processed' step, per the progress DB.

    Globbing PROCESSED_DIR would also count non-bug artifacts written there
    (e.g. cherrypick_map.json from the stable-cherrypick scraper), inflating
    the total; the DB is the authoritative bug list.
    """
    if not config.DB_PATH.exists():
        return 0
    from .storage import ProgressDB
    db = ProgressDB()
    try:
        return len(db.get_bugs_at_step("processed"))
    finally:
        db.close()


def _run_module(mod: str, *args: str) -> None:
    cmd = [sys.executable, "-m", mod, *args]
    # flush so the header lands before the child's inherited-stdout output
    print(f"\n──▶ {' '.join(cmd[1:])}", flush=True)
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(
        description="Incrementally pull new syzbot bugs, prep locally, upload to HF.")
    parser.add_argument("--repo", default=None,
                        help="HuggingFace repo ID (e.g. user/syzfix-dataset). "
                             "Omit for a local-only update.")
    parser.add_argument("--limit", default=0, type=int,
                        help="Max new bugs to process this run (0 = all)")
    parser.add_argument("--skip-patchwork", action="store_true",
                        help="Skip the patchwork fallback step during collection")
    parser.add_argument("--skip-analysis", action="store_true",
                        help="Do not re-run the heuristic analyzers")
    parser.add_argument("--force-upload", action="store_true",
                        help="Upload even if no new bugs were processed")
    parser.add_argument("--private", action="store_true", help="Make the HF repo private")
    parser.add_argument("--dry-run", action="store_true",
                        help="Collect and prep locally, but only show upload stats")
    args = parser.parse_args()

    # ── 1. Incremental collect (resume=True → only new/pending bugs) ──────
    before = _count_processed()
    from .pipeline import run_pipeline
    asyncio.run(run_pipeline(
        limit=args.limit,
        resume=True,
        skip_patchwork=args.skip_patchwork,
    ))
    after = _count_processed()
    new_bugs = after - before
    print(f"\nProcessed bugs: {after} total ({new_bugs:+d} this run)")

    # Nothing new → nothing to rebuild or upload. --force-upload overrides
    # (e.g. after retry_missing backfilled data without adding bugs);
    # --dry-run still walks the remaining steps to show what would happen.
    if new_bugs == 0 and not args.force_upload and not args.dry_run:
        print("No new bugs — index, analyzers and upload all skipped. "
              "Use --force-upload to rebuild and upload anyway.")
        return

    # ── 2. Rebuild the lightweight viewer index ───────────────────────────
    _run_module("dataset.view", "build-index")

    # ── 3. Refresh analyzer results (stats, cross-layer, …) ───────────────
    if args.skip_analysis:
        print("Skipping analyzer refresh (--skip-analysis).")
    else:
        _run_module("analysis.run_all")

    # ── 4. Upload to HuggingFace ──────────────────────────────────────────
    if not args.repo:
        print("\nNo --repo given — local update finished, nothing uploaded.")
        return

    from .upload_hf import upload, upload_processed, _write_restore_script
    # Flat structured export (small, rebuilt from processed data)
    upload(args.repo, private=args.private, dry_run=args.dry_run)
    # Full processed data (~11 GB raw → ~2 GB gzipped, streamed)
    _write_restore_script()
    upload_processed(args.repo, private=args.private, dry_run=args.dry_run)

    print("\nDone." if not args.dry_run else "\nDry run complete — nothing uploaded.")


if __name__ == "__main__":
    main()

"""Main pipeline: orchestrates all scrapers to build the dataset."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

from tqdm import tqdm

from . import config
from .models import BugEntry
from .storage import DataStore, ProgressDB
from .utils import RateLimitedClient
from .scraper import syzbot, git_kernel, lore, patchwork

logger = logging.getLogger(__name__)


async def process_single_bug(
    client: RateLimitedClient,
    bug_id: str,
    db: ProgressDB,
    store: DataStore,
) -> BugEntry | None:
    """Process a single bug through all pipeline stages."""
    current_step = db._conn.execute(
        "SELECT step FROM bugs WHERE bug_id = ?", (bug_id,)
    ).fetchone()
    current_step = current_step["step"] if current_step else "pending"

    bug = None

    # Stage 1: Fetch syzbot details
    if current_step == "pending":
        try:
            bug = await syzbot.fetch_bug_details(client, bug_id, store)
            if bug:
                store.save_processed(bug)
                db.update_step(bug_id, "syzbot_fetched")
                current_step = "syzbot_fetched"
            else:
                db.add_error(bug_id, "Failed to fetch syzbot details")
                return None
        except Exception as e:
            logger.error(f"Bug {bug_id}: syzbot fetch error: {e}")
            db.add_error(bug_id, f"syzbot fetch error: {e}")
            return None

    # Load existing data for subsequent stages
    if bug is None:
        stored = store.load_processed(bug_id)
        if stored:
            bug = _dict_to_bug(stored)
        else:
            logger.error(f"Bug {bug_id}: no stored data found at step {current_step}")
            return None

    # Stage 2: Fetch patch diffs from git.kernel.org
    if current_step == "syzbot_fetched":
        try:
            n = await git_kernel.fetch_patch_diffs(client, bug)
            logger.info(f"Bug {bug_id}: fetched {n}/{len(bug.fix_commits)} patch diffs")
            store.save_processed(bug)
            db.update_step(bug_id, "patches_fetched")
            current_step = "patches_fetched"
        except Exception as e:
            logger.error(f"Bug {bug_id}: patch fetch error: {e}")
            db.add_error(bug_id, f"patch fetch error: {e}")

    # Stage 3: Fetch discussions from lore.kernel.org
    if current_step == "patches_fetched":
        try:
            n = await lore.fetch_discussions(client, bug)
            logger.info(f"Bug {bug_id}: fetched {n}/{len(bug.discussions)} discussions")

            # If no discussions, try searching lore
            if n == 0 and bug.fix_commits:
                for fc in bug.fix_commits:
                    if fc.title:
                        extra_discs = await lore.search_lore_for_patch(client, fc.title, bug)
                        if extra_discs:
                            bug.discussions.extend(extra_discs)
                            # Fetch the newly found discussions
                            await lore.fetch_discussions(client, bug)
                            break

            # Build patch versions from discussions
            bug.patch_versions = lore.build_patch_versions(bug)

            store.save_processed(bug)
            db.update_step(bug_id, "discussions_fetched")
            current_step = "discussions_fetched"
        except Exception as e:
            logger.error(f"Bug {bug_id}: discussion fetch error: {e}")
            db.add_error(bug_id, f"discussion fetch error: {e}")

    # Stage 4: Supplement from patchwork (fallback)
    if current_step == "discussions_fetched":
        try:
            # Only use patchwork if we have limited discussion data
            has_good_data = any(
                len(d.messages) > 0 for d in bug.discussions if not d.is_syzbot_report
            )
            if not has_good_data:
                found = await patchwork.supplement_bug_data(client, bug)
                if found:
                    logger.info(f"Bug {bug_id}: supplemented data from patchwork")

            store.save_processed(bug)
            db.update_step(bug_id, "processed")
            current_step = "processed"
        except Exception as e:
            logger.error(f"Bug {bug_id}: patchwork fetch error: {e}")
            db.add_error(bug_id, f"patchwork fetch error: {e}")
            # Still mark as processed even if patchwork fails
            db.update_step(bug_id, "processed")

    return bug


async def run_pipeline(
    limit: int = 0,
    resume: bool = True,
    skip_patchwork: bool = False,
):
    """
    Run the full pipeline.

    Args:
        limit: Max number of bugs to process (0 = all)
        resume: Whether to resume from previous progress
        skip_patchwork: Skip the patchwork fallback step
    """
    db = ProgressDB()
    store = DataStore()

    async with RateLimitedClient() as client:
        # Step 1: Fetch bug list
        stats = db.get_stats()
        total_in_db = sum(stats.values())

        bug_list = await syzbot.fetch_bug_list(client)
        if not bug_list:
            if total_in_db == 0 or not resume:
                logger.error("Failed to fetch bug list. Aborting.")
                return
            logger.warning("Failed to refresh bug list; continuing with existing DB state.")
        else:
            db.save_bug_list(bug_list)
            if total_in_db == 0 or not resume:
                logger.info(f"Saved {len(bug_list)} bugs to database")
            else:
                new_total = sum(db.get_stats().values())
                added = new_total - total_in_db
                logger.info(
                    f"Refreshed bug list: {added} new bugs since last run "
                    f"({new_total} total)"
                )

        # Step 2: Process each bug
        pending = db.get_pending_bugs("processed")
        if limit > 0:
            pending = pending[:limit]

        logger.info(f"Processing {len(pending)} bugs...")

        progress = tqdm(total=len(pending), desc="Processing bugs")
        for bug_id in pending:
            try:
                await process_single_bug(client, bug_id, db, store)
            except Exception as e:
                logger.error(f"Unexpected error processing {bug_id}: {e}")
                db.add_error(bug_id, f"Unexpected error: {e}")
            progress.update(1)

        progress.close()

        # Print stats
        final_stats = db.get_stats()
        logger.info("=== Pipeline Stats ===")
        for step, count in sorted(final_stats.items()):
            logger.info(f"  {step}: {count}")

    db.close()


def _dict_to_bug(data: dict) -> BugEntry:
    """Reconstruct a BugEntry from a stored dict."""
    from .models import Crash, Discussion, Email, FixCommit, PatchVersion

    bug = BugEntry(
        bug_id=data.get("bug_id", ""),
        title=data.get("title", ""),
        status=data.get("status", ""),
        first_crash=data.get("first_crash", ""),
        last_crash=data.get("last_crash", ""),
        fix_time=data.get("fix_time", ""),
        raw_syzbot_data=data.get("raw_syzbot_data", {}),
        processing_errors=data.get("processing_errors", []),
    )

    for fc_data in data.get("fix_commits", []):
        bug.fix_commits.append(FixCommit(**{
            k: fc_data.get(k, "") for k in FixCommit.__dataclass_fields__
        }))

    for disc_data in data.get("discussions", []):
        disc = Discussion(
            url=disc_data.get("url", ""),
            subject=disc_data.get("subject", ""),
            patch_version=disc_data.get("patch_version"),
            is_syzbot_report=disc_data.get("is_syzbot_report", False),
        )
        for msg_data in disc_data.get("messages", []):
            disc.messages.append(Email(**{
                k: msg_data.get(k, "") for k in Email.__dataclass_fields__
            }))
        bug.discussions.append(disc)

    for crash_data in data.get("crashes", []):
        bug.crashes.append(Crash(**{
            k: crash_data.get(k, "") for k in Crash.__dataclass_fields__
        }))

    for pv_data in data.get("patch_versions", []):
        pv = PatchVersion(
            version=pv_data.get("version", 1),
            subject=pv_data.get("subject", ""),
            diff=pv_data.get("diff", ""),
            cover_letter=pv_data.get("cover_letter", ""),
        )
        for msg_data in pv_data.get("discussion", []):
            pv.discussion.append(Email(**{
                k: msg_data.get(k, "") for k in Email.__dataclass_fields__
            }))
        bug.patch_versions.append(pv)

    return bug

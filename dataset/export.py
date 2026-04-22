"""Export processed data into fine-tuning friendly dataset formats."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from . import config
from .models import DatasetEntry
from .storage import DataStore, ProgressDB
from .pipeline import _dict_to_bug

logger = logging.getLogger(__name__)


def bug_to_dataset_entry(bug_data: dict) -> DatasetEntry | None:
    """Convert a processed bug dict to a dataset entry."""
    bug = _dict_to_bug(bug_data)

    if not bug.fix_commits:
        return None

    # Get the primary crash report and reproducer (from first crash)
    crash_report = ""
    c_reproducer = ""
    syz_reproducer = ""
    if bug.crashes:
        crash = bug.crashes[0]
        crash_report = crash.crash_report
        c_reproducer = crash.c_reproducer
        syz_reproducer = crash.syz_reproducer

    # Get the final patch (from first fix commit)
    fc = bug.fix_commits[0]
    final_patch_diff = fc.patch_diff

    # Build patch evolution
    patch_evolution = []
    for pv in sorted(bug.patch_versions, key=lambda x: x.version):
        evolution_entry = {
            "version": pv.version,
            "subject": pv.subject,
            "diff": pv.diff,
            "cover_letter": pv.cover_letter,
            "discussion": [
                {
                    "from": msg.from_addr,
                    "date": msg.date,
                    "subject": msg.subject,
                    "body": msg.body,
                }
                for msg in pv.discussion
            ],
        }
        patch_evolution.append(evolution_entry)

    # Determine subsystem from title or raw data
    subsystem = ""
    raw = bug.raw_syzbot_data
    if isinstance(raw, dict):
        # Try to extract from raw data
        subsystem = raw.get("subsystem", "")

    entry = DatasetEntry(
        bug_id=bug.bug_id,
        title=bug.title,
        crash_report=crash_report,
        c_reproducer=c_reproducer,
        syz_reproducer=syz_reproducer,
        fix_commit_hash=fc.hash,
        fix_commit_message=fc.title,
        final_patch_diff=final_patch_diff,
        patch_evolution=patch_evolution,
        subsystem=subsystem,
        first_crash_date=bug.first_crash,
        fix_date=bug.fix_time,
        num_patch_versions=len(bug.patch_versions),
        has_discussion=any(
            len(d.messages) > 0 for d in bug.discussions if not d.is_syzbot_report
        ),
    )

    return entry


def export_jsonl(output_path: Path | None = None):
    """Export all processed bugs as a JSONL file (one JSON per line)."""
    output_path = output_path or config.DATASET_DIR / "syzbot_dataset.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    store = DataStore()
    db = ProgressDB()
    processed_ids = db.get_bugs_at_step("processed")

    count = 0
    skipped = 0

    with open(output_path, "w", encoding="utf-8") as f:
        for bug_id in processed_ids:
            data = store.load_processed(bug_id)
            if not data:
                skipped += 1
                continue

            entry = bug_to_dataset_entry(data)
            if entry is None:
                skipped += 1
                continue

            # Only include entries with meaningful data
            if not entry.crash_report and not entry.final_patch_diff:
                skipped += 1
                continue

            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            count += 1

    db.close()
    logger.info(f"Exported {count} entries to {output_path} (skipped {skipped})")
    return count


def export_huggingface(output_dir: Path | None = None):
    """Export as a HuggingFace Dataset."""
    try:
        from datasets import Dataset
    except ImportError:
        logger.error("Please install 'datasets' package: pip install datasets")
        return

    output_dir = output_dir or config.DATASET_DIR / "hf_dataset"

    store = DataStore()
    db = ProgressDB()
    processed_ids = db.get_bugs_at_step("processed")

    records = []
    for bug_id in processed_ids:
        data = store.load_processed(bug_id)
        if not data:
            continue

        entry = bug_to_dataset_entry(data)
        if entry is None:
            continue

        if not entry.crash_report and not entry.final_patch_diff:
            continue

        record = asdict(entry)
        # Flatten patch_evolution to JSON string for HF compatibility
        record["patch_evolution"] = json.dumps(record["patch_evolution"], ensure_ascii=False)
        records.append(record)

    if records:
        dataset = Dataset.from_list(records)
        dataset.save_to_disk(str(output_dir))
        logger.info(f"Exported {len(records)} entries to HuggingFace Dataset at {output_dir}")
    else:
        logger.warning("No records to export")

    db.close()


def print_dataset_stats():
    """Print statistics about the dataset."""
    db = ProgressDB()
    store = DataStore()
    processed_ids = db.get_bugs_at_step("processed")

    total = len(processed_ids)
    has_crash = 0
    has_patch = 0
    has_discussion = 0
    has_evolution = 0
    has_reproducer = 0

    for bug_id in processed_ids:
        data = store.load_processed(bug_id)
        if not data:
            continue

        entry = bug_to_dataset_entry(data)
        if entry is None:
            continue

        if entry.crash_report:
            has_crash += 1
        if entry.final_patch_diff:
            has_patch += 1
        if entry.has_discussion:
            has_discussion += 1
        if entry.num_patch_versions > 1:
            has_evolution += 1
        if entry.c_reproducer or entry.syz_reproducer:
            has_reproducer += 1

    print(f"\n{'='*50}")
    print(f"Dataset Statistics")
    print(f"{'='*50}")
    print(f"Total processed bugs: {total}")
    print(f"With crash report:    {has_crash} ({_pct(has_crash, total)})")
    print(f"With final patch:     {has_patch} ({_pct(has_patch, total)})")
    print(f"With discussion:      {has_discussion} ({_pct(has_discussion, total)})")
    print(f"With patch evolution:  {has_evolution} ({_pct(has_evolution, total)})")
    print(f"With reproducer:      {has_reproducer} ({_pct(has_reproducer, total)})")
    print(f"{'='*50}\n")

    # Pipeline stats
    print("Pipeline Progress:")
    for step, count in sorted(db.get_stats().items()):
        print(f"  {step}: {count}")

    db.close()


def _pct(n: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{n/total*100:.1f}%"

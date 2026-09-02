#!/usr/bin/env python3
"""
Upload the SyzFix dataset to HuggingFace Hub.

Usage:
    # First login: huggingface-cli login
    python upload_hf.py --repo your-username/syzfix-dataset            # flat export
    python upload_hf.py --repo your-username/syzfix-dataset --training # training configs
    python upload_hf.py --repo your-username/syzfix-dataset --processed # full processed data
    python upload_hf.py --repo your-username/syzfix-dataset --memory   # memory system data

Options:
    --repo       HuggingFace repo ID  (e.g. "alice/syzfix-dataset")
    --private    Make the repo private (default: public)
    --export     Also regenerate the JSONL before uploading
    --training   Upload training-format JSONL files as dataset configs
    --processed  Pack and upload data/processed/ as processed.jsonl.gz
                 (needed for collaborators to run prepare_training.py)
    --memory     Upload memory system data (knowledge base, FAISS indices,
                 trajectories, distilled rules) to HF under memory/ prefix
    --dry-run    Show what would be uploaded without actually doing it
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from . import config

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def iter_records(stats: dict | None = None):
    """Yield flat dataset records one bug at a time (constant memory).

    Building all records in a list needs the whole corpus in RAM (~25 GB at
    7k bugs) and gets OOM-killed; this generator keeps one bug at a time.
    Pass a dict as `stats` to have counters filled in as a side effect.
    """
    from .export import bug_to_dataset_entry
    from .storage import DataStore, ProgressDB

    store = DataStore()
    db = ProgressDB()
    processed_ids = db.get_bugs_at_step("processed")
    db.close()

    for bug_id in processed_ids:
        data = store.load_processed(bug_id)
        if data:
            entry = bug_to_dataset_entry(data)
        if not data or entry is None or (not entry.crash_report and not entry.final_patch_diff):
            if stats is not None:
                stats["skipped"] = stats.get("skipped", 0) + 1
            continue
        record = asdict(entry)
        record["patch_evolution"] = json.dumps(record["patch_evolution"], ensure_ascii=False)
        if stats is not None:
            stats["total"] = stats.get("total", 0) + 1
            for key, hit in (
                ("crash_report", bool(record["crash_report"])),
                ("patch_diff", bool(record["final_patch_diff"])),
                ("discussion", bool(record["has_discussion"])),
                ("evolution", record["num_patch_versions"] > 1),
            ):
                stats[key] = stats.get(key, 0) + hit
        yield record


def upload(repo_id: str, private: bool = False, dry_run: bool = False):
    try:
        from datasets import Dataset
        from huggingface_hub import HfApi
    except ImportError:
        print("Install required packages: pip install datasets huggingface_hub")
        return

    api = HfApi()

    # Check login
    try:
        user = api.whoami()
        logger.info(f"Logged in as: {user['name']}")
    except Exception:
        print("Not logged in. Run: huggingface-cli login")
        return

    stats: dict = {}

    if dry_run:
        for _ in iter_records(stats):
            pass
        _print_summary(stats)
        print(f"\n[dry-run] Would upload {stats.get('total', 0)} records to: {repo_id}")
        return

    # Stream records into an on-disk Arrow cache — never the whole corpus in
    # RAM. push_to_hub then reads the cache memory-mapped and uploads parquet
    # shards under data/.
    logger.info("Building dataset (streaming into on-disk Arrow cache)...")
    dataset = Dataset.from_generator(iter_records, gen_kwargs={"stats": stats})
    if not len(dataset):
        print("No records to upload.")
        return
    _print_summary(stats)

    logger.info(f"Uploading to HuggingFace Hub: {repo_id}")
    dataset.push_to_hub(
        repo_id,
        private=private,
        commit_message=f"Update dataset: {len(dataset)} syzbot fixed kernel bugs",
    )

    hub_url = f"https://huggingface.co/datasets/{repo_id}"
    print(f"\nDataset uploaded successfully!")
    print(f"View at: {hub_url}")


def _print_summary(stats: dict):
    print(f"\nDataset summary:")
    print(f"  Total records         : {stats.get('total', 0)} ({stats.get('skipped', 0)} skipped)")
    print(f"  With crash report     : {stats.get('crash_report', 0)}")
    print(f"  With patch diff       : {stats.get('patch_diff', 0)}")
    print(f"  With discussion       : {stats.get('discussion', 0)}")
    print(f"  With patch evolution  : {stats.get('evolution', 0)}")


def _build_dataset_card(repo_id: str, task_splits: dict[str, dict[str, int]]) -> str:
    """Build a README.md with YAML front-matter so HF auto-parses configs."""
    lines = ["---", "configs:"]
    for task, splits in task_splits.items():
        lines.append(f"- config_name: {task}")
        lines.append(f"  data_files:")
        for split in ("train", "val", "test"):
            if split in splits:
                hf_split = "validation" if split == "val" else split
                lines.append(f"  - split: {hf_split}")
                lines.append(f"    path: data/{task}/{split}.jsonl")
    lines += [
        "---",
        "",
        "# SyzFix – Linux Kernel Bug-Fix Dataset (Training Edition)",
        "",
        "Derived from [syzbot](https://syzkaller.appspot.com/) fixed-bug reports.",
        "Each config is a self-contained training task.",
        "",
        "## Configs",
        "",
        "| Config | Task type | Description |",
        "|--------|-----------|-------------|",
        "| `bug_to_patch` | SFT | Generate a patch from a crash report |",
        "| `patch_review` | SFT | Critique a patch given the bug context |",
        "| `patch_improvement` | SFT | Rewrite an earlier patch into a better one |",
        "| `dpo` | DPO/ORPO | Preference pairs: better vs worse patch |",
        "| `commit_message` | SFT | Write a commit message for a patch |",
        "",
        "## Usage",
        "",
        "```python",
        "from datasets import load_dataset",
        "",
        f'ds = load_dataset("{repo_id}", "bug_to_patch")',
        "print(ds[\"train\"][0])",
        "",
        "# DPO",
        f'dpo = load_dataset("{repo_id}", "dpo")',
        "# fields: prompt, chosen, rejected",
        "```",
        "",
        "## Split sizes",
        "",
    ]
    for task, splits in task_splits.items():
        parts = ", ".join(f"{s}: {n}" for s, n in splits.items())
        lines.append(f"- **{task}**: {parts}")
    lines.append("")
    return "\n".join(lines)


def upload_training(repo_id: str, private: bool = False, dry_run: bool = False):
    """Upload training-format data as separate dataset configurations.

    Files are streamed directly via HfApi.upload_file() — no dataset is
    materialised in RAM — so this works even on machines with limited memory.

    Creates one HF dataset config per task, each with train/val/test splits.
    Users can then load individual tasks with:
        load_dataset("user/syzfix-dataset", "bug_to_patch")
    """
    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("Install required packages: pip install huggingface_hub")
        return

    import training_config as tcfg

    api = HfApi()
    try:
        user = api.whoami()
        logger.info(f"Logged in as: {user['name']}")
    except Exception:
        print("Not logged in. Run: huggingface-cli login")
        return

    training_dir = tcfg.TRAINING_DIR
    if not training_dir.exists():
        print(f"Training data not found at {training_dir}")
        print("Run: python prepare_training.py --tasks all")
        return

    # Map task name → local subdirectory
    task_dirs = {
        "bug_to_patch":       training_dir / "sft_bug_to_patch",
        "patch_review":       training_dir / "sft_patch_review",
        "patch_improvement":  training_dir / "sft_patch_improvement",
        "dpo":                training_dir / "dpo_patch_preference",
        "commit_message":     training_dir / "sft_commit_message",
    }

    # Collect available files and line counts (cheap – just counts newlines)
    task_splits: dict[str, dict[str, int]] = {}
    upload_queue: list[tuple[Path, str]] = []  # (local_path, repo_path)
    for task, task_dir in task_dirs.items():
        if not task_dir.exists():
            print(f"  {task}: MISSING – skipping")
            continue
        splits: dict[str, int] = {}
        for split in ("train", "val", "test"):
            f = task_dir / f"{split}.jsonl"
            if not f.exists():
                continue
            with open(f, "rb") as fh:
                splits[split] = sum(1 for _ in fh)
            upload_queue.append((f, f"data/{task}/{split}.jsonl"))
        if splits:
            task_splits[task] = splits

    print(f"\nTraining data to upload → {repo_id}")
    for task, splits in task_splits.items():
        parts = "  ".join(f"{s}={n}" for s, n in splits.items())
        print(f"  {task:20s}  {parts}")
    total_files = len(upload_queue)
    print(f"\n  {total_files} JSONL files total")

    if dry_run:
        print(f"\n[dry-run] No files uploaded.")
        return

    if not task_splits:
        print("Nothing to upload.")
        return

    # Ensure repo exists
    api.create_repo(repo_id=repo_id, repo_type="dataset", private=private, exist_ok=True)

    # Upload JSONL files one at a time – constant memory, file is streamed
    for i, (local_path, repo_path) in enumerate(upload_queue, 1):
        size_mb = local_path.stat().st_size / 1_048_576
        logger.info(f"[{i}/{total_files}] {repo_path}  ({size_mb:.1f} MB)")
        api.upload_file(
            path_or_fileobj=str(local_path),
            path_in_repo=repo_path,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=f"Add {repo_path}",
        )

    # Upload dataset card (README.md) so HF parses configs automatically
    readme = _build_dataset_card(repo_id, task_splits)
    api.upload_file(
        path_or_fileobj=readme.encode(),
        path_in_repo="README.md",
        repo_id=repo_id,
        repo_type="dataset",
        commit_message="Add dataset card with config definitions",
    )

    print(f"\nTraining data uploaded successfully!")
    print(f"  https://huggingface.co/datasets/{repo_id}")
    print(f"\nLoad example:")
    print(f'  from datasets import load_dataset')
    print(f'  ds = load_dataset("{repo_id}", "bug_to_patch")')
    print(f'  # Or for DPO:')
    print(f'  ds = load_dataset("{repo_id}", "dpo")')


def upload_processed(repo_id: str, private: bool = False, dry_run: bool = False):
    """Pack data/processed/*.json into a gzipped JSONL and upload to HF.

    This is the full rich dataset (11 GB raw → ~2.2 GB gzipped) that contains
    complete mailing-list threads, all crash variants, raw syzbot fields, etc.
    Collaborators need this file to run prepare_training.py with custom settings.

    The file is streamed through gzip line-by-line so RAM usage stays constant.
    """
    import gzip
    import tempfile

    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("Install required packages: pip install huggingface_hub")
        return

    api = HfApi()
    try:
        user = api.whoami()
        logger.info(f"Logged in as: {user['name']}")
    except Exception:
        print("Not logged in. Run: huggingface-cli login")
        return

    processed_dir = config.DATA_DIR / "processed"
    if not processed_dir.exists():
        print(f"Processed data not found at {processed_dir}")
        return

    # Take the bug list from the progress DB instead of globbing:
    # PROCESSED_DIR also holds non-bug artifacts (e.g. cherrypick_map.json
    # from the stable-cherrypick scraper) which would otherwise be packed
    # into processed.jsonl.gz as a bogus record and counted as a bug.
    # Matches iter_records() / the flat export.
    from .storage import ProgressDB
    db = ProgressDB()
    try:
        bug_ids = db.get_bugs_at_step("processed")
    finally:
        db.close()
    json_files = sorted(processed_dir / f"{bug_id}.json" for bug_id in bug_ids)
    missing = [f for f in json_files if not f.exists()]
    if missing:
        logger.warning(f"{len(missing)} bugs marked processed have no JSON on disk")
        json_files = [f for f in json_files if f.exists()]
    total = len(json_files)
    raw_bytes = sum(f.stat().st_size for f in json_files)
    print(f"\nProcessed data to pack → {repo_id}")
    print(f"  {total} JSON files  ({raw_bytes / 1_073_741_824:.1f} GB raw)")
    print(f"  Destination in repo: processed/processed.jsonl.gz  (~2 GB estimated)")

    if dry_run:
        print("\n[dry-run] No files uploaded.")
        return

    api.create_repo(repo_id=repo_id, repo_type="dataset", private=private, exist_ok=True)

    # Stream: read each JSON → write one line to a temporary .gz file
    with tempfile.NamedTemporaryFile(suffix=".jsonl.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    logger.info(f"Packing {total} files into {tmp_path} ...")
    written = 0
    with gzip.open(tmp_path, "wb") as gz:
        for i, jf in enumerate(json_files, 1):
            try:
                obj = json.loads(jf.read_text(encoding="utf-8", errors="replace"))
                line = json.dumps(obj, ensure_ascii=False) + "\n"
                gz.write(line.encode("utf-8"))
                written += 1
            except Exception as e:
                logger.warning(f"Skipping {jf.name}: {e}")
            if i % 500 == 0:
                done_mb = tmp_path.stat().st_size / 1_048_576
                logger.info(f"  [{i}/{total}] packed  →  {done_mb:.0f} MB so far")

    gz_size_mb = tmp_path.stat().st_size / 1_048_576
    logger.info(f"Packed {written} records into {gz_size_mb:.0f} MB gzip file")

    logger.info("Uploading processed.jsonl.gz ...")
    api.upload_file(
        path_or_fileobj=str(tmp_path),
        path_in_repo="processed/processed.jsonl.gz",
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=f"Add full processed data ({written} bugs, {gz_size_mb:.0f} MB gzip)",
    )
    tmp_path.unlink()

    print(f"\nProcessed data uploaded!")
    print(f"  https://huggingface.co/datasets/{repo_id}/tree/main/processed")
    print(f"\nCollaborators can restore it with:")
    print(f'  python -m dataset.restore_processed --repo {repo_id}')


def upload_memory(repo_id: str, private: bool = False, dry_run: bool = False):
    """Upload the memory system data to HuggingFace Hub.

    Uploads all memory artifacts under a memory/ prefix in the dataset repo.
    This lets others download the pre-built memory system without running
    the ~50-min build step.

    Files uploaded:
        memory/instance_memory.jsonl    - per-bug memory entries (46 MB)
        memory/trajectories.jsonl       - multi-version conversation chains (3.7 MB)
        memory/pattern_memory.json      - aggregated fix strategies + review lessons
        memory/distilled_rules.json     - revision-specific rules
        memory/inverted_indices.json    - fast lookup indices
        memory/split.json               - train/eval split definition
        memory/crash_embeddings.npy     - FAISS crash embeddings
        memory/patch_embeddings.npy     - FAISS patch embeddings
        memory/faiss_crash.index        - FAISS crash index
        memory/faiss_patch.index        - FAISS patch index
        memory/bug_id_map.json          - embedding ID mapping
    """
    # Memory data directory — built in the syzfix-research repo. Defaults to
    # memory/data under the current working directory (i.e. run this from the
    # research repo root); SYZFIX_MEMORY_DIR overrides.
    memory_dir = Path(os.environ.get("SYZFIX_MEMORY_DIR", Path.cwd() / "memory" / "data"))
    if not memory_dir.exists():
        print(f"Memory data not found at {memory_dir}")
        print("Memory artifacts are built in the syzfix-research repo "
              "(python -m memory.build); run this command from its root "
              "or set SYZFIX_MEMORY_DIR.")
        return

    # Files to upload (order: large → small for progress visibility)
    memory_files = [
        "instance_memory.jsonl",
        "trajectories.jsonl",
        "inverted_indices.json",
        "split.json",
        "pattern_memory.json",
        "distilled_rules.json",
        "crash_embeddings.npy",
        "patch_embeddings.npy",
        "faiss_crash.index",
        "faiss_patch.index",
        "bug_id_map.json",
    ]

    # Collect available files
    upload_queue: list[tuple[Path, str]] = []
    total_bytes = 0
    for fname in memory_files:
        fpath = memory_dir / fname
        if fpath.exists():
            upload_queue.append((fpath, f"memory/{fname}"))
            total_bytes += fpath.stat().st_size
        else:
            logger.warning(f"Missing: {fname}")

    # Also upload the human-readable knowledge export
    export_md = memory_dir / "export" / "pattern_knowledge.md"
    if export_md.exists():
        upload_queue.append((export_md, "memory/pattern_knowledge.md"))
        total_bytes += export_md.stat().st_size

    print(f"\nMemory data to upload → {repo_id}")
    for fpath, repo_path in upload_queue:
        size_mb = fpath.stat().st_size / 1_048_576
        print(f"  {repo_path:45s}  {size_mb:6.1f} MB")
    print(f"\n  {len(upload_queue)} files, {total_bytes / 1_048_576:.1f} MB total")

    if dry_run:
        print(f"\n[dry-run] No files uploaded.")
        return

    if not upload_queue:
        print("Nothing to upload.")
        return

    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("Install required packages: pip install huggingface_hub")
        return

    api = HfApi()
    try:
        user = api.whoami()
        logger.info(f"Logged in as: {user['name']}")
    except Exception:
        print("Not logged in. Run: huggingface-cli login")
        return

    api.create_repo(repo_id=repo_id, repo_type="dataset", private=private, exist_ok=True)

    for i, (local_path, repo_path) in enumerate(upload_queue, 1):
        size_mb = local_path.stat().st_size / 1_048_576
        logger.info(f"[{i}/{len(upload_queue)}] {repo_path}  ({size_mb:.1f} MB)")
        api.upload_file(
            path_or_fileobj=str(local_path),
            path_in_repo=repo_path,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=f"Add {repo_path}",
        )

    print(f"\nMemory data uploaded successfully!")
    print(f"  https://huggingface.co/datasets/{repo_id}/tree/main/memory")
    print(f"\nDownload with:")
    print(f"  python -m memory.download --repo {repo_id}")


def _write_restore_script():
    """Write restore_processed.py next to this file if it doesn't exist."""
    restore = Path(__file__).parent / "restore_processed.py"
    if restore.exists():
        return
    script = '''\
#!/usr/bin/env python3
"""
Download processed.jsonl.gz from HF and restore data/processed/*.json.

Usage:
    python restore_processed.py --repo yourname/syzfix-dataset
"""
import argparse, gzip, json, sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--out", default="data/processed",
                        help="Output directory (default: data/processed)")
    args = parser.parse_args()

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        sys.exit("pip install huggingface_hub")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading processed.jsonl.gz from {args.repo} ...")
    gz_path = hf_hub_download(
        repo_id=args.repo,
        filename="processed/processed.jsonl.gz",
        repo_type="dataset",
    )

    print(f"Unpacking into {out_dir} ...")
    with gzip.open(gz_path, "rt", encoding="utf-8") as gz:
        for i, line in enumerate(gz, 1):
            obj = json.loads(line)
            bug_id = obj.get("bug_id", f"unknown_{i:06d}")
            (out_dir / f"{bug_id}.json").write_text(
                json.dumps(obj, indent=2, ensure_ascii=False)
            )
            if i % 500 == 0:
                print(f"  {i} records restored ...")

    print(f"Done. {i} records written to {out_dir}/")

if __name__ == "__main__":
    main()
'''
    restore.write_text(script)
    print(f"[info] Wrote restore_processed.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload dataset to HuggingFace Hub")
    parser.add_argument("--repo", required=True, help="HuggingFace repo ID (e.g. user/syzfix-dataset)")
    parser.add_argument("--private", action="store_true", help="Make repo private")
    parser.add_argument("--export", action="store_true", help="Regenerate JSONL before uploading")
    parser.add_argument("--training", action="store_true",
                        help="Upload training-format data as separate HF dataset configs")
    parser.add_argument("--processed", action="store_true",
                        help="Pack and upload data/processed/ as processed.jsonl.gz")
    parser.add_argument("--memory", action="store_true",
                        help="Upload memory system data (knowledge base, indices, trajectories)")
    parser.add_argument("--dry-run", action="store_true", help="Show stats without uploading")
    args = parser.parse_args()

    if args.memory:
        upload_memory(args.repo, private=args.private, dry_run=args.dry_run)
    elif args.training:
        upload_training(args.repo, private=args.private, dry_run=args.dry_run)
    elif args.processed:
        _write_restore_script()
        upload_processed(args.repo, private=args.private, dry_run=args.dry_run)
    else:
        if args.export:
            from .export import export_jsonl
            logger.info("Regenerating JSONL export...")
            export_jsonl()
        upload(args.repo, private=args.private, dry_run=args.dry_run)

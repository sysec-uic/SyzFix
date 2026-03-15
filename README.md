# SyzFix

**SyzFix** is a dataset builder that collects fixed Linux kernel bugs from
[syzbot/syzkaller](https://syzkaller.appspot.com/upstream/fixed) and assembles a
structured dataset capturing the **full bug-fix lifecycle** — from the initial crash
report through patch iterations and reviewer discussions to the final merged commit.

The dataset is intended for:
- **Understanding** how real Linux kernel bugs get diagnosed and fixed
- **Fine-tuning** language models to generate better kernel bug fixes
- **Researching** patch evolution patterns in the Linux development process

> **Resources**
> - Code: https://github.com/sysec-uic/syzfix
> - Dataset: https://huggingface.co/datasets/xiaoguangwang/syzfix-dataset

---

## Reproducing Without Re-crawling

Collaborators can skip the 8–10 hour crawl entirely. The dataset is already on
HuggingFace; you only need to choose how deeply you want to work with it.

### Step 0 — Clone the repo and install dependencies

```bash
git clone https://github.com/sysec-uic/syzfix.git
cd syzfix
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r syzbot-dataset/requirements.txt
cd syzbot-dataset
```

### Option A — Use the training data directly (fastest, ~210 MB)

Ready-to-use JSONL files in chat/instruction format. No further processing needed.

```python
from datasets import load_dataset

# SFT: generate a patch from a crash report
ds = load_dataset("xiaoguangwang/syzfix-dataset", "bug_to_patch")
print(ds["train"][0])           # {"messages": [{"role": "user", ...}, {"role": "assistant", ...}]}

# DPO/ORPO: preference pairs (better patch vs worse patch)
dpo = load_dataset("xiaoguangwang/syzfix-dataset", "dpo")
print(dpo["train"][0])          # {"prompt": ..., "chosen": ..., "rejected": ...}
```

Available configs:

| Config | Type | Description |
|--------|------|-------------|
| `bug_to_patch` | SFT | Crash report → patch diff |
| `patch_review` | SFT | Bug + patch → reviewer critique |
| `patch_improvement` | SFT | Bug + v1 patch → improved patch |
| `dpo` | DPO/ORPO | Preference pairs: better vs worse patch |
| `commit_message` | SFT | Bug + patch → commit message |

### Option B — Restore full processed data and regenerate training sets (~2 GB download)

Do this if you want to change prompt templates, add new task types, or filter
bugs differently. The full rich per-bug data (mailing-list threads, all crash
variants, raw syzbot fields) lives in a single gzipped JSONL on HuggingFace.

```bash
# Downloads processed/processed.jsonl.gz and unpacks into data/processed/
python restore_processed.py --repo xiaoguangwang/syzfix-dataset

# Then regenerate training data with your own settings
python prepare_training.py --tasks all
```

`restore_processed.py` streams the file and never loads everything into RAM at
once, so it works on any machine.

### What you get with each option

| | Option A | Option B |
|---|---|---|
| Fine-tune immediately | ✅ | ✅ (after `prepare_training.py`) |
| Change prompt templates | ❌ | ✅ |
| Add new training tasks | ❌ | ✅ |
| Re-crawl from syzbot | ❌ not needed | ❌ not needed |
| Download size | ~210 MB | ~2 GB |

---

## What the Dataset Contains

Each entry in the dataset corresponds to one fixed kernel bug and includes:

| Field | Source | Description |
|-------|--------|-------------|
| `crash_report` | syzbot | Full kernel oops / KASAN / BUG output |
| `c_reproducer` | syzbot | C program that reproduces the crash |
| `syz_reproducer` | syzbot | Syzkaller syscall description reproducer |
| `fix_commit` | git.kernel.org | Commit hash, message, author, date |
| `final_patch_diff` | git.kernel.org | The actual merged patch diff |
| `patch_evolution` | lore.kernel.org | v1 → v2 → … patch iterations with inline diffs |
| `discussion` | lore.kernel.org | Full reviewer email threads per patch version |

### Example: A Complete Bug Fix Story

```
[Sep 16, 2024]  syzbot reports: NULL pointer deref in filemap_read_folio
                erofs mounted over a directory instead of a regular file

[Sep 17, 2024]  Developer submits [PATCH v1]
                  - Checks S_ISREG + read_folio != NULL before proceeding

[Sep 17, 2024]  syzbot auto-tests v1 → patch confirmed working ✅

[Sep 17, 2024]  Developer notices v1 misses multi-device/blob case
                Developer submits [PATCH v2]
                  - Also fixes erofs_init_device() for secondary devices

[Oct 11, 2024]  Maintainer Chao Yu: "Reviewed-by" ✅

[Final]         Commit 416a8b2c merged into torvalds/linux
```

---

## Data Sources

| Source | What it provides | API / Access method |
|--------|-----------------|---------------------|
| [syzkaller.appspot.com](https://syzkaller.appspot.com/upstream/fixed) | Bug list, crash reports, reproducers, fix commit links | JSON API (`?json=1`) + HTML scraping |
| [lore.kernel.org](https://lore.kernel.org) | Full mailing list discussion threads, patch versions | mbox download (`/t.mbox.gz`) |
| [git.kernel.org](https://git.kernel.org) | Actual patch diffs | cgit patch view (`/patch/?id=<hash>`) |
| [patchwork.kernel.org](https://patchwork.kernel.org) | Patch series, version tracking (fallback) | REST API (`/api/1.2/`) |

---

## Project Structure

```
SyzFix/
├── venv/                    # Python virtual environment (project-wide)
├── syzbot-dataset/          # Data collection & training-data pipeline
│   ├── main.py              # CLI entry point (collect / export / stats / inspect)
│   ├── view.py              # Interactive dataset explorer
│   ├── retry_missing.py     # Retry failed fetches; show completeness report
│   ├── prepare_training.py  # Convert processed data → training-format JSONL
│   ├── training_config.py   # Prompt templates and task definitions
│   ├── upload_hf.py         # Upload dataset to HuggingFace Hub
│   ├── restore_processed.py # Download & unpack processed data from HF
│   ├── config.py            # All URLs, rate limits, paths
│   ├── models.py            # Data models (BugEntry, FixCommit, Discussion, …)
│   ├── utils.py             # Async HTTP client with rate limiting, retry, cache
│   ├── storage.py           # SQLite progress tracking + JSON file storage
│   ├── pipeline.py          # Main pipeline orchestrating all scrapers
│   ├── export.py            # Export to JSONL / HuggingFace Dataset
│   ├── requirements.txt
│   └── scraper/
│       ├── syzbot.py        # Syzbot JSON API + HTML scraper
│       ├── git_kernel.py    # git.kernel.org patch diff fetcher
│       ├── lore.py          # lore.kernel.org mbox downloader & parser
│       └── patchwork.py     # patchwork.kernel.org fallback scraper
└── analysis/                # Dataset analysis (no LLM APIs required)
    ├── run_all.py           # CLI entry point for all analyses
    ├── loader.py            # Data loading, models, iterators
    ├── filters.py           # Noise filtering (bots, stable-review, trivial tags)
    └── analyzers/
        ├── base.py              # BaseAnalyzer ABC + AnalysisResult
        ├── revision_reasons.py  # Why patches need revision (12 categories)
        ├── discussion_lessons.py# Lessons from human review discussion
        ├── non_functional.py    # Non-feature revision issues (perf, style, etc.)
        └── patch_diff_analysis.py # Structural v1→v2 diff comparison
```

---

## Quick Start

### 1. Install dependencies

```bash
# Create venv at project root (shared by all subpackages)
python3 -m venv venv
source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r syzbot-dataset/requirements.txt
```

### 2. Collect data

```bash
cd syzbot-dataset

# Test with 10 bugs first
python main.py collect --limit 10

# Full collection (~7000 bugs, supports resume)
python main.py collect

# Skip patchwork fallback for faster collection
python main.py collect --skip-patchwork
```

### 3. Monitor progress

```bash
python main.py stats
```

Example output:
```
==================================================
Dataset Statistics  (3 736 / 6 982 bugs collected)
==================================================
With crash report:    3726 (99.9%)
With C reproducer:    2792 (74.9%)
With patch diff:      2813 (75.3%)
With discussion:      2823 (75.7%)
With patch evolution:  676 (18.1%)
==================================================

Pipeline Progress:
  pending  : 3244  █████████████
  processed: 3738  ████████████████
```

### 4. Explore the dataset interactively

```bash
# Browse all bugs — V=patch versions, P=has patch, D=has discussion
python view.py list
python view.py list --has-evolution          # only bugs with v1→v2+ history
python view.py list --subsystem net -n 20   # filter by keyword

# Full lifecycle for one bug
python view.py show <bug_id>

# Individual sections
python view.py crash   <bug_id>              # kernel crash report + C reproducer
python view.py patch   <bug_id>              # final merged patch diff
python view.py patch   <bug_id> --version 1  # specific patch version
python view.py discuss <bug_id>              # email review thread
python view.py discuss <bug_id> -v 2         # only v2 discussion
python view.py diff    <bug_id>              # v1 → v2 → final side-by-side

# Search and discover
python view.py search "use-after-free"
python view.py random
```

### 5. Check data completeness & retry failures

```bash
# Show what's missing and why
python retry_missing.py stats

# Retry bugs where the patch diff fetch failed (network/repo issues)
python retry_missing.py patches
python retry_missing.py patches --limit 100   # batch
```

### 6. Export the dataset

```bash
# JSONL (one JSON object per line, compatible with most fine-tuning tools)
python main.py export --format jsonl

# HuggingFace Dataset format
python main.py export --format huggingface
```

### 7. Upload to HuggingFace Hub

```bash
# Login once
hf auth login

# 1. Flat structured export (research / analysis)
python upload_hf.py --repo YOUR_USERNAME/syzfix-dataset

# 2. Training-format JSONL files (5 task configs, ~210 MB, streamed — no RAM spike)
python upload_hf.py --repo YOUR_USERNAME/syzfix-dataset --training

# 3. Full processed data (needed for collaborators to run prepare_training.py, ~2 GB)
python upload_hf.py --repo YOUR_USERNAME/syzfix-dataset --processed

# Preview what would be uploaded without actually uploading
python upload_hf.py --repo YOUR_USERNAME/syzfix-dataset --training --dry-run
```

### 8. Inspect a single bug via CLI

```bash
python main.py inspect <bug_id>
python main.py inspect 001306cd9c92ce0df23f
```

---

## Dataset Format

Each exported JSONL entry looks like:

```json
{
  "bug_id": "001306cd9c92ce0df23f",
  "title": "BUG: unable to handle kernel NULL pointer dereference in filemap_read_folio",
  "crash_report": "BUG: kernel NULL pointer dereference...\nCall Trace:\n filemap_read_folio+0x14b...",
  "c_reproducer": "#include <stdio.h>\n...",
  "syz_reproducer": "r0 = openat(0xffffffffffffff9c, ...)",
  "fix_commit_hash": "416a8b2c02fe2a5a9fbdf2a35ea294b78d939f84",
  "fix_commit_message": "erofs: ensure regular inodes for file-backed mounts",
  "final_patch_diff": "diff --git a/fs/erofs/super.c ...",
  "patch_evolution": [
    {
      "version": 1,
      "subject": "[PATCH] erofs: ensure regular inodes for file-backed mounts",
      "diff": "diff --git a/fs/erofs/super.c ...",
      "discussion": [
        {"from": "syzbot@...", "date": "...", "body": "syzbot found the following issue..."},
        {"from": "hsiangkao@linux.alibaba.com", "date": "...", "body": "[PATCH v1] ..."},
        {"from": "syzbot@...", "date": "...", "body": "patch confirmed working ✅"}
      ]
    },
    {
      "version": 2,
      "subject": "[PATCH v2] erofs: ensure regular inodes for file-backed mounts",
      "diff": "diff --git a/fs/erofs/super.c ...",
      "discussion": [
        {"from": "chao@kernel.org", "date": "...", "body": "Reviewed-by: Chao Yu"}
      ]
    }
  ],
  "subsystem": "",
  "first_crash_date": "2024-09-13T...",
  "fix_date": "2024-10-17T...",
  "num_patch_versions": 2,
  "has_discussion": true
}
```

---

## Fine-tuning Usage

The training data is pre-formatted and available directly on HuggingFace.
Use `prepare_training.py` only if you want to customise prompt templates or add new tasks.

### Quickstart with pre-built training data

```python
from datasets import load_dataset

# SFT — crash report → patch
ds = load_dataset("xiaoguangwang/syzfix-dataset", "bug_to_patch")
train = ds["train"]   # ~4 200 examples, each {"messages": [...]}

# DPO/ORPO — preference pairs
dpo = load_dataset("xiaoguangwang/syzfix-dataset", "dpo")
# each record: {"prompt": ..., "chosen": ..., "rejected": ...}

# Other tasks
review      = load_dataset("xiaoguangwang/syzfix-dataset", "patch_review")
improvement = load_dataset("xiaoguangwang/syzfix-dataset", "patch_improvement")
commits     = load_dataset("xiaoguangwang/syzfix-dataset", "commit_message")
```

### Regenerate or customise training data

```bash
# Restore the full processed data from HF first
python restore_processed.py --repo xiaoguangwang/syzfix-dataset

# Generate all five tasks (SFT + DPO) into data/training/
python prepare_training.py --tasks all

# Generate a specific task only
python prepare_training.py --tasks bug_to_patch
python prepare_training.py --tasks dpo
```

Available tasks: `bug_to_patch`, `patch_review`, `patch_improvement`, `dpo`, `commit_message`

### Using the raw flat export

```python
import json

with open("data/dataset/syzbot_dataset.jsonl") as f:
    samples = [json.loads(line) for line in f]

# Simple instruction-following format (manual)
training_data = [
    {
        "instruction": f"Fix this Linux kernel bug:\n\n{s['crash_report']}",
        "output": s["final_patch_diff"],
    }
    for s in samples
    if s["crash_report"] and s["final_patch_diff"]
]
print(f"{len(training_data)} training pairs")
```

---

## Rate Limits & Politeness

The scraper respects public infrastructure with conservative rate limits:

| Domain | Rate | Notes |
|--------|------|-------|
| syzkaller.appspot.com | 0.25 req/s | 1 request per 4 seconds |
| lore.kernel.org | 1 req/s | |
| git.kernel.org | 1 req/s | |
| patchwork.kernel.org | 1 req/s | |

Full collection of ~7000 bugs takes approximately **8–10 hours**.

---

## Resuming Interrupted Runs

Progress is tracked in `data/progress.db` (SQLite). Simply re-run:

```bash
python main.py collect
```

It will skip already-processed bugs and continue from where it left off.

---

## Dataset Analysis

The `analysis/` module provides keyword/heuristic-based insights into the dataset — no LLM APIs required.

```bash
# Run all analyzers
python -m analysis.run_all

# Run a specific analyzer
python -m analysis.run_all --analyzer revision

# Quick test on a random sample
python -m analysis.run_all --sample 500

# List available analyzers
python -m analysis.run_all --list
```

### Available Analyzers

| Analyzer | What it answers |
|----------|----------------|
| `revision` | Why do patches need revision? Classifies into 12 categories (correctness, incomplete fix, race condition, performance, style, etc.) |
| `discussion` | Lessons from human review: top reviewers, discussion depth, feedback themes, subsystem breakdown |
| `nonfunctional` | Are there revisions purely for non-feature issues? (performance, coding style, commit hygiene, build/config) |
| `patchdiff` | How do patches change structurally between v1 and v2? (size, file scope, growth vs shrink) |

### Adding New Analyzers

Create a new file in `analysis/analyzers/`, subclass `BaseAnalyzer`, implement `analyze()`, and register it in `run_all.py`:

```python
from analysis.analyzers.base import BaseAnalyzer, AnalysisResult

class MyAnalyzer(BaseAnalyzer):
    @property
    def name(self) -> str:
        return "My Custom Analysis"

    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        # Your analysis logic here
        return AnalysisResult(name=self.name, summary={...})
```

Results are saved to `analysis/results/` as JSON and CSV files.

---

## Corner Cases Handled

- **Missing patch hash** — falls back to lore search by commit title
- **Google Groups links** — skipped (can't fetch mbox); lore links used instead
- **Non-lore discussion URLs** — filtered out automatically
- **429 rate limiting** — exponential backoff retry (up to 3 retries)
- **Very large threads** — truncated to 200 emails max
- **Multi-repo commits** — tries torvalds/linux, then net, net-next, bpf, bpf-next
- **Bugs without fix commits** — collected anyway (crash report + discussion still useful)
- **Both syzbot link formats** — handles both `?extid=` and `?id=` bug URLs with automatic fallback

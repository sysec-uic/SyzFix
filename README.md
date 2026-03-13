# PatchWeaver

**PatchWeaver** is a dataset builder that collects fixed Linux kernel bugs from
[syzbot/syzkaller](https://syzkaller.appspot.com/upstream/fixed) and assembles a
structured dataset capturing the **full bug-fix lifecycle** — from the initial crash
report through patch iterations and reviewer discussions to the final merged commit.

The dataset is intended for:
- **Understanding** how real Linux kernel bugs get diagnosed and fixed
- **Fine-tuning** language models to generate better kernel bug fixes
- **Researching** patch evolution patterns in the Linux development process

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
PatchWeaver/
└── syzbot-dataset/
    ├── main.py              # CLI entry point
    ├── config.py            # All URLs, rate limits, paths
    ├── models.py            # Data models (BugEntry, FixCommit, Discussion, …)
    ├── utils.py             # Async HTTP client with rate limiting, retry, cache
    ├── storage.py           # SQLite progress tracking + JSON file storage
    ├── pipeline.py          # Main pipeline orchestrating all scrapers
    ├── export.py            # Export to JSONL / HuggingFace Dataset
    ├── requirements.txt
    └── scraper/
        ├── syzbot.py        # Syzbot JSON API + HTML scraper
        ├── git_kernel.py    # git.kernel.org patch diff fetcher
        ├── lore.py          # lore.kernel.org mbox downloader & parser
        └── patchwork.py     # patchwork.kernel.org fallback scraper
```

---

## Quick Start

### 1. Install dependencies

```bash
cd syzbot-dataset
python3 -m venv venv
source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Collect data

```bash
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
Dataset Statistics
==================================================
Total processed bugs: 3546
With crash report:    3541 (99.9%)
With final patch:     2645 (74.6%)
With discussion:      2638 (74.4%)
With patch evolution:  658 (18.6%)
With reproducer:      2807 (79.2%)
==================================================
```

### 4. Export the dataset

```bash
# JSONL (one JSON object per line, compatible with most fine-tuning tools)
python main.py export --format jsonl

# HuggingFace Dataset format
python main.py export --format huggingface

# Custom output path
python main.py export --format jsonl --output /path/to/output.jsonl
```

### 5. Inspect a single bug

```bash
python main.py inspect <bug_id>
# Example:
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

### Basic: crash → patch

```python
import json

with open("data/dataset/syzbot_dataset.jsonl") as f:
    samples = [json.loads(line) for line in f]

# Simple instruction-following format
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

### Advanced: with patch discussion context

```python
# Use patch evolution for multi-turn or chain-of-thought training
rich_samples = [s for s in samples if s["num_patch_versions"] > 1]

for s in rich_samples:
    messages = [
        {"role": "user", "content": f"Bug report:\n{s['crash_report']}"},
    ]
    for pv in s["patch_evolution"]:
        # Add each reviewer comment as a turn
        for msg in pv["discussion"]:
            messages.append({"role": "assistant" if "patch" in msg["subject"].lower() else "user",
                             "content": msg["body"]})
    messages.append({"role": "assistant", "content": s["final_patch_diff"]})
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

## Corner Cases Handled

- **Missing patch hash** — falls back to lore search by commit title
- **Google Groups links** — skipped (can't fetch mbox); lore links used instead
- **Non-lore discussion URLs** — filtered out automatically
- **429 rate limiting** — exponential backoff retry (up to 3 retries)
- **Very large threads** — truncated to 200 emails max
- **Multi-repo commits** — tries torvalds/linux, then net, net-next, bpf, bpf-next
- **Bugs without fix commits** — collected anyway (crash report + discussion still useful)

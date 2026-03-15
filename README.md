# SyzFix

**SyzFix** collects fixed Linux kernel bugs from [syzbot](https://syzkaller.appspot.com/upstream/fixed)
and assembles a structured dataset capturing the **full bug-fix lifecycle** — from the initial crash
report through patch iterations and reviewer discussions to the final merged commit.

Intended for fine-tuning language models to generate and review kernel patches,
and for researching patch evolution patterns in the Linux development process.

> **Code:** https://github.com/sysec-uic/syzfix  
> **Dataset:** https://huggingface.co/datasets/xiaoguangwang/syzfix-dataset

---

## Install

```bash
git clone https://github.com/sysec-uic/syzfix.git
cd syzfix
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

## Documentation

| | |
|---|---|
| [**Reproducing without re-crawling**](docs/reproducing.md) | Use the pre-built HF dataset to start training in minutes |
| [**Exploring the dataset**](docs/exploring.md) | Browse, search, and inspect individual bugs interactively |
| [**Training guide**](docs/training.md) | SFT, DPO, prompt customisation, TRL examples |
| [**Analysis**](docs/analysis.md) | Heuristic analyzers and the iteration timeline figure |
| [**Data collection**](docs/collection.md) | Full crawl pipeline, rate limits, resuming, upload (**optional**) |

---

## What the dataset contains

Each entry captures one fixed kernel bug end-to-end:

| Field | Source | Description |
|-------|--------|-------------|
| `crash_report` | syzbot | Full kernel oops / KASAN / BUG output |
| `c_reproducer` | syzbot | C program that reproduces the crash |
| `syz_reproducer` | syzbot | Syzkaller syscall description |
| `fix_commit` | git.kernel.org | Commit hash, message, author, date |
| `final_patch_diff` | git.kernel.org | The merged patch diff |
| `patch_evolution` | lore.kernel.org | v1 → v2 → … diffs with inline discussions |
| `discussion` | lore.kernel.org | Full reviewer email threads per version |

**~6,900 bugs** collected, of which ~5,200 have a patch diff and ~5,000 have
mailing-list discussions. Up to 9 patch versions captured per bug.

### Example

```
[Sep 16, 2024]  syzbot: NULL pointer deref in filemap_read_folio

[Sep 17, 2024]  Developer → [PATCH v1]: check S_ISREG before proceeding

[Sep 17, 2024]  syzbot: patch confirmed working ✅

[Sep 17, 2024]  Developer → [PATCH v2]: also fix multi-device/blob case

[Oct 11, 2024]  Chao Yu: Reviewed-by ✅

[Final]         Commit 416a8b2c merged into torvalds/linux
```

---

## Data sources

| Source | What it provides |
|--------|-----------------|
| [syzkaller.appspot.com](https://syzkaller.appspot.com/upstream/fixed) | Bug list, crash reports, reproducers, fix commit links |
| [lore.kernel.org](https://lore.kernel.org) | Mailing list threads, patch versions |
| [git.kernel.org](https://git.kernel.org) | Patch diffs |
| [patchwork.kernel.org](https://patchwork.kernel.org) | Patch series fallback |

---

## Project structure

```
SyzFix/
├── requirements.txt             # All dependencies (crawler + analysis)
├── docs/                        # Extended documentation
│   ├── reproducing.md           # Use HF dataset without re-crawling
│   ├── training.md              # Fine-tuning guide (SFT, DPO, TRL)
│   ├── analysis.md              # Analysis scripts and plots
│   └── collection.md            # Full crawl pipeline reference
├── syzbot-dataset/              # Data collection & training-data pipeline
│   ├── main.py                  # collect / export / stats / inspect
│   ├── view.py                  # Interactive dataset explorer
│   ├── pipeline.py              # Orchestrates all scrapers
│   ├── prepare_training.py      # Processed data → training JSONL
│   ├── training_config.py       # Prompt templates and task definitions
│   ├── upload_hf.py             # Upload to HuggingFace Hub
│   ├── restore_processed.py     # Download processed data from HF
│   ├── retry_missing.py         # Retry failed fetches
│   └── scraper/
│       ├── syzbot.py            # syzbot JSON API + HTML
│       ├── git_kernel.py        # git.kernel.org patch diffs
│       ├── lore.py              # lore.kernel.org mbox threads
│       └── patchwork.py         # patchwork fallback
└── analysis/                    # Heuristic dataset analysis
    ├── run_all.py               # Run all analyzers
    ├── plot_iteration_timeline.py  # Figure 1: patch iteration timeline
    ├── loader.py
    ├── filters.py
    └── analyzers/
        ├── revision_reasons.py
        ├── discussion_lessons.py
        ├── non_functional.py
        └── patch_diff_analysis.py
```

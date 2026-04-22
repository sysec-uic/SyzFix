# SyzFix

**SyzFix** collects fixed Linux kernel bugs from [syzbot](https://syzkaller.appspot.com/upstream/fixed)
and assembles a structured dataset capturing the **full bug-fix lifecycle** — from the initial crash
report through patch iterations and reviewer discussions to the final merged commit.

Intended for fine-tuning language models to generate and review kernel patches,
for researching patch evolution patterns in the Linux development process,
and for studying **cross-layer kernel bugs** — bugs where the crash occurs in one
architectural layer but the fix belongs in another (e.g., a specific filesystem crash
fixed in the VFS layer).

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

## Quick Start

```bash
# Download and unpack the full per-bug JSON files (~2GB download, ~11GB unpacked)
python -m dataset.restore_processed --repo xiaoguangwang/syzfix-dataset

# Build a lightweight index (~4 MB) instead of loading all 11 GB of processed data on every data viewer call
python -m dataset.view build-index

# List all bugs — V=patch versions, P=has patch, R=has C reproducer, D=has discussion
python -m dataset.view list
# Only bugs with a C reproducer (needed for crash reproduction)
python -m dataset.view list --has-reproducer

# Run all analyzers (Heuristic analyzers)
python -m analysis.run_all

# Print previously saved results without re-running (instant)
python -m analysis.run_all --show
python -m analysis.run_all --show --analyzer revision
```
---

## Documentation

| | |
|---|---|
| [**Reproducing without re-crawling**](docs/reproducing.md) | Use the pre-built HF dataset to start training in minutes |
| [**Exploring the dataset**](docs/exploring.md) | Browse, search, and inspect individual bugs interactively |
| [**Analysis**](docs/analysis.md) | Heuristic analyzers and the iteration timeline figure |
| [**Cross-layer analysis**](docs/cross_layer.md) | Cross-layer bugs, stack-overlap verification, kernel layer taxonomy |
| [**Memory system**](docs/memory.md) | RAG knowledge base for agent-based kernel bug fixing |
| [**Evaluation**](docs/evaluation.md) | Reproduce crashes, generate fixes, and verify patches end-to-end [TODO: untested] |
| [**Training guide**](docs/training.md) | SFT, DPO, prompt customisation, TRL examples [TODO: untested] |
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
| linux-stable.git (optional) | 10+ years of stable/LTS cherry-picks for ground-truth backport analysis |

---

## Project structure

The repository is split into a stable **dataset core**, shared **infrastructure**
packages (consumed by every paper), and **projects/** — one subdirectory per
research paper.

```
SyzFix/
├── requirements.txt          # All dependencies (crawler + analysis + memory)
├── docs/                     # Extended documentation (see table above)
├── dataset/                  # Dataset collection, storage, viewer, HF upload
│   ├── main.py               # collect / export / stats / inspect
│   ├── view.py               # Interactive dataset explorer
│   ├── pipeline.py           # Orchestrates all scrapers
│   ├── upload_hf.py          # Upload to HuggingFace Hub
│   ├── restore_processed.py  # Download processed data from HF
│   ├── retry_missing.py      # Retry failed fetches
│   ├── data/                 # (gitignored) processed/, raw/, training/, index.jsonl
│   └── scraper/              # syzbot.py, git_kernel.py, lore.py, patchwork.py, stable_cherrypick.py
├── analysis/                 # Heuristic dataset analysis (shared)
│   ├── run_all.py            # Run all analyzers
│   ├── plot_iteration_timeline.py
│   ├── loader.py, filters.py
│   └── analyzers/            # revision_reasons, bug_type, fix_pattern, kernel_layers, cross_layer, …
├── memory/                   # RAG knowledge base for LLM agents (shared)
│   ├── build.py, retrieve.py, schemas.py, embeddings.py, store.py
│   ├── knowledge/            # Git-tracked distilled rules and pattern knowledge
│   └── data/                 # (gitignored) FAISS indices, instance memory, embeddings
├── evaluation/               # Crash reproduction + agent-fix pipeline (shared)
│   ├── reproduce_crash.py, generate_fix.py, run_eval.py, fetch_cases.py
│   ├── agents/               # claude_code, opencode, codex adapters
│   ├── scripts/, docker/     # Host & container build scripts
│   └── cases/, kernel/, ccache/, results/   # (gitignored) build + run artifacts
├── training/                 # Training-data prep + fine-tuning (shared)
│   ├── prepare_training.py   # Processed data → training JSONL (5 tasks + crash_to_patch_location)
│   ├── training_config.py    # Prompt templates and task definitions
│   ├── train_patch_location.py, eval_patch_location.py
├── projects/                 # One subdirectory per research paper
│   ├── cross-layer/          # Cross-layer bug analysis + patch-location paper
│   └── patch-evolution/      # SyzFix dataset + memory-augmented fixing paper
└── tests/
    └── test_memory_retrieval.py   # End-to-end retrieval demo & test
```

## Research projects

| Project | Topic |
|---|---|
| [`projects/cross-layer/`](projects/cross-layer/) | Cross-layer bugs: taxonomy, patch-location prediction |
| [`projects/patch-evolution/`](projects/patch-evolution/) | Dataset overview + memory-augmented bug-fix lifecycle |

Shared code lives at the repo root; each project's `README.md` lists which
shared packages it consumes.

---

## Cross-layer analysis

Some kernel bugs crash in one architectural layer but need to be fixed in another.
Of 4,983 analyzed bugs, **466 (9.4%) are cross-layer** — and of those,
**130 (27.9%) have the fix completely off the crash stack**, making them the
hardest cases for LLM-based bug localization.

```bash
python -m dataset.view stats                    # full breakdown
python -m dataset.view list --true-cross-layer   # the 130 hardest cases
python -m dataset.view crosslayer <bug_id>       # per-bug analysis
```

→ **[Full documentation](docs/cross_layer.md)**: dataset breakdown, stack-overlap
verification, kernel layer taxonomy, examples, and all commands.

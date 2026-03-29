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
| [**Evaluation**](docs/evaluation.md) | Reproduce crashes, generate fixes, and verify patches end-to-end |
| [**Analysis**](docs/analysis.md) | Heuristic analyzers and the iteration timeline figure |
| [**Memory system**](docs/memory.md) | RAG knowledge base for agent-based kernel bug fixing |
| [**Training guide**](docs/training.md) | SFT, DPO, prompt customisation, TRL examples |
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

```
SyzFix/
├── requirements.txt             # All dependencies (crawler + analysis)
├── docs/                        # Extended documentation
│   ├── reproducing.md           # Use HF dataset without re-crawling
│   ├── exploring.md             # Browse and search bugs with the dataset viewer
│   ├── evaluation.md            # Reproduce crashes and verify patches end-to-end
│   ├── training.md              # Fine-tuning guide (SFT, DPO, TRL)
│   ├── analysis.md              # Analysis scripts and plots
│   ├── memory.md                # Memory system for agent-based bug fixing
│   └── collection.md            # Full crawl pipeline reference
├── evaluation/                  # End-to-end crash reproduction & fix evaluation
│   ├── reproduce_crash.py       # Reproduce a crash; optionally verify a patch
│   ├── generate_fix.py          # Generate a fix patch via a coding agent
│   ├── run_eval.py              # Batch evaluation orchestrator (agent loop)
│   ├── fetch_cases.py           # Fetch fresh test cases from live syzbot
│   ├── agents/                  # Pluggable coding agent implementations
│   │   ├── base.py              # Abstract CodingAgent + prompt builder
│   │   ├── claude_code.py       # Claude Code CLI agent
│   │   ├── opencode.py          # OpenCode CLI agent
│   │   └── codex.py             # OpenAI Codex CLI agent
│   ├── scripts/                 # Host-side shell scripts (build + QEMU)
│   │   ├── setup_host.sh        # Install host dependencies (run once, as root)
│   │   ├── create_rootfs.sh     # Build minimal busybox initramfs (run once)
│   │   ├── reproduce.sh         # Build kernel, run reproducer in QEMU
│   │   └── verify_fix.sh        # Apply patch, rebuild, confirm crash is gone
│   ├── docker/                  # Docker-based execution environment (optional)
│   │   ├── Dockerfile           # Ubuntu 24.04 + gcc/clang/qemu
│   │   ├── reproduce.sh         # Docker counterpart of scripts/reproduce.sh
│   │   └── verify_fix.sh        # Docker counterpart of scripts/verify_fix.sh
│   ├── cases/                   # Per-bug test case directories (auto-created)
│   │   └── <bug_id>/            # case.json, reproducer.c, kernel.config, …
│   ├── results/                 # Agent-generated patches and evaluation results
│   ├── kernel/                  # Cached Linux kernel source (bind-mounted)
│   └── ccache/                  # Compiler cache (shared across runs)
├── memory/                      # RAG knowledge base for LLM agents
│   ├── build.py                 # Extract & index all bug-fix knowledge
│   ├── retrieve.py              # MemoryRetriever: similarity + structured search
│   ├── schemas.py               # BugMemoryEntry, FixStrategy, ReviewLesson, …
│   ├── embeddings.py            # BAAI/bge-base-en-v1.5 embedding wrapper
│   ├── store.py                 # Persistence (JSONL, JSON, numpy, FAISS)
│   └── data/                    # Generated artifacts (gitignored)
│       ├── instance_memory.jsonl
│       ├── pattern_memory.json
│       ├── inverted_indices.json
│       ├── faiss_crash.index
│       └── export/pattern_knowledge.md
├── test_memory_retrieval.py     # End-to-end retrieval demo & test
├── syzbot-dataset/              # Data collection & training-data pipeline
│   ├── main.py                  # collect / export / stats / inspect
│   ├── view.py                  # Interactive dataset explorer
│   ├── pipeline.py              # Orchestrates all scrapers
│   ├── prepare_training.py      # Processed data → training JSONL
│   ├── training_config.py       # Prompt templates and task definitions
│   ├── upload_hf.py             # Upload to HuggingFace Hub
│   ├── restore_processed.py     # Download processed data from HF
│   ├── retry_missing.py         # Retry failed fetches
│   ├── data/
│   │   ├── processed/           # Per-bug JSON files (~6,900 bugs, 11 GB)
│   │   └── index.jsonl          # Lightweight fast-lookup index (~4 MB, auto-built)
│   └── scraper/
│       ├── syzbot.py            # syzbot JSON API + HTML
│       ├── git_kernel.py        # git.kernel.org patch diffs
│       ├── lore.py              # lore.kernel.org mbox threads
│       ├── patchwork.py         # patchwork fallback
│       └── stable_cherrypick.py # Extract cherry-pick map from linux-stable.git
└── analysis/                    # Heuristic dataset analysis
    ├── run_all.py               # Run all analyzers (13 total)
    ├── plot_iteration_timeline.py  # Figure 1: patch iteration timeline
    ├── loader.py
    ├── filters.py
    └── analyzers/
        ├── revision_reasons.py
        ├── discussion_lessons.py
        ├── non_functional.py
        ├── patch_diff_analysis.py
        ├── fix_patterns.py
        ├── fix_locality.py
        ├── difficulty_stratification.py
        ├── information_sufficiency.py
        ├── case_study_finder.py
        ├── insight_clusters.py
        ├── patch_evolution.py
        ├── backport_downstream.py   # Backport signals from discussion threads
        └── backport_comparison.py   # Ground-truth comparison vs. linux-stable.git
```

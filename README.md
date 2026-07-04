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
pip install -e .
```

The editable install exposes two packages, `dataset` (collection pipeline,
viewer, HuggingFace sync) and `analysis` (heuristic analyzers), so downstream
projects can `pip install -e path/to/syzfix` and import them directly.

---

## Quick Start

```bash
# Download and unpack the full per-bug JSON files (~2GB download, ~11GB unpacked)
python -m dataset.restore_processed --repo xiaoguangwang/syzfix-dataset

# Build a lightweight index (~4 MB) instead of loading all 11 GB of processed data on every data viewer call
python -m dataset.view build-index

# List all bugs — V=patch versions, P=has patch, R=has C reproducer, D=has discussion
python -m dataset.view list

# Run all analyzers and show the results
python -m analysis.run_all
python -m analysis.run_all --show
```

Data lives in `dataset/data/` by default; set `SYZFIX_DATA_DIR` to point at a
data directory elsewhere (and `SYZFIX_RESULTS_DIR` for saved analyzer results).

---

## Command reference

### Getting & updating the data

| Command | What it does |
|---|---|
| `python -m dataset.restore_processed --repo xiaoguangwang/syzfix-dataset` | Download the pre-built dataset from HuggingFace (no crawling) |
| `python -m dataset.update` | **One-command incremental update**: pull only new bugs from syzbot, rebuild the index, refresh analyzer results |
| `python -m dataset.update --repo <user>/syzfix-dataset` | Same, then upload to HuggingFace (skipped automatically when nothing is new) |
| `python -m dataset.main collect` | Raw crawl pipeline (resumable; `--limit N`, `--no-resume`, `--skip-patchwork`) |
| `python -m dataset.retry_missing stats\|patches\|crashes` | Diagnose and backfill bugs with missing patches / crash reports |

The collect pipeline is incremental by design: progress is tracked per bug in
`dataset/data/progress.db`, the refreshed syzbot bug list is merged without
touching completed entries, and only unprocessed bugs are fetched. A periodic
`python -m dataset.update --repo …` therefore downloads just the delta.
→ [docs/collection.md](docs/collection.md)

### Exploring the dataset

| Command | What it does |
|---|---|
| `python -m dataset.view list` | List bugs (`--has-reproducer`, `--cross-layer`, `--true-cross-layer`, …) |
| `python -m dataset.view show <bug_id>` | Full record for one bug |
| `python -m dataset.view crash\|patch\|discuss <bug_id>` | Crash report / patch diffs / review thread |
| `python -m dataset.view diff <bug_id>` | What changed between patch versions |
| `python -m dataset.view search <query>` | Search titles and crash signatures |
| `python -m dataset.view random` | A random interesting bug |
| `python -m dataset.view stats` | Dataset & cross-layer statistics |
| `python -m dataset.view crosslayer <bug_id>` | Per-bug cross-layer classification |
| `python -m dataset.view build-index` | Rebuild the fast-lookup index after new data |

→ [docs/exploring.md](docs/exploring.md)

### Analysis

| Command | What it does |
|---|---|
| `python -m analysis.run_all` | Run all 13 heuristic analyzers (`--analyzer X`, `--sample N`) |
| `python -m analysis.run_all --show` | Print previously saved results instantly |
| `python -m analysis.plot_iteration_timeline` | Patch-iteration timeline figure (PNG/PDF/SVG) |
| `python -m analysis.generate_case_study <bug_id>` | Paper-ready case-study narrative for a bug |
| `python -m analysis.run_cross_layer_modes` | Cross-layer counts under strict/relax mode grid |
| `python -m analysis.audit_cross_layer <bug_id>` | Decision trace of the cross-layer classifier |
| `python -m analysis.viz_layer_bug <bug_id>` | Self-contained HTML layer visualization per bug |

→ [docs/analysis.md](docs/analysis.md), [docs/cross_layer.md](docs/cross_layer.md)

### Publishing to HuggingFace

| Command | What it does |
|---|---|
| `python -m dataset.upload_hf --repo <user>/syzfix-dataset` | Upload the flat structured export |
| `python -m dataset.upload_hf --repo … --processed` | Pack & upload the full per-bug data (~2 GB gz, streamed) |
| `python -m dataset.upload_hf --repo … --training` | Upload training-task JSONLs (built in the research repo) |
| `python -m dataset.main export --format jsonl\|huggingface` | Local export only, no upload |

All upload commands support `--dry-run` and `--private`. Login once with
`hf auth login`. → [docs/collection.md](docs/collection.md)

---

## Documentation

| | |
|---|---|
| [**Reproducing without re-crawling**](docs/reproducing.md) | Use the pre-built HF dataset to start exploring in minutes |
| [**Exploring the dataset**](docs/exploring.md) | Browse, search, and inspect individual bugs interactively |
| [**Analysis**](docs/analysis.md) | Heuristic analyzers and the iteration timeline figure |
| [**Cross-layer analysis**](docs/cross_layer.md) | Cross-layer bugs, stack-overlap verification, kernel layer taxonomy |
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

**7,000+ bugs** collected (7,091 as of July 2026 — the dataset grows with
each `dataset.update` run), of which 5,127 have a patch diff, 6,057 have
mailing-list discussions, and 4,742 have a C reproducer. Up to 9 patch
versions captured per bug.

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
syzfix/
├── pyproject.toml            # Installable package: dataset + analysis
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
└── analysis/                 # Heuristic dataset analysis
    ├── run_all.py            # Run all analyzers
    ├── plot_iteration_timeline.py
    ├── loader.py, filters.py, paths.py
    └── analyzers/            # revision_reasons, bug_type, fix_pattern, kernel_layers, cross_layer, …
```

Downstream research built on this dataset — the RAG memory system for
LLM-agent kernel bug fixing, crash-reproduction/evaluation harness, and
fine-tuning recipes — lives in a separate research repository that consumes
this package via `pip install`.

---

## Cross-layer analysis

Some kernel bugs crash in one architectural layer but need to be fixed in another.
Of 5,067 analyzed bugs, **574 (11.3%) are cross-layer** and a further
**465 (9.2%) are cross-domain**; among the cross-layer bugs, **215 (37.5%)
have the fix completely off the crash stack**, making them the hardest cases
for LLM-based bug localization.

```bash
python -m dataset.view stats                    # full breakdown
python -m dataset.view list --true-cross-layer   # the hardest cases
python -m dataset.view crosslayer <bug_id>       # per-bug analysis
```

→ **[Full documentation](docs/cross_layer.md)**: dataset breakdown, stack-overlap
verification, kernel layer taxonomy, examples, and all commands.

---

## License

Code is released under the [MIT License](LICENSE). The dataset aggregates
publicly available content from syzbot, lore.kernel.org, and git.kernel.org;
see the [HuggingFace dataset card](https://huggingface.co/datasets/xiaoguangwang/syzfix-dataset)
for details.

# SyzFix

**SyzFix** collects fixed Linux kernel bugs from [syzbot](https://syzkaller.appspot.com/upstream/fixed)
and assembles a structured dataset capturing the **full bug-fix lifecycle** — from the initial crash
report through patch iterations and reviewer discussions to the final merged commit.

Intended for fine-tuning language models to generate and review kernel patches,
for researching patch evolution patterns in the Linux development process,
and for studying **cross-layer kernel bugs** — bugs where the crash occurs in one
architectural layer but the fix belongs in another (e.g., a specific filesystem crash
fixed in the VFS layer).

> **Website & Documentation:** [https://sysec-uic.github.io/SyzFix/](https://sysec-uic.github.io/SyzFix/)  
> **Code:** [https://github.com/sysec-uic/SyzFix](https://github.com/sysec-uic/SyzFix)  
> **Dataset:** [https://huggingface.co/datasets/xiaoguangwang/syzfix-dataset](https://huggingface.co/datasets/xiaoguangwang/syzfix-dataset)

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

## Documentation

For full details on the dataset, command references, data sources, project structure, and in-depth cross-layer analysis, please visit our official website:

**👉 [https://sysec-uic.github.io/SyzFix/](https://sysec-uic.github.io/SyzFix/)**

---

## License

Code is released under the [MIT License](LICENSE). The dataset aggregates
publicly available content from syzbot, lore.kernel.org, and git.kernel.org;
see the [HuggingFace dataset card](https://huggingface.co/datasets/xiaoguangwang/syzfix-dataset)
for details.

# Reproducing Without Re-crawling

Collaborators can skip the 8–10 hour crawl entirely. The dataset is already on
HuggingFace; choose how deeply you want to work with it.

## Step 0 — Clone and install

```bash
git clone https://github.com/sysec-uic/syzfix.git
cd syzfix
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

All commands below are run from the **project root** (`syzfix/`).

## Option A — Use training data directly (~210 MB)

Ready-to-use JSONL files in chat/instruction format. No further processing needed.

```python
from datasets import load_dataset

# SFT: crash report → patch
ds = load_dataset("xiaoguangwang/syzfix-dataset", "bug_to_patch")
print(ds["train"][0])   # {"messages": [{"role": "user", ...}, {"role": "assistant", ...}]}

# DPO/ORPO: preference pairs
dpo = load_dataset("xiaoguangwang/syzfix-dataset", "dpo")
print(dpo["train"][0])  # {"prompt": ..., "chosen": ..., "rejected": ...}
```

| Config | Type | Description |
|--------|------|-------------|
| `bug_to_patch` | SFT | Crash report → patch diff |
| `patch_review` | SFT | Bug + patch → reviewer critique |
| `patch_improvement` | SFT | Bug + v1 patch → improved patch |
| `dpo` | DPO/ORPO | Preference pairs: better vs worse patch |
| `commit_message` | SFT | Bug + patch → commit message |

## Option B — Restore full processed data (~2 GB download)

Do this to change prompt templates, add new tasks, or filter bugs differently.

```bash
# Download and unpack the full per-bug JSON files
python -m dataset.restore_processed --repo xiaoguangwang/syzfix-dataset

# Regenerate training tasks (from the syzfix-research repo, which
# consumes this package): python -m training.prepare_training --tasks all
```

`restore_processed` streams the file line-by-line — constant RAM usage.

Once restored, you can browse individual bugs with the interactive viewer — see **[exploring.md](exploring.md)**.

## Updating the dataset with new syzbot bugs

If you maintain the dataset and want to add bugs that syzbot has fixed since
the last crawl, see **[collection.md → Adding new bugs from syzbot](collection.md#adding-new-bugs-from-syzbot-incremental-update)**
for the incremental crawl + rebuild checklist.

## Comparison

| | Option A | Option B |
|---|---|---|
| Fine-tune immediately | ✅ | ✅ (after `prepare_training`) |
| Change prompt templates | ❌ | ✅ |
| Add new training tasks | ❌ | ✅ |
| Re-crawl from syzbot | not needed | not needed |
| Download size | ~210 MB | ~2 GB |

# Data Collection Pipeline

All commands are run from the **project root** (`syzfix/`).

## Install

```bash
git clone https://github.com/sysec-uic/syzfix.git
cd syzfix
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Collect

```bash
# Smoke test (10 bugs)
python -m dataset.main collect --limit 10

# Full collection (~7 000 bugs, 8–10 hours)
python -m dataset.main collect

# Faster: skip patchwork fallback
python -m dataset.main collect --skip-patchwork
```

Progress is saved to `dataset/data/progress.db` (SQLite) — simply re-run to resume.

## Adding new bugs from syzbot (incremental update)

The crawler is incremental by default: `python -m dataset.main collect` refetches
the current syzbot fixed-bug list, merges it into `progress.db` via `INSERT OR IGNORE`,
and only processes bugs that have not yet reached `step=processed`. Existing
processed bugs are skipped; only the delta (new bug IDs since the last run)
is fetched. Watch for a log line like:

```
Refreshed bug list: 119 new bugs since last run (7066 total)
```

After the incremental crawl finishes, rebuild downstream artifacts (none of
them support per-bug incremental updates — they rebuild from the full corpus):

1. **Re-export the flat JSONL**
   ```bash
   python -m dataset.main export --format jsonl
   ```
2. **Re-run the analyzers** (full run, ~2–3 min)
   ```bash
   python -m analysis.run_all
   ```
3. **Rebuild training JSONLs**
   ```bash
   python -m dataset.prepare_training --tasks all
   ```
4. **Rebuild the memory index**
   ```bash
   # Fast path — structured data + trajectories + rules (~3 min)
   python -m memory.build --skip-embeddings

   # Full rebuild with FAISS embeddings (~50 min CPU)
   python -m memory.build
   ```
5. **Re-upload to HuggingFace**
   ```bash
   python -m dataset.upload_hf --repo xiaoguangwang/syzfix-dataset
   python -m dataset.upload_hf --repo xiaoguangwang/syzfix-dataset --training
   python -m dataset.upload_hf --repo xiaoguangwang/syzfix-dataset --processed
   python -m dataset.upload_hf --repo xiaoguangwang/syzfix-dataset --memory
   ```

To force a full re-crawl instead, pass `--no-resume`.

## Monitor

```bash
python -m dataset.main stats
```

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

## Explore interactively

See **[exploring.md](exploring.md)** for the full reference.

## Retry failures

```bash
python -m dataset.retry_missing stats
python -m dataset.retry_missing patches
python -m dataset.retry_missing patches --limit 100
```

## Export

```bash
python -m dataset.main export --format jsonl
python -m dataset.main export --format huggingface
```

## Upload to HuggingFace

```bash
hf auth login

# Flat structured export (research / analysis)
python -m dataset.upload_hf --repo YOUR_USERNAME/syzfix-dataset

# Training-format JSONL (5 task configs, ~210 MB, streamed)
python -m dataset.upload_hf --repo YOUR_USERNAME/syzfix-dataset --training

# Full processed data for collaborators (~2 GB)
python -m dataset.upload_hf --repo YOUR_USERNAME/syzfix-dataset --processed

# Dry run
python -m dataset.upload_hf --repo YOUR_USERNAME/syzfix-dataset --training --dry-run
```

## Rate limits

| Domain | Rate | Notes |
|--------|------|-------|
| syzkaller.appspot.com | 0.25 req/s | 1 request per 4 seconds |
| lore.kernel.org | 1 req/s | |
| git.kernel.org | 1 req/s | |
| patchwork.kernel.org | 1 req/s | |

## Corner cases handled

- **Missing patch hash** — falls back to lore search by commit title
- **Google Groups links** — skipped; lore links used instead
- **Non-lore discussion URLs** — filtered out automatically
- **429 rate limiting** — exponential backoff (up to 3 retries)
- **Very large threads** — truncated to 200 emails max
- **Multi-repo commits** — tries torvalds/linux, then net, net-next, bpf, bpf-next
- **Bugs without fix commits** — collected anyway (crash + discussion still useful)
- **Both syzbot URL formats** — handles `?extid=` and `?id=` with automatic fallback

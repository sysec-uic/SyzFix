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

One command pulls the delta, preps everything locally, and uploads:

```bash
# Local-only: incremental crawl + rebuild index + refresh analyzers
python -m dataset.update

# Same, then upload to HuggingFace (skipped automatically when nothing is new)
python -m dataset.update --repo xiaoguangwang/syzfix-dataset

# Useful flags
python -m dataset.update --skip-analysis          # skip the analyzer refresh
python -m dataset.update --repo … --dry-run       # show upload stats, upload nothing
python -m dataset.update --repo … --force-upload  # upload even with 0 new bugs
```

When no new bugs landed on syzbot, the run costs a single API request and
exits — cheap enough to schedule. `scripts/cron_update.sh` wraps the command
for cron (logs to `dataset/data/cron_update.log`); install it on the machine
that holds the data, e.g. weekly on Monday 06:00:

```cron
0 6 * * 1 SYZFIX_PYTHON=/path/to/venv/bin/python /path/to/syzfix/scripts/cron_update.sh
```

Run it on the data-holding machine rather than GitHub Actions: the runner is
stateless, so it would have to restore the full corpus (~2 GB download,
~11 GB unpacked) and re-upload it on every run just to fetch a handful of
new bugs.

### What it does under the hood

The crawler is incremental by default: it refetches the current syzbot
fixed-bug list, merges it into `progress.db` via `INSERT OR IGNORE`, and only
processes bugs that have not yet reached `step=processed`. Existing processed
bugs are skipped; only the delta (new bug IDs since the last run) is fetched.
Watch for a log line like:

```
Refreshed bug list: 119 new bugs since last run (7066 total)
```

`dataset.update` then rebuilds the downstream artifacts (none of them support
per-bug incremental updates — they rebuild from the full corpus). The manual
equivalents, if you need a single step:

1. **Incremental crawl** — `python -m dataset.main collect`
   (`--no-resume` forces a full re-crawl)
2. **Rebuild the viewer index** — `python -m dataset.view build-index`
3. **Re-run the analyzers** (~2–3 min) — `python -m analysis.run_all`
4. **Upload** — flat export + full processed data:
   ```bash
   python -m dataset.upload_hf --repo xiaoguangwang/syzfix-dataset
   python -m dataset.upload_hf --repo xiaoguangwang/syzfix-dataset --processed
   ```
5. **Research-repo artifacts** — training JSONLs and the memory index are
   rebuilt from **syzfix-research** (see its README), then uploaded with
   `--training` / `--memory` from there. Point those at a **separate** HF
   repo (e.g. `…/syzfix-training`): the main dataset repo holds only the
   raw data, and its card defines only the raw config.

Note the upload of the full processed data repacks the whole corpus
(~11 GB → ~2 GB gzipped, streamed with constant RAM); `dataset.update` skips
the upload step entirely when the crawl found no new bugs.

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

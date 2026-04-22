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

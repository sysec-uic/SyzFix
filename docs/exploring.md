# Exploring the Dataset

All commands are run from the **project root** (`syzfix/`).
Requires Option B from [reproducing.md](reproducing.md) — the processed data must be present locally in `syzbot-dataset/data/processed/`.

## Browse the bug list

```bash
# List all bugs — V=patch versions, P=has patch, D=has discussion
python -m syzbot-dataset.view list

# Only bugs with patch iteration history (v1 → v2+)
python -m syzbot-dataset.view list --has-evolution

# Filter by keyword (title / subsystem)
python -m syzbot-dataset.view list --subsystem net -n 20
python -m syzbot-dataset.view list --subsystem fs
```

## Inspect a single bug

```bash
# Full lifecycle: crash → patches → discussion → fix commit
python -m syzbot-dataset.view show <bug_id>

# Individual sections
python -m syzbot-dataset.view crash   <bug_id>              # kernel crash report + C reproducer
python -m syzbot-dataset.view patch   <bug_id>              # final merged patch
python -m syzbot-dataset.view patch   <bug_id> --version 1  # specific patch version
python -m syzbot-dataset.view discuss <bug_id>              # full email review thread
python -m syzbot-dataset.view discuss <bug_id> -v 2         # only v2 discussion
python -m syzbot-dataset.view diff    <bug_id>              # v1 → v2 → … side-by-side diff
```

## Search and discover

```bash
# Full-text search across titles and crash reports
python -m syzbot-dataset.view search "use-after-free"
python -m syzbot-dataset.view search "null pointer"

# Jump to a random bug
python -m syzbot-dataset.view random
```

## Example session

```bash
# Find a UAF bug with patch evolution
python -m syzbot-dataset.view list --has-evolution --subsystem net -n 5

# Pick a bug_id from the output, e.g. ea1cd4aa4d1e98458a55
python -m syzbot-dataset.view show ea1cd4aa4d1e98458a55

# Inspect the v1 → v2 diff to see what reviewers changed
python -m syzbot-dataset.view diff ea1cd4aa4d1e98458a55

# Read the review thread that drove the revision
python -m syzbot-dataset.view discuss ea1cd4aa4d1e98458a55 -v 1
```

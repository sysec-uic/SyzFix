# Exploring the Dataset

All commands are run from the **project root** (`syzfix/`).
Requires Option B from [reproducing.md](reproducing.md) — the processed data must be present locally in `syzbot-dataset/data/processed/`.

## First-time setup: build the index

`list` and `search` use a lightweight index (~4 MB) instead of loading all 11 GB
of processed data on every call.  Build it once after restoring or collecting data:

```bash
python syzbot-dataset/view.py build-index
# → Building index from 6947 bug files...
# → Index written to syzbot-dataset/data/index.jsonl (6947 bugs)
# → Done: 6947 bugs indexed in 48s (4071KB)
```

Re-run this command whenever new bugs are collected to keep the index current.

---

## Browse the bug list

```bash
# List all bugs — V=patch versions, P=has patch, R=has C reproducer, D=has discussion
python syzbot-dataset/view.py list

# Only bugs with a C reproducer (needed for crash reproduction)
python syzbot-dataset/view.py list --has-reproducer
python syzbot-dataset/view.py list -r                     # short flag

# Only bugs with patch iteration history (v1 → v2+)
python syzbot-dataset/view.py list --has-evolution

# Filter by keyword in title
python syzbot-dataset/view.py list --subsystem net -n 20
python syzbot-dataset/view.py list --subsystem fs

# Combine filters
python syzbot-dataset/view.py list --has-reproducer --has-evolution --subsystem net

# Force a rebuild of the index before listing
python syzbot-dataset/view.py list --rebuild-index
```

---

## Inspect a single bug

```bash
# Full lifecycle: crash → patches → discussion → fix commit
python syzbot-dataset/view.py show <bug_id>

# Individual sections
python syzbot-dataset/view.py crash   <bug_id>              # kernel crash report + C reproducer
python syzbot-dataset/view.py patch   <bug_id>              # final merged patch
python syzbot-dataset/view.py patch   <bug_id> --version 1  # specific patch version
python syzbot-dataset/view.py discuss <bug_id>              # full email review thread
python syzbot-dataset/view.py discuss <bug_id> -v 2         # only v2 discussion
python syzbot-dataset/view.py diff    <bug_id>              # v1 → v2 → … side-by-side diff
```

---

## Search and discover

```bash
# Fast search across titles and crash report summaries (uses index, <0.1s)
python syzbot-dataset/view.py search "use-after-free"
python syzbot-dataset/view.py search "null pointer"

# Restrict to bugs with a C reproducer
python syzbot-dataset/view.py search "use-after-free" --has-reproducer
python syzbot-dataset/view.py search "use-after-free" -r

# Deep search: scan full crash reports (slow — loads all files)
python syzbot-dataset/view.py search "filemap_read_folio" --deep

# Jump to a random bug
python syzbot-dataset/view.py random
```

> **Fast vs deep search:** the default fast search matches against the bug title
> and the first 300 characters of the crash report.  Use `--deep` if you need to
> search the full crash report text.

---

## Example session

```bash
# Find reproducible UAF bugs with patch evolution in networking
python syzbot-dataset/view.py list --has-reproducer --has-evolution --subsystem net -n 10

# Pick a bug_id from the output, e.g. ea1cd4aa4d1e98458a55
python syzbot-dataset/view.py show ea1cd4aa4d1e98458a55

# Inspect the v1 → v2 diff to see what reviewers changed
python syzbot-dataset/view.py diff ea1cd4aa4d1e98458a55

# Read the review thread that drove the revision
python syzbot-dataset/view.py discuss ea1cd4aa4d1e98458a55 -v 1

# Reproduce the crash (see evaluation.md)
python evaluation/reproduce_crash.py ea1cd4aa4d1e98458a55
```

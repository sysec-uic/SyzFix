#!/usr/bin/env bash
# Cron wrapper for the incremental dataset update (syzbot → local → HF).
#
# Install (weekly, Monday 06:00):
#   crontab -e
#   0 6 * * 1 SYZFIX_PYTHON=/path/to/venv/bin/python /path/to/syzfix/scripts/cron_update.sh
#
# When no new bugs landed on syzbot, the run costs one API request and exits.
# Logs append to dataset/data/cron_update.log (gitignored).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${SYZFIX_PYTHON:-$REPO_DIR/venv/bin/python}"
HF_REPO="${SYZFIX_HF_REPO:-xiaoguangwang/syzfix-dataset}"
LOG="$REPO_DIR/dataset/data/cron_update.log"

cd "$REPO_DIR"
{
  echo "── $(date -Is) update start"
  "$PYTHON" -m dataset.update --repo "$HF_REPO"
  echo "── $(date -Is) update done"
} >>"$LOG" 2>&1

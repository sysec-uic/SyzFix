"""Refresh the landing-page stats (docs/index.md) from the local viewer index.

The numbers between the `<!-- stats:start -->` / `<!-- stats:end -->` markers
and the "N+ fixed Linux kernel bugs" headline are generated; edit this file,
not the Markdown, to change them.

Usage:
    python -m dataset.view build-index   # if the index is stale
    python -m dataset.site_stats         # rewrite docs/index.md in place
    python -m dataset.site_stats --check # exit 1 if docs/index.md is stale
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

DOCS_INDEX = Path(__file__).resolve().parent.parent / "docs" / "index.md"
INDEX_FILE = Path(__file__).parent / "data" / "index.jsonl"

_BLOCK_RE = re.compile(r"<!-- stats:start -->.*?<!-- stats:end -->", re.S)
_HEADLINE_RE = re.compile(r"(\*\*The full lifecycle of )[\d,]+\+( fixed Linux kernel bugs\*\*)")


def compute_stats(index_file: Path = INDEX_FILE) -> dict:
    records = [json.loads(line) for line in index_file.open()]
    return {
        "bugs": len(records),
        "diffs": sum(r["has_patch"] for r in records),
        "discussions": sum(r["has_discussion"] for r in records),
        "reproducers": sum(r["has_c_reproducer"] for r in records),
        "multi_version": sum(r["n_patch_versions"] > 1 for r in records),
        "max_versions": max(r["n_patch_versions"] for r in records),
    }


def render_block(s: dict, as_of: date) -> str:
    tiles = [
        (f"{s['bugs']:,}", "fixed kernel bugs"),
        (f"{s['diffs']:,}", "merged patch diffs"),
        (f"{s['discussions']:,}", "review discussions"),
        (f"{s['reproducers']:,}", "C reproducers"),
        (f"{s['multi_version']:,}", "multi-version patch histories"),
        (f"≤{s['max_versions']}", "patch versions per bug"),
    ]
    rows = "\n".join(
        f'  <div class="sf-stat"><span class="num">{num}</span>'
        f'<span class="label">{label}</span></div>' for num, label in tiles)
    return (
        "<!-- stats:start -->\n"
        '<div class="sf-stats" markdown>\n'
        f"{rows}\n"
        "</div>\n\n"
        f"*Counts as of {as_of:%B %Y} — the dataset tracks syzbot continuously and grows\n"
        "with each incremental update.*\n"
        "<!-- stats:end -->"
    )


def refresh(text: str, s: dict, as_of: date) -> str:
    if not _BLOCK_RE.search(text):
        raise SystemExit(f"stats markers not found in {DOCS_INDEX}")
    text = _BLOCK_RE.sub(lambda _: render_block(s, as_of), text)
    floor = s["bugs"] // 100 * 100
    return _HEADLINE_RE.sub(lambda m: f"{m[1]}{floor:,}+{m[2]}", text)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="Only report whether docs/index.md is up to date")
    args = parser.parse_args()

    s = compute_stats()
    old = DOCS_INDEX.read_text()
    # Keep the "as of" month when the numbers are unchanged, so --check and
    # re-runs don't churn the file just because the calendar moved.
    new = refresh(old, s, date.today())
    if refresh(old, s, _as_of(old)) == old:
        new = old

    if args.check:
        print("docs/index.md is up to date" if new == old
              else "docs/index.md stats are stale — run: python -m dataset.site_stats")
        sys.exit(0 if new == old else 1)
    if new == old:
        print(f"docs/index.md already current ({s['bugs']:,} bugs)")
        return
    DOCS_INDEX.write_text(new)
    print(f"docs/index.md updated: {s['bugs']:,} bugs, {s['diffs']:,} diffs, "
          f"{s['discussions']:,} discussions, {s['reproducers']:,} reproducers, "
          f"{s['multi_version']:,} multi-version")


def _as_of(text: str) -> date:
    from datetime import datetime
    m = re.search(r"\*Counts as of (\w+ \d{4})", text)
    return datetime.strptime(m[1], "%B %Y").date() if m else date.today()


if __name__ == "__main__":
    main()

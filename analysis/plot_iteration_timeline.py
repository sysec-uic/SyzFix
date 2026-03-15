#!/usr/bin/env python3
"""
Plot the Evolutionary Timeline of Kernel Repairs.

Produces a stacked area chart (average days per iteration gap, by year)
overlaid with a bug-count line — matching the style of Figure 1 in the paper.

Usage:
    cd /path/to/SyzFix
    python -m analysis.plot_iteration_timeline
    python -m analysis.plot_iteration_timeline --out figure1.png
    python -m analysis.plot_iteration_timeline --min-year 2020
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# ── make sure the syzbot-dataset package is importable ────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "syzbot-dataset"))

from storage import DataStore, ProgressDB  # noqa: E402


# ── timestamp helpers ──────────────────────────────────────────────────────────

def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_email_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except Exception:
        return None


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── per-bug extraction ─────────────────────────────────────────────────────────

MAX_TRACKED_ITER = 5   # Iter5+ lumped together


def extract_gaps(bug: dict) -> dict | None:
    """Return a dict with year and per-gap durations (days), or None if unusable."""

    crash_dt = _parse_iso(bug.get("first_crash"))
    if crash_dt is None:
        return None
    crash_dt = _to_utc(crash_dt)
    year = crash_dt.year

    # Build version → earliest submission datetime from discussion threads
    version_dates: dict[int, datetime] = {}
    for disc in bug.get("discussions", []):
        pv = disc.get("patch_version")
        if pv is None:
            continue
        for msg in disc.get("messages", []):
            dt = _parse_email_date(msg.get("date"))
            if dt is None:
                continue
            dt = _to_utc(dt)
            if pv not in version_dates or dt < version_dates[pv]:
                version_dates[pv] = dt
            break  # first message in thread is the submission

    if not version_dates:
        return None

    versions = sorted(version_dates)
    v1_dt = version_dates[versions[0]]

    gaps: dict[str, float] = {}

    # Report → Iter1
    gap = (v1_dt - crash_dt).total_seconds() / 86400
    if gap < 0 or gap > 3650:   # sanity: discard implausible values
        return None
    gaps["Report->Iter1"] = gap

    # Iter_n → Iter_{n+1}
    for i in range(len(versions) - 1):
        va, vb = versions[i], versions[i + 1]
        label = f"Iter{min(va, MAX_TRACKED_ITER)}->Iter{min(vb, MAX_TRACKED_ITER + 1)}"
        if va >= MAX_TRACKED_ITER:
            label = f"Iter{MAX_TRACKED_ITER}+"
        g = (version_dates[vb] - version_dates[va]).total_seconds() / 86400
        if 0 <= g <= 3650:
            gaps[label] = gaps.get(label, 0) + g

    # Last iter → merge (using fix_commit date, fallback to fix_time)
    merge_dt = None
    for fc in bug.get("fix_commits", []):
        merge_dt = _parse_iso(fc.get("date"))
        if merge_dt:
            break
    if merge_dt is None:
        merge_dt = _parse_iso(bug.get("fix_time"))
    if merge_dt:
        merge_dt = _to_utc(merge_dt)
        last_v = version_dates[versions[-1]]
        g = (merge_dt - last_v).total_seconds() / 86400
        if 0 <= g <= 3650:
            gaps[f"Iter{min(versions[-1], MAX_TRACKED_ITER)}->Merge"] = g

    return {"year": year, "gaps": gaps}


# ── aggregation ────────────────────────────────────────────────────────────────

# Ordered layer names (bottom → top of stack)
LAYERS = [
    "Report->Iter1",
    "Iter1->Iter2",
    "Iter2->Iter3",
    "Iter3->Iter4",
    "Iter4->Iter5",
    f"Iter{MAX_TRACKED_ITER}+",
]
LAYER_COLORS = [
    "#76c4ae",   # teal      Report→Iter1
    "#f4a87d",   # salmon    Iter1→Iter2
    "#9db8d2",   # blue-grey Iter2→Iter3
    "#e89ec0",   # pink      Iter3→Iter4
    "#b5d98a",   # green     Iter4→Iter5
    "#f5d060",   # yellow    Iter5+
]


def aggregate(records: list[dict], min_year: int, max_year: int) -> dict:
    """
    Returns:
        years: list[int]
        layer_means: dict[label -> list[float]]  (avg days, one per year)
        counts: list[int]  (bugs per year)
    """
    year_gaps: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    year_counts: dict[int, int] = defaultdict(int)

    for rec in records:
        y = rec["year"]
        if y < min_year or y > max_year:
            continue
        year_counts[y] += 1
        for label, days in rec["gaps"].items():
            year_gaps[y][label].append(days)

    years = list(range(min_year, max_year + 1))
    layer_means: dict[str, list[float]] = {lbl: [] for lbl in LAYERS}
    counts: list[int] = []

    for y in years:
        counts.append(year_counts.get(y, 0))
        for lbl in LAYERS:
            vals = year_gaps[y].get(lbl, [])
            layer_means[lbl].append(sum(vals) / len(vals) if vals else 0.0)

    return {"years": years, "layer_means": layer_means, "counts": counts}


# ── plotting ───────────────────────────────────────────────────────────────────

def plot(agg: dict, out_path: Path | None) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg" if out_path else "TkAgg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        sys.exit("pip install matplotlib numpy")

    years = agg["years"]
    layer_means = agg["layer_means"]
    counts = agg["counts"]

    x = np.array(years)
    bottoms = np.zeros(len(years))

    fig, ax1 = plt.subplots(figsize=(12, 6))

    for lbl, color in zip(LAYERS, LAYER_COLORS):
        vals = np.array(layer_means[lbl])
        ax1.fill_between(x, bottoms, bottoms + vals,
                         label=lbl, color=color, alpha=0.85, linewidth=0)
        bottoms = bottoms + vals

    ax1.set_xlabel("Year", fontsize=13)
    ax1.set_ylabel("Average Iteration Duration (Days)", fontsize=12)
    ax1.set_xlim(x[0] - 0.3, x[-1] + 0.3)
    ax1.set_xticks(x)
    ax1.set_xticklabels([f"'{str(y)[2:]}" for y in years])
    ax1.set_ylim(0)
    ax1.grid(axis="y", linestyle="--", linewidth=0.5, alpha=0.5)
    ax1.spines["top"].set_visible(False)

    # Bug count overlay
    ax2 = ax1.twinx()
    ax2.plot(x, counts, "k--o", linewidth=1.5, markersize=4,
             label="Bug Count (Volume)", zorder=5)
    ax2.set_ylabel("Number of Analyzed Bugs", fontsize=12)
    ax2.set_ylim(0)
    ax2.spines["top"].set_visible(False)

    # Combined legend at top
    handles1, labels1 = ax1.get_legend_handles_labels()
    handles2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(handles1 + handles2, labels1 + labels2,
               loc="upper center", bbox_to_anchor=(0.5, 1.13),
               ncol=4, fontsize=9, frameon=False)

    plt.title("")
    plt.tight_layout()

    if out_path:
        plt.savefig(out_path, dpi=180, bbox_inches="tight")
        print(f"Saved to {out_path}")
    else:
        plt.show()


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Plot kernel repair iteration timeline")
    parser.add_argument("--out", default="analysis/results/iteration_timeline.png",
                        help="Output PNG path (default: analysis/results/iteration_timeline.png)")
    parser.add_argument("--min-year", type=int, default=2016)
    parser.add_argument("--max-year", type=int, default=datetime.now().year)
    parser.add_argument("--no-save", action="store_true",
                        help="Show interactive plot instead of saving")
    args = parser.parse_args()

    print("Loading processed bugs...")
    store = DataStore()
    db = ProgressDB()
    ids = db.get_bugs_at_step("processed")
    db.close()

    records = []
    skipped = 0
    for bug_id in ids:
        bug = store.load_processed(bug_id)
        if not bug:
            continue
        rec = extract_gaps(bug)
        if rec:
            records.append(rec)
        else:
            skipped += 1

    print(f"  {len(records)} bugs with usable timestamps ({skipped} skipped)")

    agg = aggregate(records, args.min_year, args.max_year)

    # Print summary table
    print(f"\n{'Year':>6}  {'Bugs':>5}  " +
          "  ".join(f"{lbl[:12]:>12}" for lbl in LAYERS))
    for i, y in enumerate(agg["years"]):
        if agg["counts"][i] == 0:
            continue
        row = f"{y:>6}  {agg['counts'][i]:>5}  "
        row += "  ".join(f"{agg['layer_means'][lbl][i]:>12.1f}" for lbl in LAYERS)
        print(row)

    out_path = None if args.no_save else Path(args.out)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    plot(agg, out_path)


if __name__ == "__main__":
    main()

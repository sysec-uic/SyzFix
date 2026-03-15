"""
Analyzer: Difficulty stratification.

Computes a composite difficulty score per bug and stratifies into
easy / medium / hard tiers for use in model evaluation.
"""

import re
from collections import Counter, defaultdict
from typing import Optional

from ..loader import BugEntry
from ..filters import parse_stack_trace
from .base import BaseAnalyzer, AnalysisResult


def _patch_size(bug: BugEntry) -> Optional[int]:
    """Total lines changed in fix patch."""
    for fc in bug.fix_commits:
        if fc.patch_diff:
            added = len(re.findall(r'^\+[^+]', fc.patch_diff, re.MULTILINE))
            removed = len(re.findall(r'^-[^-]', fc.patch_diff, re.MULTILINE))
            return added + removed
    return None


def _num_files(bug: BugEntry) -> Optional[int]:
    """Number of files modified in fix patch."""
    for fc in bug.fix_commits:
        if fc.patch_diff:
            return len(re.findall(r'diff --git a/(\S+)', fc.patch_diff))
    return None


def _fix_locality_score(bug: BugEntry) -> int:
    """Simplified locality score: 0=same-function, 1=same-file, 2=same-dir, 3=different.

    Returns 3 (unknown) if we can't compute locality.
    """
    crash = bug.crash_report
    patch_diff = ""
    for fc in bug.fix_commits:
        if fc.patch_diff:
            patch_diff = fc.patch_diff
            break

    if not crash or not patch_diff:
        return 3

    frames = parse_stack_trace(crash)
    if not frames:
        return 3

    fix_files = set(re.findall(r'diff --git a/(\S+)', patch_diff))
    fix_functions = set(re.findall(r'^@@.*@@\s+(\w+)', patch_diff, re.MULTILINE))

    crash_functions = {f.function for f in frames}
    crash_files = {f.file for f in frames if f.file}

    import os
    crash_basenames = {os.path.basename(f) for f in crash_files}
    fix_basenames = {os.path.basename(f) for f in fix_files}

    # Same function
    if crash_functions & fix_functions:
        return 0
    # Same file
    if crash_files & fix_files or crash_basenames & fix_basenames:
        return 1
    # Same directory
    crash_dirs = {os.path.dirname(f) for f in crash_files}
    fix_dirs = {os.path.dirname(f) for f in fix_files}
    if crash_dirs & fix_dirs:
        return 2
    return 3


def _time_to_fix_days(bug: BugEntry) -> Optional[float]:
    """Time from first crash to fix in days."""
    from datetime import datetime
    first = bug.first_crash
    fix = bug.fix_time
    if not first or not fix:
        return None
    try:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
            try:
                t1 = datetime.strptime(first, fmt)
                break
            except ValueError:
                continue
        else:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
            try:
                t2 = datetime.strptime(fix, fmt)
                break
            except ValueError:
                continue
        else:
            return None
        delta = (t2 - t1).total_seconds() / 86400
        return delta if delta >= 0 else None
    except Exception:
        return None


def compute_difficulty(bug: BugEntry) -> Optional[dict]:
    """Compute difficulty features and tier for a bug.

    Returns dict with individual features and composite tier, or None
    if the bug doesn't have enough data.
    """
    size = _patch_size(bug)
    if size is None:
        return None

    nfiles = _num_files(bug) or 1
    iterations = bug.num_patch_versions
    locality = _fix_locality_score(bug)
    fix_days = _time_to_fix_days(bug)
    has_c_repro = bool(bug.c_reproducer and len(bug.c_reproducer) > 20)

    # ── Scoring (each feature → 0-2 points, higher = harder) ────────

    # Patch size
    if size <= 10:
        size_score = 0
    elif size <= 50:
        size_score = 1
    else:
        size_score = 2

    # Files modified
    if nfiles == 1:
        files_score = 0
    elif nfiles <= 3:
        files_score = 1
    else:
        files_score = 2

    # Iterations
    if iterations <= 1:
        iter_score = 0
    elif iterations <= 3:
        iter_score = 1
    else:
        iter_score = 2

    # Locality (0=same-func, 1=same-file, 2=same-dir, 3=different)
    if locality <= 1:
        loc_score = 0
    elif locality == 2:
        loc_score = 1
    else:
        loc_score = 2

    # Time to fix
    if fix_days is not None:
        if fix_days <= 7:
            time_score = 0
        elif fix_days <= 30:
            time_score = 1
        else:
            time_score = 2
    else:
        time_score = 1  # assume medium if unknown

    # Reproducer (having one makes it easier)
    repro_score = 0 if has_c_repro else 1

    # Composite score (max 12)
    total_score = size_score + files_score + iter_score + loc_score + time_score + repro_score

    # Tier assignment
    if total_score <= 3:
        tier = "easy"
    elif total_score <= 7:
        tier = "medium"
    else:
        tier = "hard"

    return {
        "patch_size": size,
        "num_files": nfiles,
        "iterations": iterations,
        "locality_score": locality,
        "fix_days": round(fix_days, 1) if fix_days is not None else None,
        "has_c_repro": has_c_repro,
        "size_score": size_score,
        "files_score": files_score,
        "iter_score": iter_score,
        "loc_score": loc_score,
        "time_score": time_score,
        "repro_score": repro_score,
        "total_score": total_score,
        "tier": tier,
    }


class DifficultyStratificationAnalyzer(BaseAnalyzer):
    """Stratify bugs into difficulty tiers."""

    @property
    def name(self) -> str:
        return "Difficulty Stratification"

    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        tier_counter = Counter()
        tier_data: dict[str, list[dict]] = defaultdict(list)
        details = []
        analyzed = 0

        # Score distribution
        score_counter = Counter()

        for bug in bugs:
            result = compute_difficulty(bug)
            if result is None:
                continue

            analyzed += 1
            tier = result["tier"]
            tier_counter[tier] += 1
            score_counter[result["total_score"]] += 1

            entry = {
                "bug_id": bug.bug_id,
                "title": bug.title,
                **result,
            }
            details.append(entry)
            tier_data[tier].append(entry)

        # Summary
        summary = {
            "Bugs analyzed": analyzed,
            "Bugs skipped (no patch)": len(bugs) - analyzed,
        }
        for tier in ["easy", "medium", "hard"]:
            count = tier_counter.get(tier, 0)
            summary[f"  {tier}"] = f"{count} ({count / max(analyzed, 1) * 100:.1f}%)"

        # Per-tier statistics
        tier_table = []
        for tier in ["easy", "medium", "hard"]:
            entries = tier_data.get(tier, [])
            if not entries:
                continue
            sizes = [e["patch_size"] for e in entries]
            iters = [e["iterations"] for e in entries if e["iterations"] > 0]
            nfiles = [e["num_files"] for e in entries]
            days = [e["fix_days"] for e in entries if e["fix_days"] is not None]
            repro_pct = sum(1 for e in entries if e["has_c_repro"]) / len(entries) * 100

            def _med(lst):
                return sorted(lst)[len(lst) // 2] if lst else None

            tier_table.append({
                "tier": tier,
                "count": len(entries),
                "median_patch_lines": _med(sizes),
                "median_files": _med(nfiles),
                "median_iterations": _med(iters),
                "median_fix_days": round(_med(days), 1) if _med(days) is not None else None,
                "pct_with_c_repro": f"{repro_pct:.1f}%",
            })

        # Score distribution table
        score_table = [
            {"score": s, "count": c}
            for s, c in sorted(score_counter.items())
        ]

        # Feature correlation: which features contribute most to difficulty?
        feature_means: dict[str, dict[str, float]] = {}
        for tier in ["easy", "medium", "hard"]:
            entries = tier_data.get(tier, [])
            if not entries:
                continue
            feature_means[tier] = {
                "avg_size_score": sum(e["size_score"] for e in entries) / len(entries),
                "avg_files_score": sum(e["files_score"] for e in entries) / len(entries),
                "avg_iter_score": sum(e["iter_score"] for e in entries) / len(entries),
                "avg_loc_score": sum(e["loc_score"] for e in entries) / len(entries),
                "avg_time_score": sum(e["time_score"] for e in entries) / len(entries),
                "avg_repro_score": sum(e["repro_score"] for e in entries) / len(entries),
            }
        feature_table = []
        for tier in ["easy", "medium", "hard"]:
            if tier in feature_means:
                feature_table.append({
                    "tier": tier,
                    **{k: round(v, 2) for k, v in feature_means[tier].items()},
                })

        return AnalysisResult(
            name=self.name,
            summary=summary,
            details=details,
            tables={
                "tier_statistics": tier_table,
                "score_distribution": score_table,
                "feature_contributions": feature_table,
            },
        )

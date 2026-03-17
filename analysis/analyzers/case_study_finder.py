"""
Analyzer: Case Study Finder.

Ranks bugs by a composite "interestingness" score across multiple dimensions
to identify compelling case studies for a research paper. Includes patch
complexity metrics and a paper-friendly filter.
"""

import re
from collections import Counter

from ..loader import BugEntry
from ..filters import get_human_reviews, is_stable_backport_thread, parse_stack_trace
from .base import BaseAnalyzer, AnalysisResult
from .difficulty_stratification import (
    _patch_size, _num_files, _fix_locality_score, _time_to_fix_days,
)
from .patch_diff_analysis import parse_diff_stats, extract_diff_from_patch_message


# ─── Dimension scoring helpers ─────────────────────────────────────────────

def _score_iterations(bug: BugEntry) -> int:
    """0-3 score based on number of patch versions."""
    n = bug.num_patch_versions
    if n <= 1:
        return 0
    if n == 2:
        return 1
    if n == 3:
        return 2
    return 3  # 4+


def _count_human_reviews(bug: BugEntry) -> int:
    """Count substantive human review messages across non-backport threads."""
    count = 0
    for disc in bug.discussions:
        if is_stable_backport_thread(disc):
            continue
        count += len(get_human_reviews(disc.messages))
    return count


def _score_discussion(review_count: int) -> int:
    """0-3 score based on human review count."""
    if review_count <= 1:
        return 0
    if review_count <= 5:
        return 1
    if review_count <= 15:
        return 2
    return 3  # 16+


def _get_version_stats(bug: BugEntry) -> list[dict]:
    """Get patch stats per version from discussion threads."""
    pvs = bug.patch_versions
    stats = []
    for pv in pvs:
        diff = extract_diff_from_patch_message(pv.messages)
        if diff:
            s = parse_diff_stats(diff)
            stats.append({
                "version": pv.patch_version,
                "lines": s["total_lines"],
                "files": len(s["files"]),
            })
        else:
            stats.append({
                "version": pv.patch_version,
                "lines": None,
                "files": None,
            })
    return stats


def _score_structural_change(v_stats: list[dict]) -> tuple[int, int | None, int | None]:
    """0-3 score based on abs line delta between v1 and v2.

    Returns (score, v1_lines, v2_lines).
    """
    if len(v_stats) < 2:
        return 0, None, None

    v1 = v_stats[0]
    v2 = v_stats[1]
    v1_lines = v1["lines"]
    v2_lines = v2["lines"]

    if v1_lines is None or v2_lines is None:
        return 0, v1_lines, v2_lines

    delta = abs(v2_lines - v1_lines)
    if delta < 10:
        score = 0
    elif delta < 100:
        score = 1
    elif delta < 500:
        score = 2
    else:
        score = 3
    return score, v1_lines, v2_lines


def _score_fix_time(bug: BugEntry) -> tuple[int, float | None]:
    """0-3 score based on time to fix. Returns (score, days)."""
    days = _time_to_fix_days(bug)
    if days is None:
        return 1, None  # assume medium if unknown
    if days < 7:
        return 0, days
    if days < 30:
        return 1, days
    if days < 180:
        return 2, days
    return 3, days


def _score_locality(bug: BugEntry) -> int:
    """0-3 score based on fix locality (same-func=0, diff-subsys=3)."""
    return _fix_locality_score(bug)


def _score_scope_change(v_stats: list[dict]) -> int:
    """0-3 score based on file count delta between v1 and v2."""
    if len(v_stats) < 2:
        return 0
    v1_files = v_stats[0]["files"]
    v2_files = v_stats[1]["files"]
    if v1_files is None or v2_files is None:
        return 0
    delta = abs(v2_files - v1_files)
    if delta == 0:
        return 0
    if delta == 1:
        return 1
    if delta <= 5:
        return 2
    return 3  # >5


def _score_info_scarcity(bug: BugEntry) -> int:
    """0-3 score based on missing information."""
    score = 0
    has_c_repro = bool(bug.c_reproducer and len(bug.c_reproducer) > 20)
    # Check for syz reproducer
    has_syz = False
    for c in bug.raw.get("crashes", []):
        if c.get("syz_reproducer") and len(c.get("syz_reproducer", "")) > 20:
            has_syz = True
            break
    has_stack = bool(parse_stack_trace(bug.crash_report))

    if not has_c_repro:
        score += 1
    if not has_syz:
        score += 1
    if not has_stack:
        score += 1
    return score


# ─── Narrative hook generation ─────────────────────────────────────────────

def _generate_hooks(entry: dict) -> list[str]:
    """Generate human-readable narrative hooks from scored entry."""
    hooks = []
    v1 = entry.get("v1_lines")
    v2 = entry.get("v2_lines")
    if v1 is not None and v2 is not None and v1 != v2:
        v1f = entry.get("v1_files")
        v2f = entry.get("v2_files")
        hooks.append(f"v1: {v1} lines/{v1f or '?'} files -> v2: {v2} lines/{v2f or '?'} files")

    iters = entry.get("iterations")
    if iters and iters >= 3:
        hooks.append(f"{iters} patch iterations")

    reviews = entry.get("num_human_reviews")
    if reviews and reviews >= 10:
        hooks.append(f"{reviews} human review messages")

    days = entry.get("fix_days")
    if days is not None and days > 180:
        hooks.append(f"{days:.0f} days to fix")

    loc = entry.get("locality_score")
    if loc == 3:
        hooks.append("fix in different subsystem from crash")
    elif loc == 2:
        hooks.append("fix in different directory from crash")

    info = entry.get("info_scarcity_score")
    if info and info >= 2:
        hooks.append("limited debugging information available")

    final = entry.get("final_patch_lines")
    if final is not None and final <= 10:
        hooks.append(f"final fix is only {final} lines")

    return hooks


# ─── Main analyzer ─────────────────────────────────────────────────────────

class CaseStudyFinder(BaseAnalyzer):
    """Find compelling case studies by ranking bugs on multi-dimensional interestingness."""

    @property
    def name(self) -> str:
        return "Case Study Finder"

    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        scored = []

        for bug in bugs:
            # Must have at least a fix commit with a diff
            final_lines = _patch_size(bug)
            if final_lines is None:
                continue

            final_files = _num_files(bug) or 1
            iterations = bug.num_patch_versions
            review_count = _count_human_reviews(bug)
            v_stats = _get_version_stats(bug)

            # Compute dimension scores
            iter_score = _score_iterations(bug)
            disc_score = _score_discussion(review_count)
            struct_score, v1_lines, v2_lines = _score_structural_change(v_stats)
            time_score, fix_days = _score_fix_time(bug)
            loc_score = _score_locality(bug)
            scope_score = _score_scope_change(v_stats)
            info_score = _score_info_scarcity(bug)

            composite = (iter_score + disc_score + struct_score +
                         time_score + loc_score + scope_score + info_score)

            # Version file counts
            v1_files = v_stats[0]["files"] if v_stats else None
            v2_files = v_stats[1]["files"] if len(v_stats) >= 2 else None

            entry = {
                "bug_id": bug.bug_id,
                "title": bug.title,
                "composite_score": composite,
                "iterations": iterations,
                "num_human_reviews": review_count,
                "fix_days": round(fix_days, 1) if fix_days is not None else None,
                "final_patch_lines": final_lines,
                "final_patch_files": final_files,
                "v1_lines": v1_lines,
                "v2_lines": v2_lines,
                "v1_files": v1_files,
                "v2_files": v2_files,
                "paper_friendly": final_lines <= 50,
                # Individual dimension scores
                "iter_score": iter_score,
                "disc_score": disc_score,
                "struct_score": struct_score,
                "time_score": time_score,
                "locality_score": loc_score,
                "scope_score": scope_score,
                "info_scarcity_score": info_score,
            }
            entry["narrative_hooks"] = _generate_hooks(entry)
            scored.append(entry)

        # Sort by composite score descending, then by iterations as tiebreaker
        scored.sort(key=lambda x: (-x["composite_score"], -x["iterations"]))

        # Summary
        total = len(scored)
        score_dist = Counter(e["composite_score"] for e in scored)
        paper_friendly_count = sum(1 for e in scored if e["paper_friendly"])

        summary = {
            "Bugs scored": total,
            "Paper-friendly (final patch ≤50 lines)": paper_friendly_count,
            "Max composite score": scored[0]["composite_score"] if scored else 0,
            "Score distribution": {s: score_dist[s] for s in sorted(score_dist, reverse=True)},
        }

        # Tables
        ranked_candidates = []
        for e in scored[:50]:
            ranked_candidates.append({
                "bug_id": e["bug_id"],
                "title": e["title"][:60],
                "score": e["composite_score"],
                "iters": e["iterations"],
                "reviews": e["num_human_reviews"],
                "fix_days": e["fix_days"],
                "final_lines": e["final_patch_lines"],
                "final_files": e["final_patch_files"],
                "v1_lines": e["v1_lines"],
                "v2_lines": e["v2_lines"],
                "paper_ok": "Y" if e["paper_friendly"] else "",
                "hooks": "; ".join(e["narrative_hooks"]),
            })

        # Paper-friendly top picks
        paper_friendly_top = [
            {
                "bug_id": e["bug_id"],
                "title": e["title"][:60],
                "score": e["composite_score"],
                "iters": e["iterations"],
                "reviews": e["num_human_reviews"],
                "fix_days": e["fix_days"],
                "final_lines": e["final_patch_lines"],
                "v1_lines": e["v1_lines"],
                "v2_lines": e["v2_lines"],
                "hooks": "; ".join(e["narrative_hooks"]),
            }
            for e in scored if e["paper_friendly"]
        ][:20]

        # Top by each dimension
        dimensions = [
            ("iter_score", "iterations"),
            ("disc_score", "discussion depth"),
            ("struct_score", "structural change"),
            ("time_score", "fix time"),
            ("locality_score", "fix locality"),
            ("scope_score", "scope change"),
            ("info_scarcity_score", "information scarcity"),
        ]
        top_by_dim = []
        for dim_key, dim_name in dimensions:
            dim_sorted = sorted(scored, key=lambda x: -x[dim_key])
            for e in dim_sorted[:5]:
                top_by_dim.append({
                    "dimension": dim_name,
                    "bug_id": e["bug_id"],
                    "title": e["title"][:50],
                    "dim_score": e[dim_key],
                    "composite": e["composite_score"],
                    "final_lines": e["final_patch_lines"],
                    "paper_ok": "Y" if e["paper_friendly"] else "",
                })

        return AnalysisResult(
            name=self.name,
            summary=summary,
            details=scored[:50],
            tables={
                "ranked_candidates": ranked_candidates,
                "top_paper_friendly": paper_friendly_top,
                "top_by_dimension": top_by_dim,
            },
        )

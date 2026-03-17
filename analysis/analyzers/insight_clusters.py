"""
Analyzer: Insight Clusters.

Cross-references bug type, fix pattern, locality, difficulty, revision reasons,
and structural change to identify named categories of bugs that share interesting
characteristics. Each category is defined by a predicate over per-bug features,
and the analyzer computes statistics, representative examples, and overlap
analysis.

Designed for paper writing: each cluster becomes a paragraph in the findings
section describing a class of kernel bugs and why they are hard.
"""

import re
from collections import Counter, defaultdict
from typing import Any, Callable, Optional

from ..loader import BugEntry
from ..filters import get_human_reviews, is_stable_backport_thread
from .base import BaseAnalyzer, AnalysisResult

# Import classification functions from sibling analyzers
from .bug_type_classifier import classify_bug_type
from .fix_patterns import classify_fix_patterns
from .difficulty_stratification import (
    _patch_size, _num_files, _fix_locality_score, _time_to_fix_days,
)
from .patch_diff_analysis import parse_diff_stats, extract_diff_from_patch_message
from .revision_reasons import analyze_bug_revision


# ─── Per-bug feature computation ───────────────────────────────────────────

def _compute_bug_features(bug: BugEntry) -> Optional[dict[str, Any]]:
    """Compute all cross-dimensional features for a single bug.

    Returns None if the bug doesn't have enough data (no patch diff).
    """
    patch_lines = _patch_size(bug)
    if patch_lines is None:
        return None

    bug_type = classify_bug_type(bug.title, bug.crash_report)
    fix_patterns = classify_fix_patterns(bug)
    locality = _fix_locality_score(bug)
    fix_days = _time_to_fix_days(bug)
    num_files = _num_files(bug) or 1
    iterations = bug.num_patch_versions
    has_c_repro = bool(bug.c_reproducer and len(bug.c_reproducer) > 20)

    # Syz reproducer
    has_syz = False
    for c in bug.raw.get("crashes", []):
        if c.get("syz_reproducer") and len(c.get("syz_reproducer", "")) > 20:
            has_syz = True
            break

    # Revision reasons (only for multi-version bugs)
    revision_cats = []
    if bug.has_multiple_versions:
        rev_info = analyze_bug_revision(bug)
        if rev_info:
            revision_cats = rev_info.get("categories", [])

    # Discussion depth (filtered)
    review_count = 0
    for disc in bug.discussions:
        if not is_stable_backport_thread(disc):
            review_count += len(get_human_reviews(disc.messages))

    # Structural change between v1 and v2
    v_stats = []
    for pv in bug.patch_versions:
        diff = extract_diff_from_patch_message(pv.messages)
        if diff:
            s = parse_diff_stats(diff)
            v_stats.append({"lines": s["total_lines"], "files": len(s["files"])})
        else:
            v_stats.append({"lines": None, "files": None})

    v1_lines = v_stats[0]["lines"] if v_stats else None
    v2_lines = v_stats[1]["lines"] if len(v_stats) >= 2 else None

    return {
        "bug_id": bug.bug_id,
        "title": bug.title,
        "bug_type": bug_type,
        "fix_patterns": fix_patterns,
        "locality": locality,  # 0=same-func, 1=same-file, 2=same-dir, 3=diff-subsys
        "fix_days": fix_days,
        "patch_lines": patch_lines,
        "num_files": num_files,
        "iterations": iterations,
        "has_c_repro": has_c_repro,
        "has_syz_repro": has_syz,
        "revision_cats": revision_cats,
        "review_count": review_count,
        "v1_lines": v1_lines,
        "v2_lines": v2_lines,
    }


# ─── Cluster definitions ──────────────────────────────────────────────────

# Expected fix patterns per bug type.  If the actual fix uses a DIFFERENT
# pattern, this bug has "misleading symptoms".
EXPECTED_PATTERNS: dict[str, set[str]] = {
    "null-ptr-deref":      {"add-null-check", "add-return-check", "add-init"},
    "use-after-free":      {"add-missing-free", "fix-refcount", "add-lock"},
    "memory-leak":         {"add-missing-free"},
    "out-of-bounds-read":  {"add-bounds-check"},
    "out-of-bounds-write": {"add-bounds-check"},
    "out-of-bounds":       {"add-bounds-check"},
    "data-race":           {"add-lock"},
    "deadlock":            {"add-lock", "fix-order"},
    "info-leak":           {"add-init"},
    "double-free":         {"add-null-check", "fix-refcount", "remove-code"},
    "refcount-bug":        {"fix-refcount"},
}


def _is_misleading_symptoms(f: dict) -> bool:
    """Bug type suggests one pattern, but fix uses a completely different one."""
    bt = f["bug_type"]
    expected = EXPECTED_PATTERNS.get(bt)
    if not expected or not f["fix_patterns"]:
        return False
    # None of the actual fix patterns match the expected ones
    actual = set(f["fix_patterns"])
    return len(actual & expected) == 0


def _is_deceptively_simple(f: dict) -> bool:
    """Final patch ≤ 10 lines, but took > 180 days or ≥ 3 iterations."""
    if f["patch_lines"] > 10:
        return False
    if f["fix_days"] is not None and f["fix_days"] > 180:
        return True
    if f["iterations"] >= 3:
        return True
    return False


def _is_approach_revolution(f: dict) -> bool:
    """v1 and v2 differ dramatically in size or scope."""
    v1 = f["v1_lines"]
    v2 = f["v2_lines"]
    if v1 is None or v2 is None:
        return False
    big = max(v1, v2, 1)
    delta = abs(v1 - v2)
    # > 50% relative change AND absolute change > 20 lines
    if delta / big > 0.5 and delta > 20:
        return True
    return False


def _is_cross_subsystem(f: dict) -> bool:
    """Fix is in a different subsystem from the crash site."""
    return f["locality"] == 3


def _is_review_rescued(f: dict) -> bool:
    """Community review caught critical issues that v1 missed."""
    if f["iterations"] < 2:
        return False
    critical = {"correctness", "incomplete_fix", "memory_safety", "race_condition"}
    return bool(set(f["revision_cats"]) & critical)


def _is_long_lived(f: dict) -> bool:
    """Bug was open for more than 1 year."""
    return f["fix_days"] is not None and f["fix_days"] > 365


def _is_concurrency_labyrinth(f: dict) -> bool:
    """Bug involves concurrency: deadlock/data-race type, add-lock fix, or race revision."""
    if f["bug_type"] in ("deadlock", "data-race"):
        return True
    if "add-lock" in f["fix_patterns"]:
        return True
    if "race_condition" in f["revision_cats"]:
        return True
    return False


def _is_information_desert(f: dict) -> bool:
    """No C reproducer AND no syz reproducer — fixed blind."""
    return not f["has_c_repro"] and not f["has_syz_repro"]


# All insight clusters in order of paper importance
INSIGHT_CLUSTERS: list[dict[str, Any]] = [
    {
        "name": "Misleading Symptoms",
        "predicate": _is_misleading_symptoms,
        "paper_insight": (
            "The surface bug type (e.g., null-ptr-deref) suggests a certain fix pattern "
            "(e.g., add null check), but the actual fix addresses a completely different "
            "root cause (e.g., a race condition requiring a lock). An LLM trained only on "
            "bug-type-to-pattern mappings would propose the wrong fix."
        ),
    },
    {
        "name": "Deceptively Simple",
        "predicate": _is_deceptively_simple,
        "paper_insight": (
            "The final fix is tiny (≤10 lines), yet it took >6 months or 3+ patch "
            "iterations. The difficulty lies in diagnosis and understanding, not in "
            "writing code — a key challenge for automated repair."
        ),
    },
    {
        "name": "Approach Revolution",
        "predicate": _is_approach_revolution,
        "paper_insight": (
            "The developer completely changed their fixing approach between v1 and v2 "
            "(>50% structural change). Shows that even expert kernel developers often "
            "start with the wrong solution and need reviewer feedback to find the right one."
        ),
    },
    {
        "name": "Cross-Subsystem Root Cause",
        "predicate": _is_cross_subsystem,
        "paper_insight": (
            "The crash occurs in one subsystem, but the fix is in a different subsystem "
            "entirely. Requires deep architectural knowledge of kernel interactions — "
            "the fix location cannot be predicted from the crash stack trace alone."
        ),
    },
    {
        "name": "Review-Rescued",
        "predicate": _is_review_rescued,
        "paper_insight": (
            "The v1 patch had correctness issues, was incomplete, or missed a race "
            "condition that human reviewers caught. Without community review, these "
            "patches would have introduced new bugs or left the original bug unfixed."
        ),
    },
    {
        "name": "Long-Lived (>1 year)",
        "predicate": _is_long_lived,
        "paper_insight": (
            "These bugs remained open for over a year, often because they are "
            "hard to reproduce, require deep subsystem expertise, or are masked "
            "by related changes. The long tail of unfixed bugs is a dataset gap "
            "that SyzFix specifically addresses."
        ),
    },
    {
        "name": "Concurrency Labyrinth",
        "predicate": _is_concurrency_labyrinth,
        "paper_insight": (
            "Bugs involving concurrency — deadlocks, data races, lock-ordering issues, "
            "or patches that add locking. These require reasoning about multiple execution "
            "paths and shared state, a known weakness of current LLMs."
        ),
    },
    {
        "name": "Information Desert",
        "predicate": _is_information_desert,
        "paper_insight": (
            "Fixed without any C or syz reproducer — developers had to diagnose and fix "
            "the bug from the crash report alone. Evaluates the ability to generate "
            "patches with minimal input information."
        ),
    },
]


# ─── Statistics helpers ────────────────────────────────────────────────────

LOCALITY_NAMES = {0: "same-function", 1: "same-file", 2: "same-directory", 3: "different-subsystem"}


def _median(lst: list) -> Any:
    """Safe median that handles empty lists."""
    if not lst:
        return None
    s = sorted(lst)
    return s[len(s) // 2]


def _cluster_stats(members: list[dict]) -> dict[str, Any]:
    """Compute summary statistics for a cluster of bugs."""
    n = len(members)
    patch_lines = [m["patch_lines"] for m in members]
    fix_days = [m["fix_days"] for m in members if m["fix_days"] is not None]
    iters = [m["iterations"] for m in members]
    reviews = [m["review_count"] for m in members]

    # Top bug types
    bt_counter = Counter(m["bug_type"] for m in members)
    # Top fix patterns
    fp_counter = Counter()
    for m in members:
        for p in m["fix_patterns"]:
            fp_counter[p] += 1
    # Locality distribution
    loc_counter = Counter(LOCALITY_NAMES.get(m["locality"], "unknown") for m in members)

    return {
        "count": n,
        "median_patch_lines": _median(patch_lines),
        "median_fix_days": round(_median(fix_days), 1) if _median(fix_days) is not None else None,
        "median_iterations": _median(iters),
        "median_reviews": _median(reviews),
        "pct_with_c_repro": round(sum(1 for m in members if m["has_c_repro"]) / n * 100, 1),
        "top_bug_types": bt_counter.most_common(5),
        "top_fix_patterns": fp_counter.most_common(5),
        "locality_distribution": dict(loc_counter.most_common()),
    }


# ─── Main analyzer ─────────────────────────────────────────────────────────

class InsightClusterAnalyzer(BaseAnalyzer):
    """Cross-dimensional insight cluster analysis."""

    @property
    def name(self) -> str:
        return "Insight Clusters"

    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        # Phase 1: Compute features for all bugs
        all_features = []
        for bug in bugs:
            f = _compute_bug_features(bug)
            if f is not None:
                all_features.append(f)

        total = len(all_features)

        # Phase 2: Assign bugs to clusters
        cluster_members: dict[str, list[dict]] = {
            c["name"]: [] for c in INSIGHT_CLUSTERS
        }
        bug_cluster_count = Counter()  # how many clusters per bug

        for f in all_features:
            count = 0
            for cluster in INSIGHT_CLUSTERS:
                if cluster["predicate"](f):
                    cluster_members[cluster["name"]].append(f)
                    count += 1
            bug_cluster_count[count] += 1

        # Phase 3: Compute per-cluster statistics
        cluster_table = []
        cluster_details = []
        for cluster in INSIGHT_CLUSTERS:
            name = cluster["name"]
            members = cluster_members[name]
            if not members:
                continue

            stats = _cluster_stats(members)
            pct = stats["count"] / total * 100

            cluster_table.append({
                "cluster": name,
                "count": stats["count"],
                "pct": f"{pct:.1f}%",
                "median_patch_lines": stats["median_patch_lines"],
                "median_fix_days": stats["median_fix_days"],
                "median_iterations": stats["median_iterations"],
                "median_reviews": stats["median_reviews"],
                "pct_with_c_repro": f"{stats['pct_with_c_repro']:.0f}%",
                "top_bug_types": ", ".join(f"{t}({c})" for t, c in stats["top_bug_types"][:3]),
                "top_fix_patterns": ", ".join(f"{p}({c})" for p, c in stats["top_fix_patterns"][:3]),
            })

            # Detailed entry with examples and paper insight
            examples = sorted(members, key=lambda m: -(m["review_count"] + m["iterations"]))[:5]
            cluster_details.append({
                "cluster": name,
                "paper_insight": cluster["paper_insight"],
                "count": stats["count"],
                "pct": f"{pct:.1f}%",
                "stats": stats,
                "examples": [
                    {
                        "bug_id": e["bug_id"],
                        "title": e["title"][:60],
                        "bug_type": e["bug_type"],
                        "fix_patterns": e["fix_patterns"],
                        "patch_lines": e["patch_lines"],
                        "fix_days": round(e["fix_days"], 1) if e["fix_days"] is not None else None,
                        "iterations": e["iterations"],
                    }
                    for e in examples
                ],
            })

        # Phase 4: Overlap analysis
        overlap_matrix = []
        cluster_names = [c["name"] for c in INSIGHT_CLUSTERS if cluster_members[c["name"]]]
        for i, c1 in enumerate(cluster_names):
            s1 = {m["bug_id"] for m in cluster_members[c1]}
            for c2 in cluster_names[i + 1:]:
                s2 = {m["bug_id"] for m in cluster_members[c2]}
                inter = len(s1 & s2)
                if inter > 0:
                    overlap_matrix.append({
                        "cluster_1": c1,
                        "cluster_2": c2,
                        "overlap": inter,
                        "pct_of_smaller": f"{inter / min(len(s1), len(s2)) * 100:.1f}%",
                    })
        overlap_matrix.sort(key=lambda x: -x["overlap"])

        # Phase 5: Multi-cluster membership distribution
        membership_table = [
            {"clusters_per_bug": k, "bug_count": v}
            for k, v in sorted(bug_cluster_count.items())
        ]

        # Summary
        summary = {
            "Total bugs analyzed": total,
            "Clusters defined": len(INSIGHT_CLUSTERS),
            "Clusters with members": len([c for c in INSIGHT_CLUSTERS if cluster_members[c["name"]]]),
            "Bugs in 0 clusters": bug_cluster_count.get(0, 0),
            "Bugs in 1 cluster": bug_cluster_count.get(1, 0),
            "Bugs in 2+ clusters": sum(v for k, v in bug_cluster_count.items() if k >= 2),
        }

        return AnalysisResult(
            name=self.name,
            summary=summary,
            details=cluster_details,
            tables={
                "cluster_overview": cluster_table,
                "cluster_overlap": overlap_matrix[:20],
                "membership_distribution": membership_table,
            },
        )

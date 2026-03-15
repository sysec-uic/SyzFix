"""
Analyzer: Bug type / vulnerability class taxonomy.

Classifies each bug by type from the title and crash report using regex
pattern matching against known KASAN/KMSAN/KCSAN/kernel error signatures.
"""

import re
from collections import Counter, defaultdict
from typing import Any, Optional

from ..loader import BugEntry
from .base import BaseAnalyzer, AnalysisResult


# ─── Bug type taxonomy ─────────────────────────────────────────────────────
#
# Order matters: first match wins. More specific types come before generic ones.
# Each entry: (type_name, list_of_patterns_to_match_against_title_and_crash)

BUG_TYPE_PATTERNS: list[tuple[str, list[re.Pattern]]] = [
    # Memory safety — specific KASAN types
    ("use-after-free", [
        re.compile(r'use.after.free', re.I),
        re.compile(r'slab-use-after-free', re.I),
    ]),
    ("double-free", [
        re.compile(r'double.free', re.I),
        re.compile(r'KASAN:.*double-free', re.I),
    ]),
    ("out-of-bounds-write", [
        re.compile(r'out.of.bounds\s+Write', re.I),
        re.compile(r'slab-out-of-bounds.*Write', re.I),
        re.compile(r'stack-out-of-bounds.*Write', re.I),
        re.compile(r'global-buffer-overflow.*Write', re.I),
    ]),
    ("out-of-bounds-read", [
        re.compile(r'out.of.bounds\s+Read', re.I),
        re.compile(r'slab-out-of-bounds.*Read', re.I),
        re.compile(r'stack-out-of-bounds.*Read', re.I),
        re.compile(r'global-buffer-overflow.*Read', re.I),
    ]),
    ("out-of-bounds", [
        re.compile(r'out.of.bounds', re.I),
        re.compile(r'slab-out-of-bounds', re.I),
        re.compile(r'stack-out-of-bounds', re.I),
        re.compile(r'global-buffer-overflow', re.I),
    ]),
    ("null-ptr-deref", [
        re.compile(r'NULL pointer dereference', re.I),
        re.compile(r'null-ptr-deref', re.I),
        re.compile(r'general protection fault.*0000', re.I),
        re.compile(r'GPF.*NULL', re.I),
    ]),
    ("memory-leak", [
        re.compile(r'memory leak', re.I),
    ]),
    # Concurrency
    ("data-race", [
        re.compile(r'KCSAN:.*data-race', re.I),
        re.compile(r'BUG: KCSAN', re.I),
    ]),
    ("deadlock", [
        re.compile(r'possible circular locking', re.I),
        re.compile(r'possible deadlock', re.I),
        re.compile(r'inconsistent lock state', re.I),
    ]),
    ("task-hung", [
        re.compile(r'task hung', re.I),
        re.compile(r'hung_task', re.I),
        re.compile(r'blocked for more than.*seconds', re.I),
    ]),
    # Info leak
    ("info-leak", [
        re.compile(r'KMSAN:\s*uninit-value', re.I),
        re.compile(r'KMSAN:\s*kernel-\S*infoleak', re.I),
        re.compile(r'uninit-value', re.I),
        re.compile(r'infoleak', re.I),
    ]),
    # UBSAN
    ("ubsan", [
        re.compile(r'UBSAN:', re.I),
        re.compile(r'shift-out-of-bounds', re.I),
        re.compile(r'signed-integer-overflow', re.I),
        re.compile(r'undefined-behavior', re.I),
    ]),
    # Refcount
    ("refcount-bug", [
        re.compile(r'refcount_t:', re.I),
        re.compile(r'refcount.*underflow', re.I),
        re.compile(r'refcount.*saturated', re.I),
    ]),
    # Stack overflow
    ("stack-overflow", [
        re.compile(r'stack guard page', re.I),
        re.compile(r'stack-overflow', re.I),
        re.compile(r'kernel stack overflow', re.I),
    ]),
    # RCU stall
    ("rcu-stall", [
        re.compile(r'rcu.*stall', re.I),
        re.compile(r'INFO:.*rcu.*detected.*stall', re.I),
    ]),
    # Paging request (often a corrupted pointer, distinct from null-ptr)
    ("invalid-access", [
        re.compile(r'unable to handle kernel paging request', re.I),
        re.compile(r'BUG:\s*unable to handle kernel', re.I),
    ]),
    # Corrupted state
    ("corrupted-state", [
        re.compile(r'corrupted list', re.I),
        re.compile(r'list_add corruption', re.I),
        re.compile(r'list_del corruption', re.I),
        re.compile(r'spinlock.*bad magic', re.I),
        re.compile(r'bad.*page state', re.I),
    ]),
    # Generic assertions / warnings (must be last — catch-all)
    ("warning", [
        re.compile(r'^WARNING:', re.M),
        re.compile(r'WARNING:.*at\s+\S+', re.I),
        re.compile(r'WARNING\s+in\s+\w+', re.I),
    ]),
    ("kernel-bug", [
        re.compile(r'BUG:\s*kernel bug', re.I),
        re.compile(r'kernel BUG at', re.I),
        re.compile(r'kernel BUG in', re.I),
    ]),
    ("general-protection-fault", [
        re.compile(r'general protection fault', re.I),
    ]),
]


def classify_bug_type(title: str, crash_report: str) -> str:
    """Classify a bug into a type based on title and crash report.

    Returns the first matching type, or 'unknown'.
    """
    # Check title first (most reliable), then first 50 lines of crash report
    crash_head = "\n".join(crash_report.split("\n")[:50]) if crash_report else ""
    search_text = title + "\n" + crash_head

    for type_name, patterns in BUG_TYPE_PATTERNS:
        for pattern in patterns:
            if pattern.search(search_text):
                return type_name
    return "unknown"


def _get_patch_size(bug: BugEntry) -> Optional[int]:
    """Get total lines changed in the fix patch."""
    for fc in bug.fix_commits:
        diff = fc.patch_diff
        if not diff:
            continue
        additions = len(re.findall(r'^\+[^+]', diff, re.MULTILINE))
        deletions = len(re.findall(r'^-[^-]', diff, re.MULTILINE))
        return additions + deletions
    return None


def _get_time_to_fix_days(bug: BugEntry) -> Optional[float]:
    """Get time from first crash to fix in days."""
    from datetime import datetime
    first = bug.first_crash
    fix = bug.fix_time
    if not first or not fix:
        return None
    try:
        # Handle various datetime formats
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


class BugTypeClassifier(BaseAnalyzer):
    """Classify bugs by vulnerability/error type."""

    @property
    def name(self) -> str:
        return "Bug Type Classification"

    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        type_counter = Counter()
        type_bugs: dict[str, list] = defaultdict(list)

        # Per-type metrics
        type_patch_sizes: dict[str, list[int]] = defaultdict(list)
        type_iterations: dict[str, list[int]] = defaultdict(list)
        type_fix_times: dict[str, list[float]] = defaultdict(list)

        for bug in bugs:
            crash_report = bug.crash_report
            bug_type = classify_bug_type(bug.title, crash_report)

            type_counter[bug_type] += 1
            if len(type_bugs[bug_type]) < 5:
                type_bugs[bug_type].append({
                    "bug_id": bug.bug_id,
                    "title": bug.title,
                })

            # Collect metrics
            patch_size = _get_patch_size(bug)
            if patch_size is not None:
                type_patch_sizes[bug_type].append(patch_size)

            num_versions = bug.num_patch_versions
            if num_versions > 0:
                type_iterations[bug_type].append(num_versions)

            fix_days = _get_time_to_fix_days(bug)
            if fix_days is not None:
                type_fix_times[bug_type].append(fix_days)

        total = len(bugs)

        # Summary
        summary = {
            "Total bugs classified": total,
            "Distinct bug types": len(type_counter),
            "Top type": type_counter.most_common(1)[0][0] if type_counter else "N/A",
            "Unknown type": type_counter.get("unknown", 0),
        }

        # Distribution table
        dist_table = []
        for bug_type, count in type_counter.most_common():
            sizes = type_patch_sizes.get(bug_type, [])
            iters = type_iterations.get(bug_type, [])
            times = type_fix_times.get(bug_type, [])

            median_size = sorted(sizes)[len(sizes) // 2] if sizes else None
            median_iters = sorted(iters)[len(iters) // 2] if iters else None
            median_days = sorted(times)[len(times) // 2] if times else None

            dist_table.append({
                "bug_type": bug_type,
                "count": count,
                "pct": f"{count / total * 100:.1f}%",
                "median_patch_lines": median_size,
                "median_iterations": median_iters,
                "median_fix_days": round(median_days, 1) if median_days is not None else None,
            })

        # Per-bug classification for cross-analysis
        details = []
        for bug in bugs:
            bug_type = classify_bug_type(bug.title, bug.crash_report)
            details.append({
                "bug_id": bug.bug_id,
                "title": bug.title,
                "bug_type": bug_type,
            })

        return AnalysisResult(
            name=self.name,
            summary=summary,
            details=details,
            tables={
                "distribution": dist_table,
                "examples": {t: exs for t, exs in type_bugs.items()},
            },
        )

"""
Analyzer: Fix pattern taxonomy.

Classifies WHAT the patch does by analyzing the diff content —
e.g., adds a null check, adds locking, fixes refcounting, etc.
"""

import re
from collections import Counter, defaultdict
from typing import Any

from ..loader import BugEntry
from .base import BaseAnalyzer, AnalysisResult


# ─── Fix pattern taxonomy ──────────────────────────────────────────────────
#
# Each pattern is checked against (added_lines, removed_lines, commit_message).
# A bug can match multiple patterns.

def _extract_diff_lines(patch_diff: str) -> tuple[list[str], list[str]]:
    """Extract added and removed lines from a patch diff.

    Returns (added_lines, removed_lines) with the +/- prefix stripped.
    Skips the diff header lines (---, +++, diff --git).
    """
    added = []
    removed = []
    for line in patch_diff.split("\n"):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def _extract_hunk_functions(patch_diff: str) -> list[str]:
    """Extract function names from @@ hunk headers."""
    return re.findall(r'^@@.*@@\s+(\w+)', patch_diff, re.MULTILINE)


FIX_PATTERNS: dict[str, dict[str, Any]] = {
    "add-null-check": {
        "description": "Adds a NULL/IS_ERR check before dereferencing",
        "added_patterns": [
            re.compile(r'if\s*\(\s*![\w>.\-]+\s*\)', re.I),
            re.compile(r'if\s*\(\s*[\w>.\-]+\s*==\s*NULL', re.I),
            re.compile(r'if\s*\(\s*IS_ERR\s*\(', re.I),
            re.compile(r'if\s*\(\s*IS_ERR_OR_NULL\s*\(', re.I),
            re.compile(r'if\s*\(\s*unlikely\s*\(\s*!', re.I),
        ],
    },
    "add-lock": {
        "description": "Adds lock acquisition/release for synchronization",
        "added_patterns": [
            re.compile(r'spin_lock|spin_unlock', re.I),
            re.compile(r'mutex_lock|mutex_unlock', re.I),
            re.compile(r'rcu_read_lock|rcu_read_unlock', re.I),
            re.compile(r'read_lock|write_lock|read_unlock|write_unlock', re.I),
            re.compile(r'down_read|down_write|up_read|up_write', re.I),
            re.compile(r'local_irq_save|local_irq_restore', re.I),
        ],
    },
    "add-bounds-check": {
        "description": "Adds bounds/range validation",
        "added_patterns": [
            re.compile(r'if\s*\(.*[<>]=?\s*\w*(len|size|max|min|limit|count|nr|num)\b', re.I),
            re.compile(r'if\s*\(.*\b(overflow|underflow)\b', re.I),
            re.compile(r'min\s*\(|max\s*\(|clamp\s*\(', re.I),
            re.compile(r'if\s*\(.*>=\s*ARRAY_SIZE', re.I),
            re.compile(r'check_.*_overflow\s*\(', re.I),
        ],
    },
    "add-missing-free": {
        "description": "Adds missing resource release (kfree, put, release)",
        "added_patterns": [
            re.compile(r'\bkfree\s*\(', re.I),
            re.compile(r'\bkfree_skb\s*\(', re.I),
            re.compile(r'\bput_device\s*\(', re.I),
            re.compile(r'\bdev_put\s*\(', re.I),
            re.compile(r'\bfput\s*\(', re.I),
            re.compile(r'\brelease_firmware\s*\(', re.I),
            re.compile(r'\bfree_irq\s*\(', re.I),
            re.compile(r'\b\w+_free\s*\(', re.I),
            re.compile(r'\bvfree\s*\(', re.I),
        ],
    },
    "fix-refcount": {
        "description": "Fixes reference counting (get/put balance)",
        "added_patterns": [
            re.compile(r'\b\w*_get\s*\(|kref_get\s*\(|refcount_inc', re.I),
            re.compile(r'\b\w*_put\s*\(|kref_put\s*\(|refcount_dec', re.I),
            re.compile(r'\bcobalt_.*_get\s*\(|\bcobalt_.*_put\s*\(', re.I),
        ],
        "commit_patterns": [
            re.compile(r'refcount|reference.count|ref.?count|kref', re.I),
        ],
    },
    "add-init": {
        "description": "Adds missing variable/struct initialization",
        "added_patterns": [
            re.compile(r'=\s*0\s*;', re.I),
            re.compile(r'=\s*NULL\s*;', re.I),
            re.compile(r'=\s*false\s*;', re.I),
            re.compile(r'\bmemset\s*\(', re.I),
            re.compile(r'=\s*\{\s*\}\s*;'),
            re.compile(r'=\s*\{\s*0\s*\}\s*;'),
        ],
    },
    "add-return-check": {
        "description": "Adds error return value checking",
        "added_patterns": [
            re.compile(r'if\s*\(\s*\w+\s*[<>!=]=?\s*0\s*\)', re.I),
            re.compile(r'if\s*\(\s*err\b|if\s*\(\s*ret\b|if\s*\(\s*rc\b', re.I),
            re.compile(r'if\s*\(\s*result\s*[<>!=]', re.I),
        ],
    },
    "fix-order": {
        "description": "Reorders operations to fix race window or logic",
        "detect_fn": True,  # Detected by special logic below
    },
    "type-change": {
        "description": "Changes type (signed/unsigned, size_t, etc.)",
        "commit_patterns": [
            re.compile(r'(unsigned|signed|size_t|ssize_t|u32|u64|s32|s64)\b', re.I),
        ],
        "added_patterns": [
            re.compile(r'\b(unsigned|size_t|u32|u64|__u32|__u64)\b'),
        ],
    },
    "remove-code": {
        "description": "Net removal of code (more deletions than additions)",
        "detect_fn": True,  # Detected by special logic below
    },
}


def _detect_fix_order(added: list[str], removed: list[str]) -> bool:
    """Detect if the fix reorders operations (same content moved)."""
    if not added or not removed:
        return False
    added_stripped = {line.strip() for line in added if line.strip()}
    removed_stripped = {line.strip() for line in removed if line.strip()}
    overlap = added_stripped & removed_stripped
    # If significant overlap exists, lines were likely moved
    if len(overlap) >= 2:
        return True
    return False


def _detect_remove_code(added: list[str], removed: list[str]) -> bool:
    """Detect if the fix is primarily removing code."""
    # Filter out empty lines
    a = [l for l in added if l.strip()]
    r = [l for l in removed if l.strip()]
    return len(r) > len(a) + 3  # Net removal of at least 3 lines


def classify_fix_patterns(bug: BugEntry) -> list[str]:
    """Classify a bug's fix into zero or more fix patterns."""
    patterns_found = []

    # Get the fix diff
    diff = ""
    commit_msg = ""
    for fc in bug.fix_commits:
        if fc.patch_diff:
            diff = fc.patch_diff
            commit_msg = fc.title
            break

    if not diff:
        return []

    added, removed = _extract_diff_lines(diff)
    added_text = "\n".join(added)

    for pattern_name, config in FIX_PATTERNS.items():
        matched = False

        # Check added-line patterns
        for pat in config.get("added_patterns", []):
            if pat.search(added_text):
                matched = True
                break

        # Check commit message patterns
        if not matched:
            for pat in config.get("commit_patterns", []):
                if pat.search(commit_msg):
                    matched = True
                    break

        # Special detection functions
        if not matched and config.get("detect_fn"):
            if pattern_name == "fix-order":
                matched = _detect_fix_order(added, removed)
            elif pattern_name == "remove-code":
                matched = _detect_remove_code(added, removed)

        if matched:
            patterns_found.append(pattern_name)

    return patterns_found


class FixPatternAnalyzer(BaseAnalyzer):
    """Classify what fix patches actually do."""

    @property
    def name(self) -> str:
        return "Fix Pattern Taxonomy"

    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        pattern_counter = Counter()
        pattern_examples: dict[str, list] = defaultdict(list)
        bugs_with_diff = 0
        bugs_no_pattern = 0
        per_bug_patterns = []

        # Per-pattern patch sizes
        pattern_sizes: dict[str, list[int]] = defaultdict(list)

        for bug in bugs:
            # Check if bug has a fix diff
            has_diff = any(fc.patch_diff for fc in bug.fix_commits)
            if not has_diff:
                continue
            bugs_with_diff += 1

            patterns = classify_fix_patterns(bug)
            if not patterns:
                bugs_no_pattern += 1

            per_bug_patterns.append({
                "bug_id": bug.bug_id,
                "title": bug.title,
                "patterns": patterns,
            })

            # Get patch size for this bug
            patch_size = None
            for fc in bug.fix_commits:
                if fc.patch_diff:
                    added = len(re.findall(r'^\+[^+]', fc.patch_diff, re.MULTILINE))
                    removed = len(re.findall(r'^-[^-]', fc.patch_diff, re.MULTILINE))
                    patch_size = added + removed
                    break

            for pat in patterns:
                pattern_counter[pat] += 1
                if patch_size is not None:
                    pattern_sizes[pat].append(patch_size)
                if len(pattern_examples[pat]) < 3:
                    pattern_examples[pat].append({
                        "bug_id": bug.bug_id,
                        "title": bug.title,
                        "fix": bug.fix_commits[0].title if bug.fix_commits else "",
                    })

        # Summary
        summary = {
            "Bugs with fix diff": bugs_with_diff,
            "Bugs with at least one pattern": bugs_with_diff - bugs_no_pattern,
            "Bugs with no pattern matched": bugs_no_pattern,
            "Distinct patterns matched": len(pattern_counter),
            "Avg patterns per bug": round(
                sum(len(p["patterns"]) for p in per_bug_patterns) / max(bugs_with_diff, 1), 2
            ),
        }

        # Pattern distribution table
        dist_table = []
        for pat, count in pattern_counter.most_common():
            sizes = pattern_sizes.get(pat, [])
            median_size = sorted(sizes)[len(sizes) // 2] if sizes else None
            dist_table.append({
                "pattern": pat,
                "count": count,
                "pct_of_bugs_with_diff": f"{count / max(bugs_with_diff, 1) * 100:.1f}%",
                "median_patch_lines": median_size,
                "description": FIX_PATTERNS[pat]["description"],
            })

        # Coverage: top-K patterns cover what fraction?
        cumulative = 0
        coverage_table = []
        for pat, count in pattern_counter.most_common():
            cumulative += count
            coverage_table.append({
                "pattern": pat,
                "cumulative_bugs": cumulative,
                "cumulative_pct": f"{cumulative / max(bugs_with_diff, 1) * 100:.1f}%",
            })

        # Co-occurrence
        cooccurrence = Counter()
        for entry in per_bug_patterns:
            pats = sorted(set(entry["patterns"]))
            for i in range(len(pats)):
                for j in range(i + 1, len(pats)):
                    cooccurrence[(pats[i], pats[j])] += 1

        cooccur_table = []
        for (p1, p2), count in sorted(cooccurrence.items(), key=lambda x: -x[1])[:15]:
            cooccur_table.append({
                "pattern_1": p1,
                "pattern_2": p2,
                "count": count,
            })

        return AnalysisResult(
            name=self.name,
            summary=summary,
            details=per_bug_patterns,
            tables={
                "distribution": dist_table,
                "coverage": coverage_table,
                "cooccurrence": cooccur_table,
                "examples": {p: exs for p, exs in pattern_examples.items()},
            },
        )

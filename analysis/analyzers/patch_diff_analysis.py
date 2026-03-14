"""
Analyzer: Structural diff analysis between patch versions.

Quantitative analysis comparing v1 vs v2 (and vs final merged) patches:
- Diff size changes (lines added/removed)
- File scope changes (files modified)
- Growth vs shrink patterns
"""

import re
from collections import Counter
from typing import Optional

from ..loader import BugEntry, Discussion
from ..filters import is_patch_submission
from .base import BaseAnalyzer, AnalysisResult


def parse_diff_stats(diff_text: str) -> dict:
    """Parse a diff to extract statistics."""
    if not diff_text:
        return {"files": [], "additions": 0, "deletions": 0, "hunks": 0}

    files = re.findall(r'diff --git a/(\S+)', diff_text)
    additions = len(re.findall(r'^\+[^+]', diff_text, re.MULTILINE))
    deletions = len(re.findall(r'^-[^-]', diff_text, re.MULTILINE))
    hunks = len(re.findall(r'^@@', diff_text, re.MULTILINE))

    return {
        "files": files,
        "additions": additions,
        "deletions": deletions,
        "total_lines": additions + deletions,
        "hunks": hunks,
    }


def extract_diff_from_patch_message(messages: list) -> Optional[str]:
    """Extract the diff from a patch submission message."""
    for msg in messages:
        if is_patch_submission(msg):
            # Look for diff content in the body
            body = msg.body
            diff_start = body.find('diff --git')
            if diff_start >= 0:
                return body[diff_start:]
    return None


class PatchDiffAnalyzer(BaseAnalyzer):
    """Analyze structural changes between patch versions."""

    @property
    def name(self) -> str:
        return "Patch Structural Diff Analysis"

    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        multi_version = [b for b in bugs if b.has_multiple_versions]

        size_changes = []   # (bug_id, v1_lines, v2_lines, delta)
        file_changes = []   # (bug_id, v1_files, v2_files, added_files, removed_files)
        growth_count = 0
        shrink_count = 0
        same_count = 0
        scope_expanded = 0
        scope_narrowed = 0
        scope_same = 0

        for bug in multi_version:
            pvs = bug.patch_versions
            if len(pvs) < 2:
                continue

            v1_diff = extract_diff_from_patch_message(pvs[0].messages)
            v2_diff = extract_diff_from_patch_message(pvs[1].messages)

            if not v1_diff or not v2_diff:
                continue

            v1_stats = parse_diff_stats(v1_diff)
            v2_stats = parse_diff_stats(v2_diff)

            delta = v2_stats["total_lines"] - v1_stats["total_lines"]
            size_changes.append({
                "bug_id": bug.bug_id,
                "title": bug.title,
                "v1_lines": v1_stats["total_lines"],
                "v2_lines": v2_stats["total_lines"],
                "delta": delta,
                "v1_files": len(v1_stats["files"]),
                "v2_files": len(v2_stats["files"]),
            })

            if delta > 0:
                growth_count += 1
            elif delta < 0:
                shrink_count += 1
            else:
                same_count += 1

            v1_file_set = set(v1_stats["files"])
            v2_file_set = set(v2_stats["files"])
            added = v2_file_set - v1_file_set
            removed = v1_file_set - v2_file_set

            if added and not removed:
                scope_expanded += 1
            elif removed and not added:
                scope_narrowed += 1
            elif not added and not removed:
                scope_same += 1
            else:
                scope_expanded += 1  # mixed = generally expanded

            if added or removed:
                file_changes.append({
                    "bug_id": bug.bug_id,
                    "title": bug.title,
                    "v1_files": sorted(v1_file_set),
                    "v2_files": sorted(v2_file_set),
                    "added": sorted(added),
                    "removed": sorted(removed),
                })

        total_compared = len(size_changes)
        avg_delta = sum(s["delta"] for s in size_changes) / total_compared if total_compared else 0
        avg_v1 = sum(s["v1_lines"] for s in size_changes) / total_compared if total_compared else 0
        avg_v2 = sum(s["v2_lines"] for s in size_changes) / total_compared if total_compared else 0

        summary = {
            "Multi-version bugs": len(multi_version),
            "Bugs with comparable diffs": total_compared,
            "Avg v1 patch size (lines)": round(avg_v1, 1),
            "Avg v2 patch size (lines)": round(avg_v2, 1),
            "Avg size change v1→v2": round(avg_delta, 1),
            "Patches that grew": growth_count,
            "Patches that shrank": shrink_count,
            "Patches same size": same_count,
            "File scope expanded": scope_expanded,
            "File scope narrowed": scope_narrowed,
            "File scope unchanged": scope_same,
        }

        # Top growers and shrinkers
        sorted_by_delta = sorted(size_changes, key=lambda x: -abs(x["delta"]))

        return AnalysisResult(
            name=self.name,
            summary=summary,
            details=sorted_by_delta[:20],
            tables={
                "size_changes": size_changes,
                "file_scope_changes": file_changes[:20],
            },
        )

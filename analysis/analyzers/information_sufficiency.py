"""
Analyzer: Information sufficiency.

Analyzes what input signals are available and how they correlate with
fix properties — reproducer availability, crash report content,
token overlap between inputs and patch.
"""

import re
from collections import Counter, defaultdict
from typing import Optional

from ..loader import BugEntry
from ..filters import parse_stack_trace
from .base import BaseAnalyzer, AnalysisResult


def _tokenize(text: str) -> set[str]:
    """Simple whitespace + punctuation tokenizer for overlap analysis."""
    return set(re.findall(r'[a-zA-Z_]\w+', text))


def _get_fix_files(patch_diff: str) -> list[str]:
    """Extract file paths from diff headers."""
    return re.findall(r'diff --git a/(\S+)', patch_diff)


def _get_crash_files(crash_report: str) -> list[str]:
    """Extract file paths from stack trace."""
    frames = parse_stack_trace(crash_report)
    return [f.file for f in frames if f.file]


def _jaccard(set_a: set, set_b: set) -> float:
    """Jaccard similarity between two sets."""
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _parse_time_days(bug: BugEntry) -> Optional[float]:
    """Parse time-to-fix in days."""
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


class InformationSufficiencyAnalyzer(BaseAnalyzer):
    """Analyze what input information is available and useful."""

    @property
    def name(self) -> str:
        return "Information Sufficiency Analysis"

    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        # Reproducer availability
        repro_types = Counter()  # c_and_syz, c_only, syz_only, none
        repro_fix_times: dict[str, list[float]] = defaultdict(list)
        repro_iterations: dict[str, list[int]] = defaultdict(list)
        repro_patch_sizes: dict[str, list[int]] = defaultdict(list)

        # Crash report stats
        crash_lengths = []
        crash_has_stack = 0
        crash_has_kasan = 0

        # Token overlap
        crash_patch_overlaps = []
        repro_patch_overlaps = []

        # File path prediction
        file_pred_same_function = 0
        file_pred_same_file = 0
        file_pred_total = 0

        # Truncation analysis: for each bug, what fraction of stack trace
        # file references appear in first N lines?
        truncation_data = []  # list of (total_frames_in_report, frames_by_line_N)

        for bug in bugs:
            crash = bug.crash_report
            c_repro = bug.c_reproducer
            # syz reproducer: check raw crashes
            syz_repro = ""
            for cr in bug.raw.get("crashes", []):
                if cr.get("syz_reproducer"):
                    syz_repro = cr["syz_reproducer"]
                    break

            # Classify reproducer availability
            has_c = bool(c_repro and len(c_repro) > 20)
            has_syz = bool(syz_repro and len(syz_repro) > 20)
            if has_c and has_syz:
                repro_type = "c_and_syz"
            elif has_c:
                repro_type = "c_only"
            elif has_syz:
                repro_type = "syz_only"
            else:
                repro_type = "none"
            repro_types[repro_type] += 1

            # Correlate with fix properties
            fix_days = _parse_time_days(bug)
            if fix_days is not None:
                repro_fix_times[repro_type].append(fix_days)

            num_v = bug.num_patch_versions
            if num_v > 0:
                repro_iterations[repro_type].append(num_v)

            patch_size = 0
            patch_diff = ""
            for fc in bug.fix_commits:
                if fc.patch_diff:
                    patch_diff = fc.patch_diff
                    added = len(re.findall(r'^\+[^+]', fc.patch_diff, re.MULTILINE))
                    removed = len(re.findall(r'^-[^-]', fc.patch_diff, re.MULTILINE))
                    patch_size = added + removed
                    break
            if patch_size > 0:
                repro_patch_sizes[repro_type].append(patch_size)

            # Crash report stats
            if crash:
                crash_lengths.append(len(crash))
                frames = parse_stack_trace(crash)
                if frames:
                    crash_has_stack += 1
                if re.search(r'KASAN:|KMSAN:|KCSAN:', crash):
                    crash_has_kasan += 1

                # Truncation analysis
                if frames:
                    total_frames = len(frames)
                    lines = crash.split("\n")
                    frames_by_n = {}
                    for n in [10, 20, 30, 50, 100]:
                        truncated = "\n".join(lines[:n])
                        truncated_frames = parse_stack_trace(truncated)
                        frames_by_n[n] = len(truncated_frames)
                    truncation_data.append({
                        "total": total_frames,
                        **{f"first_{n}_lines": frames_by_n[n] for n in [10, 20, 30, 50, 100]},
                    })

            # Token overlap
            if crash and patch_diff:
                crash_tokens = _tokenize(crash)
                patch_tokens = _tokenize(patch_diff)
                crash_patch_overlaps.append(_jaccard(crash_tokens, patch_tokens))

            if c_repro and patch_diff:
                repro_tokens = _tokenize(c_repro)
                patch_tokens = _tokenize(patch_diff)
                repro_patch_overlaps.append(_jaccard(repro_tokens, patch_tokens))

            # File path prediction
            if crash and patch_diff:
                crash_files = set(_get_crash_files(crash))
                fix_files = set(_get_fix_files(patch_diff))
                if crash_files and fix_files:
                    file_pred_total += 1
                    # Check basename match
                    import os
                    crash_basenames = {os.path.basename(f) for f in crash_files}
                    fix_basenames = {os.path.basename(f) for f in fix_files}
                    if crash_files & fix_files:
                        file_pred_same_file += 1
                    elif crash_basenames & fix_basenames:
                        file_pred_same_file += 1

        total = len(bugs)

        # ── Build summary ──────────────────────────────────────────────

        def _median(lst):
            if not lst:
                return None
            s = sorted(lst)
            return s[len(s) // 2]

        summary = {
            "Total bugs": total,
            "With C reproducer": repro_types.get("c_and_syz", 0) + repro_types.get("c_only", 0),
            "With syz reproducer only": repro_types.get("syz_only", 0),
            "No reproducer": repro_types.get("none", 0),
            "Crash reports with stack trace": crash_has_stack,
            "Crash reports with sanitizer output": crash_has_kasan,
            "Median crash report length (chars)": _median(crash_lengths),
            "Median crash↔patch token overlap (Jaccard)": round(_median(crash_patch_overlaps) or 0, 4),
            "Median repro↔patch token overlap (Jaccard)": round(_median(repro_patch_overlaps) or 0, 4),
            "File path prediction accuracy (crash→fix)": (
                f"{file_pred_same_file}/{file_pred_total} "
                f"({file_pred_same_file / max(file_pred_total, 1) * 100:.1f}%)"
            ),
        }

        # ── Tables ─────────────────────────────────────────────────────

        # Reproducer type impact
        repro_table = []
        for rtype in ["c_and_syz", "c_only", "syz_only", "none"]:
            count = repro_types.get(rtype, 0)
            times = repro_fix_times.get(rtype, [])
            iters = repro_iterations.get(rtype, [])
            sizes = repro_patch_sizes.get(rtype, [])
            repro_table.append({
                "reproducer_type": rtype,
                "count": count,
                "pct": f"{count / max(total, 1) * 100:.1f}%",
                "median_fix_days": round(_median(times), 1) if _median(times) is not None else None,
                "median_iterations": _median(iters),
                "median_patch_lines": _median(sizes),
            })

        # Truncation analysis
        trunc_table = []
        for n in [10, 20, 30, 50, 100]:
            key = f"first_{n}_lines"
            ratios = []
            for td in truncation_data:
                if td["total"] > 0:
                    ratios.append(td[key] / td["total"])
            if ratios:
                median_ratio = sorted(ratios)[len(ratios) // 2]
                trunc_table.append({
                    "first_n_lines": n,
                    "median_stack_frame_retention": f"{median_ratio * 100:.1f}%",
                    "bugs_analyzed": len(ratios),
                })

        # Token overlap distribution (binned)
        overlap_bins = Counter()
        for ov in crash_patch_overlaps:
            bin_label = f"{int(ov * 100) // 5 * 5}-{int(ov * 100) // 5 * 5 + 5}%"
            overlap_bins[bin_label] += 1
        overlap_table = [
            {"range": k, "count": v}
            for k, v in sorted(overlap_bins.items())
        ]

        return AnalysisResult(
            name=self.name,
            summary=summary,
            details=[],
            tables={
                "reproducer_impact": repro_table,
                "truncation_analysis": trunc_table,
                "token_overlap_distribution": overlap_table,
            },
        )

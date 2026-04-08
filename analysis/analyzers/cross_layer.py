"""
Analyzer: Cross-layer bugs — bugs where crash and fix are in different
architectural layers of the same kernel subsystem domain.

For example, a crash in ext4 (specific filesystem) fixed in VFS core,
or a crash in net core fixed in a specific protocol implementation.
These bugs require architectural understanding of the kernel's layered
design to locate the correct fix site.
"""

import re
from collections import Counter, defaultdict
from typing import Optional

from ..loader import BugEntry
from ..filters import parse_stack_trace
from .base import BaseAnalyzer, AnalysisResult
from .kernel_layers import (
    classify_file_layer, classify_files, get_layer_label, DOMAINS,
)


def _extract_fix_files(patch_diff: str) -> list[str]:
    """Extract file paths modified by the patch."""
    return re.findall(r'diff --git a/(\S+)', patch_diff)


def compute_cross_layer(
    crash_report: str, patch_diff: str,
) -> Optional[dict]:
    """Determine if a bug is cross-layer.

    Compares the architectural layer of crash-site files (from stack trace)
    against fix-site files (from patch diff) within each subsystem domain.

    Returns a dict describing the cross-layer relationship, or None if
    the bug can't be analyzed (missing data).
    """
    if not crash_report or not patch_diff:
        return None

    frames = parse_stack_trace(crash_report)
    if not frames:
        return None

    fix_files = _extract_fix_files(patch_diff)
    if not fix_files:
        return None

    crash_files = [f.file for f in frames if f.file]
    if not crash_files:
        return None

    # Classify all files by domain.
    # For crash files, preserve stack order (top = crash site) so we can
    # determine which layer the bug actually manifests in.
    crash_by_domain = classify_files(crash_files)
    fix_by_domain = classify_files(fix_files)

    # Build ordered crash classification preserving stack-trace order
    # (first = top of stack = actual crash site)
    crash_ordered: list[tuple[str, str, str, int]] = []  # (file, domain, layer, level)
    for f in crash_files:
        result = classify_file_layer(f)
        if result:
            crash_ordered.append((f, result[0], result[1], result[2]))

    # Find domains present in both crash and fix
    shared_domains = set(crash_by_domain.keys()) & set(fix_by_domain.keys())

    if not shared_domains:
        # Crash and fix don't share any domain — not cross-layer
        # (might be cross-subsystem, handled by fix_locality analyzer)
        return {
            "is_cross_layer": False,
            "reason": "no_shared_domain",
            "crash_files": crash_files[:5],
            "fix_files": fix_files,
            "crash_domains": sorted(crash_by_domain.keys()),
            "fix_domains": sorted(fix_by_domain.keys()),
        }

    # Check each shared domain for layer level differences.
    # Cross-layer means the crash and fix are at DIFFERENT architectural
    # levels (e.g., specific FS vs VFS core), not just different components
    # at the same level (e.g., ipv4 vs ipv6 are both protocol-level).
    cross_layer_findings = []

    for domain_name in sorted(shared_domains):
        crash_entries = crash_by_domain[domain_name]
        fix_entries = fix_by_domain[domain_name]

        # Determine the crash layer from the TOP of the stack trace
        # (where the bug actually manifests), not the majority of frames.
        # The stack trace goes: crash site → callers → ... → syscall entry,
        # so the first classified frame in this domain is the crash layer.
        primary_crash = None
        for _, dom, ln, lv in crash_ordered:
            if dom == domain_name:
                primary_crash = (ln, lv)
                break
        if primary_crash is None:
            # Fallback to most common
            crash_layer_counts = Counter(
                (ln, lv) for _, ln, lv in crash_entries
            )
            primary_crash = crash_layer_counts.most_common(1)[0][0]

        # For fix files, use the most common layer (no ordering bias)
        fix_layer_counts = Counter(
            (ln, lv) for _, ln, lv in fix_entries
        )
        primary_fix = fix_layer_counts.most_common(1)[0][0]

        # Only report if the primary layers are at different levels
        if primary_crash[1] == primary_fix[1]:
            continue

        # Determine direction
        if primary_fix[1] < primary_crash[1]:
            direction = "fix_in_upper_layer"
        else:
            direction = "fix_in_lower_layer"

        cross_layer_findings.append({
            "domain": domain_name,
            "crash_layer": get_layer_label(
                domain_name, primary_crash[0], primary_crash[1]
            ),
            "crash_layer_level": primary_crash[1],
            "fix_layer": get_layer_label(
                domain_name, primary_fix[0], primary_fix[1]
            ),
            "fix_layer_level": primary_fix[1],
            "direction": direction,
            "crash_files_in_domain": [p for p, _, _ in crash_entries],
            "fix_files_in_domain": [p for p, _, _ in fix_entries],
        })

    if not cross_layer_findings:
        return {
            "is_cross_layer": False,
            "reason": "same_layer",
            "crash_files": crash_files[:5],
            "fix_files": fix_files,
            "shared_domains": sorted(shared_domains),
        }

    # Use the first (most important) finding as primary
    primary = cross_layer_findings[0]

    return {
        "is_cross_layer": True,
        "domain": primary["domain"],
        "crash_layer": primary["crash_layer"],
        "crash_layer_level": primary["crash_layer_level"],
        "fix_layer": primary["fix_layer"],
        "fix_layer_level": primary["fix_layer_level"],
        "direction": primary["direction"],
        "all_findings": cross_layer_findings,
        "crash_files": crash_files[:5],
        "fix_files": fix_files,
    }


class CrossLayerAnalyzer(BaseAnalyzer):
    """Identify bugs where crash and fix are in different architectural layers."""

    @property
    def name(self) -> str:
        return "Cross-Layer Analysis"

    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        details = []
        domain_counter = Counter()
        direction_counter = Counter()
        domain_direction = Counter()  # (domain, direction) pairs
        cross_layer_examples: dict[str, list] = defaultdict(list)

        analyzed = 0
        cross_layer_count = 0
        skipped = 0
        no_shared_domain = 0

        for bug in bugs:
            crash_report = bug.crash_report
            patch_diff = ""
            for fc in bug.fix_commits:
                if fc.patch_diff:
                    patch_diff = fc.patch_diff
                    break

            result = compute_cross_layer(crash_report, patch_diff)
            if result is None:
                skipped += 1
                continue

            analyzed += 1

            if not result["is_cross_layer"]:
                if result.get("reason") == "no_shared_domain":
                    no_shared_domain += 1
                details.append({
                    "bug_id": bug.bug_id,
                    "title": bug.title,
                    "is_cross_layer": False,
                })
                continue

            cross_layer_count += 1
            domain = result["domain"]
            direction = result["direction"]

            domain_counter[domain] += 1
            direction_counter[direction] += 1
            domain_direction[(domain, direction)] += 1

            detail = {
                "bug_id": bug.bug_id,
                "title": bug.title,
                "is_cross_layer": True,
                "domain": domain,
                "crash_layer": result["crash_layer"],
                "fix_layer": result["fix_layer"],
                "direction": direction,
            }
            details.append(detail)

            # Collect examples per domain
            if len(cross_layer_examples[domain]) < 5:
                cross_layer_examples[domain].append({
                    **detail,
                    "crash_files": result["crash_files"][:3],
                    "fix_files": result["fix_files"][:3],
                })

        # Build summary
        total = len(bugs)
        summary = {
            "Total bugs": total,
            "Analyzed (have stack trace + patch)": analyzed,
            "Skipped (missing data)": skipped,
            "Cross-layer bugs": f"{cross_layer_count} ({cross_layer_count / max(analyzed, 1) * 100:.1f}%)",
            "Same-layer bugs": f"{analyzed - cross_layer_count - no_shared_domain}",
            "No shared domain (cross-subsystem)": no_shared_domain,
        }

        # Per-domain breakdown
        for domain_name in sorted(domain_counter.keys()):
            count = domain_counter[domain_name]
            summary[f"  {domain_name}"] = (
                f"{count} ({count / max(cross_layer_count, 1) * 100:.1f}%)"
            )

        # Per-direction breakdown
        summary["---"] = "--- Direction ---"
        for direction in ["fix_in_upper_layer", "fix_in_lower_layer"]:
            count = direction_counter.get(direction, 0)
            summary[f"  {direction}"] = (
                f"{count} ({count / max(cross_layer_count, 1) * 100:.1f}%)"
            )

        # Distribution table: domain x direction
        dist_table = []
        for (domain, direction), count in sorted(
            domain_direction.items(), key=lambda x: -x[1]
        ):
            dist_table.append({
                "domain": domain,
                "direction": direction,
                "count": count,
                "pct_of_cross_layer": (
                    f"{count / max(cross_layer_count, 1) * 100:.1f}%"
                ),
            })

        # Examples table
        examples_table = {}
        for domain, exs in cross_layer_examples.items():
            examples_table[domain] = exs

        return AnalysisResult(
            name=self.name,
            summary=summary,
            details=details,
            tables={
                "distribution": dist_table,
                "examples": examples_table,
            },
        )

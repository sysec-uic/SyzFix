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


def _lines_changed_per_file(patch_diff: str) -> dict[str, int]:
    """Count changed lines (+ and -, excluding headers) per fix file.

    Walks the diff linearly, tracking which file the current hunk belongs
    to. Used as a weight for deciding the semantically primary fix layer
    when a patch touches multiple files in different layers — a one-line
    VFS fix bundled with bulk churn in a specific filesystem should still
    be labeled as a VFS fix if VFS is where the actual behavioral change
    lives, but the more defensible tie-breaker is line count.
    """
    counts: dict[str, int] = {}
    current: str | None = None
    for line in patch_diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r'diff --git a/(\S+)', line)
            current = m.group(1) if m else None
            continue
        if current is None:
            continue
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            counts[current] = counts.get(current, 0) + 1
    return counts


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

    crash_frames_with_file = [f for f in frames if f.file]
    crash_files = [f.file for f in crash_frames_with_file]
    if not crash_files:
        return None

    # Determine stack overlap: does any fix file appear in the crash stack?
    crash_file_set = set(crash_files)
    fix_on_stack = [f for f in fix_files if f in crash_file_set]
    fix_off_stack = [f for f in fix_files if f not in crash_file_set]
    if fix_on_stack:
        stack_overlap = "fix_on_stack"
    else:
        stack_overlap = "fix_off_stack"

    # Classify all files by domain.
    crash_by_domain = classify_files(crash_files)
    fix_by_domain = classify_files(fix_files)

    # Build ordered crash classification preserving stack-trace order and
    # is_inline flag, so we can prefer real (non-inline) frames when
    # picking the layer at which the bug manifests.
    crash_ordered: list[tuple[str, str, str, int, bool]] = []
    for frame in crash_frames_with_file:
        result = classify_file_layer(frame.file)
        if result:
            crash_ordered.append(
                (frame.file, result[0], result[1], result[2], frame.is_inline)
            )

    # Per-file changed-line counts — used as tie-break weights for the
    # primary fix layer (A2).
    lines_per_file = _lines_changed_per_file(patch_diff)

    # Find domains present in both crash and fix
    shared_domains = set(crash_by_domain.keys()) & set(fix_by_domain.keys())

    if not shared_domains:
        # Crash and fix classify into disjoint architectural domains.
        # Previously we returned is_cross_layer=False with no further
        # metadata; we now additionally label this as `relation=cross_domain`
        # and emit the majority crash/fix domain so downstream patch-location
        # prediction can use these bugs (A4).
        crash_domain_counts = Counter(
            dom for _, dom, _, _, _ in crash_ordered
        )
        fix_domain_counts = Counter(
            (dom, lines_per_file.get(p, 1))
            for dom, entries in fix_by_domain.items()
            for p, _, _ in entries
        )
        fix_domain_weighted: Counter = Counter()
        for dom, entries in fix_by_domain.items():
            for p, _, _ in entries:
                fix_domain_weighted[dom] += max(lines_per_file.get(p, 1), 1)
        crash_domain = (
            crash_domain_counts.most_common(1)[0][0]
            if crash_domain_counts else ""
        )
        fix_domain = (
            fix_domain_weighted.most_common(1)[0][0]
            if fix_domain_weighted else ""
        )
        return {
            "is_cross_layer": False,
            "relation": "cross_domain",
            "reason": "no_shared_domain",
            "stack_overlap": stack_overlap,
            "fix_on_stack_files": fix_on_stack,
            "fix_off_stack_files": fix_off_stack,
            "crash_files": crash_files[:5],
            "fix_files": fix_files,
            "crash_domains": sorted(crash_by_domain.keys()),
            "fix_domains": sorted(fix_by_domain.keys()),
            "crash_domain": crash_domain,
            "fix_domain": fix_domain,
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
        # Prefer the first *non-inline* frame in this domain within the
        # top 5 classified frames; fall back to the first classified
        # frame (inline or not) if no non-inline frame is available (A5).
        domain_frames = [
            (ln, lv, is_inline)
            for _, dom, ln, lv, is_inline in crash_ordered
            if dom == domain_name
        ]
        primary_crash = None
        for ln, lv, is_inline in domain_frames[:5]:
            if not is_inline:
                primary_crash = (ln, lv)
                break
        if primary_crash is None and domain_frames:
            primary_crash = (domain_frames[0][0], domain_frames[0][1])
        if primary_crash is None:
            # Fallback to most common
            crash_layer_counts = Counter(
                (ln, lv) for _, ln, lv in crash_entries
            )
            primary_crash = crash_layer_counts.most_common(1)[0][0]

        # For fix files, pick the layer with the most *changed lines*
        # across files in this domain, not simply the most files (A2).
        # Tie-break by preferring a layer that also appears on the crash
        # stack, so a one-line VFS fix bundled with bulk churn in a
        # specific filesystem is still labeled as touching VFS.
        fix_layer_weights: Counter = Counter()
        for p, ln, lv in fix_entries:
            fix_layer_weights[(ln, lv)] += max(lines_per_file.get(p, 1), 1)
        top_weight = max(fix_layer_weights.values())
        top_layers = [k for k, w in fix_layer_weights.items() if w == top_weight]
        if len(top_layers) > 1:
            crash_layer_set = {(ln, lv) for ln, lv, _ in domain_frames}
            preferred = [k for k in top_layers if k in crash_layer_set]
            primary_fix = preferred[0] if preferred else top_layers[0]
        else:
            primary_fix = top_layers[0]

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
            "relation": "same_layer",
            "reason": "same_layer",
            "stack_overlap": stack_overlap,
            "fix_on_stack_files": fix_on_stack,
            "fix_off_stack_files": fix_off_stack,
            "crash_files": crash_files[:5],
            "fix_files": fix_files,
            "shared_domains": sorted(shared_domains),
        }

    # Use the first (most important) finding as primary
    primary = cross_layer_findings[0]

    return {
        "is_cross_layer": True,
        "relation": "cross_layer",
        "domain": primary["domain"],
        "crash_layer": primary["crash_layer"],
        "crash_layer_level": primary["crash_layer_level"],
        "fix_layer": primary["fix_layer"],
        "fix_layer_level": primary["fix_layer_level"],
        "direction": primary["direction"],
        "stack_overlap": stack_overlap,
        "fix_on_stack_files": fix_on_stack,
        "fix_off_stack_files": fix_off_stack,
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
        stack_overlap_counter = Counter()  # fix_on_stack vs fix_off_stack
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
                relation = result.get("relation", "")
                if relation == "cross_domain":
                    no_shared_domain += 1
                # Still record file-level info so downstream consumers
                # (e.g. the crash_to_patch_location training task) can
                # use same-layer / cross-domain bugs as supervision.
                detail_record = {
                    "bug_id": bug.bug_id,
                    "title": bug.title,
                    "is_cross_layer": False,
                    "relation": relation,
                    "reason": result.get("reason", ""),
                    "stack_overlap": result.get("stack_overlap", ""),
                    "fix_on_stack_files": result.get("fix_on_stack_files", []),
                    "fix_off_stack_files": result.get("fix_off_stack_files", []),
                }
                if relation == "cross_domain":
                    detail_record["crash_domain"] = result.get("crash_domain", "")
                    detail_record["fix_domain"] = result.get("fix_domain", "")
                    detail_record["crash_domains"] = result.get("crash_domains", [])
                    detail_record["fix_domains"] = result.get("fix_domains", [])
                details.append(detail_record)
                continue

            cross_layer_count += 1
            domain = result["domain"]
            direction = result["direction"]

            domain_counter[domain] += 1
            direction_counter[direction] += 1
            domain_direction[(domain, direction)] += 1

            overlap = result.get("stack_overlap", "unknown")
            stack_overlap_counter[overlap] += 1

            detail = {
                "bug_id": bug.bug_id,
                "title": bug.title,
                "is_cross_layer": True,
                "relation": "cross_layer",
                "domain": domain,
                "crash_layer": result["crash_layer"],
                "fix_layer": result["fix_layer"],
                "direction": direction,
                "stack_overlap": overlap,
                "fix_on_stack_files": result.get("fix_on_stack_files", []),
                "fix_off_stack_files": result.get("fix_off_stack_files", []),
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

        # Stack overlap breakdown
        summary["----"] = "--- Stack Overlap ---"
        for overlap in ["fix_on_stack", "fix_off_stack"]:
            count = stack_overlap_counter.get(overlap, 0)
            label = "Fix ON crash stack (stack-reachable)" if overlap == "fix_on_stack" \
                else "Fix OFF crash stack (true cross-layer)"
            summary[f"  {label}"] = (
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

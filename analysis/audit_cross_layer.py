"""Hand-audit sampler for the cross-layer analyzer.

Samples bugs stratified across four label buckets and emits a Markdown
file with crash top frames, fix files, and the classified layers. The
user hand-reviews this to judge whether cross-layer / on-stack /
off-stack labels actually capture architectural reasoning.

Run from project root:

    python -m analysis.audit_cross_layer --out /tmp/cross_layer_audit.md
    python -m analysis.audit_cross_layer --per-stratum 40 --seed 7
"""

import argparse
import json
import random
from pathlib import Path

from .loader import iter_bugs, BugEntry
from .filters import parse_stack_trace
from .analyzers.cross_layer import compute_cross_layer
from .analyzers.kernel_layers import classify_file_layer


RESULT_PATH = (
    Path(__file__).resolve().parent / "results"
    / "cross-layer_analysis" / "result.json"
)


def _load_details() -> dict[str, dict]:
    data = json.loads(RESULT_PATH.read_text())
    return {d["bug_id"]: d for d in data.get("details", []) if "bug_id" in d}


def _stratum_of(detail: dict) -> str | None:
    """Bucket a saved detail into one of the four audit strata."""
    if detail.get("is_cross_layer"):
        return (
            "cross_layer_on_stack"
            if detail.get("stack_overlap") == "fix_on_stack"
            else "cross_layer_off_stack"
        )
    # Same-layer details are minimal — we can't distinguish
    # "same_layer" vs "no_shared_domain" without recomputing.
    # Fall back to "same_or_cross_subsystem" and let the render step
    # recompute the real reason.
    return "same_or_cross_subsystem"


def _pick_first_patch_diff(bug: BugEntry) -> str:
    for fc in bug.fix_commits:
        if fc.patch_diff:
            return fc.patch_diff
    return ""


def _render_bug(bug: BugEntry, saved: dict) -> str:
    """Render one bug's audit block as Markdown."""
    lines: list[str] = []
    lines.append(f"### `{bug.bug_id}` — {bug.title}")
    lines.append("")

    patch_diff = _pick_first_patch_diff(bug)
    live = compute_cross_layer(bug.crash_report, patch_diff)

    # Saved label (from result.json)
    lines.append("**Saved label:**")
    lines.append("")
    if saved.get("is_cross_layer"):
        lines.append(
            f"- cross-layer = **True** · domain = `{saved.get('domain', '')}`"
            f" · direction = `{saved.get('direction', '')}`"
        )
        lines.append(
            f"- crash_layer = `{saved.get('crash_layer', '')}`"
            f" · fix_layer = `{saved.get('fix_layer', '')}`"
        )
        lines.append(
            f"- stack_overlap = `{saved.get('stack_overlap', '')}`"
        )
    else:
        # Recompute to get the reason (same_layer vs no_shared_domain)
        reason = "?"
        if live is not None and not live.get("is_cross_layer"):
            reason = live.get("reason", "?")
        lines.append(f"- cross-layer = **False** · reason = `{reason}`")
    lines.append("")

    # Crash top frames
    frames = parse_stack_trace(bug.crash_report)
    lines.append("**Crash top frames (≤10):**")
    lines.append("")
    lines.append("```")
    for i, fr in enumerate(frames[:10]):
        label = ""
        if fr.file:
            cls = classify_file_layer(fr.file)
            if cls:
                label = f"  [{cls[0]}: {cls[1]} (L{cls[2]})]"
            else:
                label = "  [unclassified]"
        tag = " [inline]" if fr.is_inline else ""
        lines.append(f"  #{i:<2} {fr.function}{tag}")
        lines.append(f"      {fr.file}:{fr.line}{label}")
    lines.append("```")
    lines.append("")

    # Fix files
    fix_files: list[str] = []
    if live is not None:
        fix_files = live.get("fix_files", [])
    lines.append("**Fix files:**")
    lines.append("")
    if not fix_files:
        lines.append("- (none — patch had no classifiable source files)")
    else:
        lines.append("```")
        for f in fix_files:
            cls = classify_file_layer(f)
            label = (
                f"  [{cls[0]}: {cls[1]} (L{cls[2]})]"
                if cls else "  [unclassified]"
            )
            lines.append(f"  {f}{label}")
        lines.append("```")
    lines.append("")

    # Crash file set vs fix file set (sanity check stack_overlap)
    crash_files = {f.file for f in frames if f.file}
    if fix_files:
        overlap = [f for f in fix_files if f in crash_files]
        lines.append(
            f"**Stack overlap check:** "
            f"{len(overlap)}/{len(fix_files)} fix files appear somewhere "
            f"in the crash stack frames."
        )
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out",
        default="analysis/results/cross-layer_analysis/audit.md",
        help="Markdown output path",
    )
    ap.add_argument(
        "--per-stratum",
        type=int,
        default=40,
        help="Samples per stratum (default 40)",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed (default 42)",
    )
    args = ap.parse_args()

    rng = random.Random(args.seed)
    saved = _load_details()

    # Group bug_ids by stratum
    strata: dict[str, list[str]] = {
        "cross_layer_on_stack": [],
        "cross_layer_off_stack": [],
        "same_or_cross_subsystem": [],
    }
    for bug_id, detail in saved.items():
        s = _stratum_of(detail)
        if s is not None:
            strata[s].append(bug_id)

    # Sample
    picked: dict[str, set[str]] = {}
    for s, ids in strata.items():
        rng.shuffle(ids)
        picked[s] = set(ids[: args.per_stratum])

    wanted_ids: set[str] = set().union(*picked.values())
    print(f"[audit] sampling {len(wanted_ids)} bugs across {len(strata)} strata")

    # Second pass: we need the bug records, which means streaming the
    # processed JSONs. Keep a map bug_id → BugEntry as we find them.
    collected: dict[str, BugEntry] = {}
    for bug in iter_bugs():
        if bug.bug_id in wanted_ids:
            collected[bug.bug_id] = bug
            if len(collected) == len(wanted_ids):
                break
    missing = wanted_ids - set(collected.keys())
    if missing:
        print(f"[audit] warning: {len(missing)} sampled bugs not found in processed data")

    # Render
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    parts: list[str] = [
        "# Cross-Layer Analyzer Audit",
        "",
        f"Stratified random sample (seed={args.seed}, per_stratum={args.per_stratum}).",
        "",
        "Hand-review each entry and note any disagreements between the",
        "saved label and your judgment in a separate notes file.",
        "",
    ]
    for stratum_label in (
        "cross_layer_off_stack",
        "cross_layer_on_stack",
        "same_or_cross_subsystem",
    ):
        parts.append(f"## Stratum: `{stratum_label}`")
        parts.append("")
        parts.append(
            f"_{len(picked[stratum_label])} samples from "
            f"{len(strata[stratum_label])} total_"
        )
        parts.append("")
        for bug_id in sorted(picked[stratum_label]):
            bug = collected.get(bug_id)
            if bug is None:
                parts.append(f"### `{bug_id}` — (not found in processed data)")
                parts.append("")
                continue
            parts.append(_render_bug(bug, saved[bug_id]))
            parts.append("---")
            parts.append("")

    out_path.write_text("\n".join(parts))
    print(f"[audit] wrote {out_path} ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

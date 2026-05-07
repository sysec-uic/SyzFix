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
import sys
from collections import Counter
from pathlib import Path

from .loader import iter_bugs, BugEntry
from .filters import parse_stack_trace
from .analyzers.cross_layer import (
    compute_cross_layer, classify_under_mode,
)
from .analyzers.kernel_layers import (
    classify_file_layer, is_infrastructure_file,
)


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


def _find_bug(bug_id: str) -> BugEntry | None:
    """Stream processed bugs until the requested bug_id is found."""
    for bug in iter_bugs():
        if bug.bug_id == bug_id:
            return bug
    return None


def _audit_single_bug(bug_id: str) -> int:
    """Print the analyzer's intermediate state for one bug. Returns exit code.

    The output mirrors the decision points inside `compute_cross_layer`:
    how every crash frame is classified, how `primary_crash` is picked
    (showing skipped inline frames), how `primary_fix` is picked, and
    how `classify_under_mode` labels the bug under each canonical mode.
    """
    bug = _find_bug(bug_id)
    if bug is None:
        print(f"[audit] bug {bug_id} not found in processed data",
              file=sys.stderr)
        return 1

    patch_diff = _pick_first_patch_diff(bug)
    record = compute_cross_layer(bug.crash_report, patch_diff)
    if record is None:
        print(f"[audit] {bug_id} could not be classified "
              "(missing crash report or patch diff)",
              file=sys.stderr)
        return 2

    print(f"=== bug {bug_id} ===")
    print(f"Title: {bug.title or '(no title)'}")
    print()
    print(f"Saved relation: {record.get('relation', '?')}")
    if record.get("stack_overlap"):
        print(f"  stack_overlap: {record['stack_overlap']}")
    if record.get("direction"):
        print(f"  direction: {record['direction']}")

    fix_internal = record.get("fix_internal_layers") or []
    if fix_internal:
        print("  fix_internal_layers:")
        for il in fix_internal:
            print(
                f"    {il['domain']:12s} L{il['layer_level']} "
                f"{il['layer_name']:25s} "
                f"({il['lines_changed']} lines, {len(il['files'])} files)"
            )
    print()

    # Crash frame breakdown
    frames = parse_stack_trace(bug.crash_report)
    crash_frames = []
    for idx, fr in enumerate(frames):
        cls = classify_file_layer(fr.file) if fr.file else None
        crash_frames.append((idx, fr, cls))
    classified = [(i, fr, cls) for i, fr, cls in crash_frames if cls]

    print("Crash domain breakdown:")
    counts: Counter = Counter()
    for _, _, cls in classified:
        counts[(cls[0], cls[1], cls[2])] += 1
    for (dom, ln, lv), n in sorted(
        counts.items(), key=lambda kv: (kv[0][0], kv[0][2])
    ):
        print(f"  {dom:12s} L{lv} {ln:25s} {n} frame(s)")
    if not classified:
        print("  (no classifiable frames)")
    print()

    # Per-shared-domain decision trace
    shared_domains = sorted(
        set(record.get("shared_domains") or []) or
        ([record["domain"]] if record.get("relation") == "cross_layer"
                            and record.get("domain") else [])
    )
    if not shared_domains:
        if record.get("relation") == "cross_domain":
            print(
                f"Shared domains: ∅  →  cross_domain "
                f"(crash domains "
                f"{sorted(record.get('crash_domains') or [])} → "
                f"fix domain {record.get('fix_domain', '')})"
            )
        else:
            print("Shared domains: (none classified)")
    else:
        print(f"Shared domains: {shared_domains}")
    print()

    for dom in shared_domains:
        print(f"Per-domain trace · {dom}:")
        # Replay primary_crash selection
        in_domain = [
            (i, fr) for i, fr, cls in classified if cls[0] == dom
        ]
        print(f"  primary_crash selection "
              f"(top 5 frames in {dom}, prefer non-inline non-infra):")
        # Replay matches compute_cross_layer: pass-1 picks the first
        # non-inline non-infra frame; pass-2 falls back to first non-inline;
        # pass-3 falls back to the first frame regardless.
        top5 = in_domain[:5]
        chosen_idx = None
        for k, (i, fr) in enumerate(top5):
            if not fr.is_inline and not is_infrastructure_file(fr.file):
                chosen_idx = k
                break
        if chosen_idx is None:
            for k, (i, fr) in enumerate(top5):
                if not fr.is_inline:
                    chosen_idx = k
                    break
        if chosen_idx is None and top5:
            chosen_idx = 0
        for k, (i, fr) in enumerate(top5):
            cls = classify_file_layer(fr.file)
            tags = []
            tags.append("inline" if fr.is_inline else "non-inline")
            if is_infrastructure_file(fr.file):
                tags.append("infra")
            mark = "  ★ chosen" if k == chosen_idx else ""
            print(
                f"    [#{i}] {fr.function or '?':35s} "
                f"{fr.file}:{fr.line}  "
                f"L{cls[2]} {cls[1]}  ({', '.join(tags)}){mark}"
            )
        if chosen_idx is None and in_domain:
            i, fr = in_domain[0]
            cls = classify_file_layer(fr.file)
            print(
                f"    (no fallback frames in top-5) → "
                f"#{i} {fr.function} L{cls[2]} {cls[1]}  ★ chosen"
            )

        # primary_fix per domain
        print("  primary_fix selection (lines-weighted in domain):")
        in_dom_fix = [
            il for il in fix_internal if il["domain"] == dom
        ]
        if not in_dom_fix:
            print(f"    (no fix files in {dom})")
        else:
            for il in sorted(
                in_dom_fix, key=lambda x: -x["lines_changed"]
            ):
                marker = ""
                if il is max(in_dom_fix, key=lambda x: x["lines_changed"]):
                    marker = "  ★ chosen"
                print(
                    f"    L{il['layer_level']} {il['layer_name']:25s} "
                    f"{il['lines_changed']} lines  "
                    f"files={il['files']}{marker}"
                )

        # Verdict for this domain
        if record.get("relation") == "cross_layer" and \
                record.get("domain") == dom:
            print(
                f"  →  crash {record.get('crash_layer', '?')} "
                f"≠ fix {record.get('fix_layer', '?')}  ⇒  cross_layer "
                f"({record.get('direction', '?')})"
            )
        else:
            print(f"  →  same primary layer in {dom}  ⇒  same_layer")
        print()

    # Mode grid
    mode_grid = [
        ("combined", 1),
        ("layer", 1),
        ("layer", 2),
        ("layer", "all"),
        ("stack", 1),
        ("off", 1),
    ]
    print("Mode-aware verdicts:")
    for strict, window in mode_grid:
        v = classify_under_mode(record, strict=strict, relax_window=window)
        sign = "POSITIVE" if v["label"] else "negative"
        wstr = str(window).rjust(3)
        print(
            f"  strict={strict:8s} relax={wstr}   "
            f"{sign:8s}  reason={v['reason']}"
        )

    return 0


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
    ap.add_argument(
        "--bug-id",
        default=None,
        help=(
            "Audit a single bug: print classifier intermediate state "
            "(crash domain breakdown, primary_crash/primary_fix selection, "
            "and verdicts under the canonical mode grid). When set, the "
            "stratified Markdown sampler is skipped."
        ),
    )
    args = ap.parse_args()

    if args.bug_id:
        sys.exit(_audit_single_bug(args.bug_id))

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

#!/usr/bin/env python3
"""
Generate paper-ready case study narratives for selected bugs.

Usage:
    python -m analysis.generate_case_study <bug_id_1> [bug_id_2] ...
    python -m analysis.generate_case_study --from-results [--top N]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.loader import load_all_bugs, BugEntry
from analysis.filters import (
    get_human_reviews, get_review_text, is_stable_backport_thread,
    parse_stack_trace,
)
from analysis.analyzers.patch_diff_analysis import (
    parse_diff_stats, extract_diff_from_patch_message,
)
from analysis.analyzers.difficulty_stratification import (
    _patch_size, _num_files, _time_to_fix_days, _fix_locality_score,
)

LOCALITY_NAMES = {0: "same-function", 1: "same-file", 2: "same-directory", 3: "different-subsystem"}


def generate_narrative(bug: BugEntry) -> str:
    """Generate a paper-ready markdown narrative for a single bug."""
    lines = []

    # ── 1. Overview ─────────────────────────────────────────────────────
    lines.append(f"## {bug.title}")
    lines.append(f"**Bug ID**: `{bug.bug_id}`")
    lines.append(f"**Subsystem**: {bug.subsystem_guess}")

    fix_days = _time_to_fix_days(bug)
    if fix_days is not None:
        lines.append(f"**Time to fix**: {fix_days:.1f} days")

    lines.append(f"**Patch iterations**: {bug.num_patch_versions}")

    locality = _fix_locality_score(bug)
    lines.append(f"**Fix locality**: {LOCALITY_NAMES.get(locality, 'unknown')}")

    # Fix commit info
    for fc in bug.fix_commits:
        if fc.hash:
            lines.append(f"**Fix commit**: `{fc.hash[:12]}` — {fc.title}")
            break

    lines.append("")

    # ── 2. Patch complexity ─────────────────────────────────────────────
    final_lines = _patch_size(bug)
    final_files = _num_files(bug)
    lines.append("### Patch Complexity")
    lines.append(f"- **Final merged patch**: {final_lines or '?'} lines changed across {final_files or '?'} file(s)")

    pvs = bug.patch_versions
    for pv in pvs:
        diff = extract_diff_from_patch_message(pv.messages)
        if diff:
            stats = parse_diff_stats(diff)
            lines.append(
                f"- **v{pv.patch_version}**: {stats['total_lines']} lines, "
                f"{len(stats['files'])} file(s) — {pv.subject[:80]}"
            )
        else:
            lines.append(f"- **v{pv.patch_version}**: (diff not embedded) — {pv.subject[:80]}")

    lines.append("")

    # ── 3. Crash summary ────────────────────────────────────────────────
    crash = bug.crash_report
    if crash:
        lines.append("### Crash Summary")
        lines.append("```")
        crash_lines = crash.split('\n')
        for cl in crash_lines[:20]:
            lines.append(cl)
        if len(crash_lines) > 20:
            lines.append(f"... ({len(crash_lines) - 20} more lines)")
        lines.append("```")
        lines.append("")

    # ── 4. Patch version timeline ───────────────────────────────────────
    if pvs:
        lines.append("### Patch Version Timeline")
        for pv in pvs:
            # Find the submission date
            date = "?"
            for msg in pv.messages:
                if msg.subject and not msg.subject.startswith("Re:"):
                    date = msg.date[:10] if msg.date else "?"
                    break

            diff = extract_diff_from_patch_message(pv.messages)
            if diff:
                stats = parse_diff_stats(diff)
                lines.append(
                    f"- **v{pv.patch_version}** ({date}): "
                    f"{stats['total_lines']} lines, {len(stats['files'])} files, "
                    f"{stats['hunks']} hunks"
                )
            else:
                lines.append(f"- **v{pv.patch_version}** ({date}): diff not embedded in message")
        lines.append("")

    # ── 5. Review highlights ────────────────────────────────────────────
    lines.append("### Review Highlights")
    has_reviews = False
    for disc in bug.discussions:
        if is_stable_backport_thread(disc):
            continue
        reviews = get_human_reviews(disc.messages)
        if not reviews:
            continue
        version_label = f"v{disc.patch_version}" if disc.patch_version else "general"
        for rev in reviews[:3]:
            has_reviews = True
            reviewer = rev.sender_name
            text = get_review_text(rev).replace('\n', ' ')[:200]
            lines.append(f"- **[{version_label}] {reviewer}**: {text}")

    if not has_reviews:
        lines.append("- (no substantive human reviews found)")
    lines.append("")

    # ── 6. Final fix ────────────────────────────────────────────────────
    lines.append("### Final Fix")
    for fc in bug.fix_commits:
        if fc.patch_diff:
            lines.append(f"**Commit**: {fc.title}")
            diff_lines = fc.patch_diff.split('\n')
            show_lines = diff_lines if len(diff_lines) <= 60 else diff_lines[:30]
            lines.append("```diff")
            for dl in show_lines:
                lines.append(dl)
            if len(diff_lines) > 60:
                lines.append(f"... ({len(diff_lines) - 30} more lines)")
            lines.append("```")
            break

    lines.append("")
    lines.append("---")
    lines.append("")

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate paper-ready case study narratives for selected bugs.",
    )
    parser.add_argument(
        "bug_ids", nargs="*",
        help="Bug IDs to generate narratives for",
    )
    parser.add_argument(
        "--from-results", action="store_true",
        help="Load top candidates from casestudy analyzer results",
    )
    parser.add_argument(
        "--top", type=int, default=4,
        help="Number of top candidates to show (with --from-results)",
    )
    parser.add_argument(
        "--paper-friendly", action="store_true",
        help="Only show paper-friendly candidates (final patch ≤50 lines)",
    )

    args = parser.parse_args()

    if args.from_results:
        results_path = Path(__file__).resolve().parent / "results" / "case_study_finder" / "result.json"
        if not results_path.exists():
            print(f"No saved results at {results_path}")
            print("Run: python -m analysis.run_all --analyzer casestudy")
            return

        with open(results_path) as f:
            data = json.load(f)

        details = data.get("details", [])
        if args.paper_friendly:
            details = [d for d in details if d.get("paper_friendly")]

        bug_ids = [d["bug_id"] for d in details[:args.top]]
        print(f"Loading top {len(bug_ids)} candidates from saved results...")
    elif args.bug_ids:
        bug_ids = args.bug_ids
    else:
        parser.print_help()
        return

    # Load all bugs and index by ID
    print("Loading dataset...")
    all_bugs = load_all_bugs()
    bug_index = {b.bug_id: b for b in all_bugs}

    found = 0
    for bid in bug_ids:
        # Support partial ID matching
        bug = bug_index.get(bid)
        if bug is None:
            matches = [b for b in all_bugs if b.bug_id.startswith(bid)]
            if len(matches) == 1:
                bug = matches[0]
            elif len(matches) > 1:
                print(f"\nAmbiguous ID '{bid}', matches: {[m.bug_id for m in matches[:5]]}")
                continue
            else:
                print(f"\nBug '{bid}' not found")
                continue

        found += 1
        narrative = generate_narrative(bug)
        print(narrative)

    if found == 0:
        print("No bugs found. Check your bug IDs.")


if __name__ == "__main__":
    main()

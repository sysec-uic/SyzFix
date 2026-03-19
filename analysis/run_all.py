#!/usr/bin/env python3
"""
SyzFix Dataset Analysis — CLI Entry Point

Usage:
    python -m analysis.run_all                          # Run all analyzers
    python -m analysis.run_all --analyzer revision      # Run one analyzer
    python -m analysis.run_all --sample 500             # Random sample
    python -m analysis.run_all --output-dir ./results   # Custom output
    python -m analysis.run_all --list                   # List available analyzers
"""

import argparse
import csv
import json
import os
import random
import sys
import time
from pathlib import Path

# Add parent dir to path so we can import the analysis package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.loader import load_all_bugs, bugs_with_evolution, bugs_with_discussion
from analysis.analyzers.base import AnalysisResult
from analysis.analyzers.revision_reasons import RevisionReasonsAnalyzer
from analysis.analyzers.discussion_lessons import DiscussionLessonsAnalyzer
from analysis.analyzers.non_functional import NonFunctionalAnalyzer
from analysis.analyzers.patch_diff_analysis import PatchDiffAnalyzer
from analysis.analyzers.bug_type_classifier import BugTypeClassifier
from analysis.analyzers.fix_patterns import FixPatternAnalyzer
from analysis.analyzers.fix_locality import FixLocalityAnalyzer
from analysis.analyzers.difficulty_stratification import DifficultyStratificationAnalyzer
from analysis.analyzers.information_sufficiency import InformationSufficiencyAnalyzer
from analysis.analyzers.case_study_finder import CaseStudyFinder
from analysis.analyzers.insight_clusters import InsightClusterAnalyzer
from analysis.analyzers.patch_evolution import PatchEvolutionAnalyzer
from analysis.analyzers.backport_downstream import BackportDownstreamAnalyzer

# ─── Registry of available analyzers ────────────────────────────────────────

ANALYZERS = {
    "revision": RevisionReasonsAnalyzer(),
    "discussion": DiscussionLessonsAnalyzer(),
    "nonfunctional": NonFunctionalAnalyzer(),
    "patchdiff": PatchDiffAnalyzer(),
    "bugtype": BugTypeClassifier(),
    "fixpattern": FixPatternAnalyzer(),
    "locality": FixLocalityAnalyzer(),
    "difficulty": DifficultyStratificationAnalyzer(),
    "infosuff": InformationSufficiencyAnalyzer(),
    "casestudy": CaseStudyFinder(),
    "insights": InsightClusterAnalyzer(),
    "evolution": PatchEvolutionAnalyzer(),
    "backport": BackportDownstreamAnalyzer(),
}


def print_table(title: str, rows: list[dict], max_rows: int = 30):
    """Pretty-print a table of dicts."""
    if not rows:
        print(f"\n  {title}: (no data)")
        return
    rows = rows[:max_rows]
    cols = list(rows[0].keys())
    widths = {c: max(len(str(c)), max(len(str(r.get(c, ""))) for r in rows)) for c in cols}

    print(f"\n  {title}:")
    header = "  " + " | ".join(str(c).ljust(widths[c]) for c in cols)
    print(header)
    print("  " + "-+-".join("-" * widths[c] for c in cols))
    for row in rows:
        line = "  " + " | ".join(str(row.get(c, "")).ljust(widths[c]) for c in cols)
        print(line)


def print_examples(title: str, examples: dict, max_per_cat: int = 2):
    """Print example bug snippets per category."""
    print(f"\n  {title}:")
    for cat, exs in examples.items():
        print(f"\n    [{cat}]")
        for ex in exs[:max_per_cat]:
            print(f"      Bug: {ex.get('bug_id', 'N/A')} — {ex.get('title', 'N/A')}")
            for snippet in ex.get("snippets", []):
                reviewer = snippet.get("from", snippet.get("reviewer", "?"))
                text = snippet.get("snippet", "")
                # Show first 200 chars
                text = text.replace('\n', ' ')[:200]
                print(f"        [{reviewer}]: {text}...")


def save_results(result: AnalysisResult, output_dir: Path):
    """Save analysis results as JSON and CSV files."""
    analyzer_dir = output_dir / result.name.lower().replace(" ", "_")
    analyzer_dir.mkdir(parents=True, exist_ok=True)

    # Save full result as JSON
    with open(analyzer_dir / "result.json", 'w') as f:
        json.dump({
            "name": result.name,
            "summary": result.summary,
            "details": result.details[:100],  # cap details for JSON size
            "tables": {
                k: v for k, v in result.tables.items()
                if isinstance(v, list)  # only save list tables as JSON
            },
        }, f, indent=2, default=str)

    # Save each list-of-dict table as CSV
    for table_name, rows in result.tables.items():
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            csv_path = analyzer_dir / f"{table_name}.csv"
            with open(csv_path, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)

    print(f"  → Saved to {analyzer_dir}/")


def run_analysis(analyzer_names: list[str], bugs, output_dir: Path):
    """Run specified analyzers and print/save results."""
    for name in analyzer_names:
        analyzer = ANALYZERS[name]
        print(f"\nRunning: {analyzer.name}...")
        t0 = time.time()
        result = analyzer.analyze(bugs)
        elapsed = time.time() - t0
        print(f"  (completed in {elapsed:.1f}s)")

        # Print summary
        result.print_summary()

        # Print tables
        for table_name, rows in result.tables.items():
            if isinstance(rows, list):
                print_table(table_name, rows)
            elif isinstance(rows, dict):
                # This is the examples dict
                print_examples(table_name, rows)

        # Print top details if present
        if result.details:
            print(f"\n  Top entries ({len(result.details)} total):")
            for d in result.details[:5]:
                if isinstance(d, dict):
                    bug_id = d.get("bug_id", "?")
                    title = d.get("title", "?")
                    cats = d.get("categories", d.get("nonfunc_categories", []))
                    delta = d.get("delta", "")
                    extra = f" (delta={delta})" if delta != "" else ""
                    print(f"    {bug_id}: {title}")
                    if cats:
                        print(f"      Categories: {', '.join(cats)}")
                    if extra:
                        print(f"      {extra}")

        # Save results
        save_results(result, output_dir)

    print(f"\n{'=' * 70}")
    print(f"  All results saved to: {output_dir}/")
    print(f"{'=' * 70}")


def show_saved_results(analyzer_names: list[str], output_dir: Path):
    """Pretty-print already-saved results from result.json files."""
    for name in analyzer_names:
        # Derive the directory name the same way save_results() does:
        #   analyzer_dir = output_dir / result.name.lower().replace(" ", "_")
        analyzer_full_name = ANALYZERS[name].name
        analyzer_dir = output_dir / analyzer_full_name.lower().replace(" ", "_")
        match = analyzer_dir / "result.json" if (analyzer_dir / "result.json").exists() else None

        if match is None:
            print(f"\n[{name}] No saved results found in {output_dir}")
            print(f"  Run: python -m analysis.run_all --analyzer {name}")
            continue

        with open(match) as f:
            data = json.load(f)

        print(f"\n{'=' * 70}")
        print(f"  {data['name']}")
        print(f"{'=' * 70}")

        # Summary
        summary = data.get("summary", {})
        if summary:
            print("\n  Summary:")
            for k, v in summary.items():
                print(f"    {k}: {v}")

        # Tables
        for table_name, rows in data.get("tables", {}).items():
            if isinstance(rows, list):
                print_table(table_name, rows)
            elif isinstance(rows, dict):
                print_examples(table_name, rows)

        # CSV files alongside result.json (may have more rows than the capped JSON)
        csv_files = sorted(match.parent.glob("*.csv"))
        if csv_files:
            print(f"\n  Full CSV exports:")
            for csv_path in csv_files:
                print(f"    {csv_path}")

        print()


def main():
    parser = argparse.ArgumentParser(
        description="SyzFix Dataset Analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m analysis.run_all                          # Run all analyzers
  python -m analysis.run_all --analyzer revision      # Only revision reasons
  python -m analysis.run_all --sample 500             # Quick test on 500 bugs
  python -m analysis.run_all --list                   # List available analyzers
  python -m analysis.run_all --show                   # Print saved results
  python -m analysis.run_all --show --analyzer revision  # Print one saved result
        """,
    )
    parser.add_argument(
        "--analyzer", "-a",
        choices=list(ANALYZERS.keys()),
        help="Run a specific analyzer (default: all)",
    )
    parser.add_argument(
        "--sample", "-s",
        type=int, default=0,
        help="Run on a random sample of N bugs (for fast iteration)",
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=str, default=None,
        help="Output directory for results (default: analysis/results/)",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List available analyzers and exit",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Print previously saved results without re-running analysis",
    )

    args = parser.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else Path(__file__).resolve().parent / "results"
    analyzer_names = [args.analyzer] if args.analyzer else list(ANALYZERS.keys())

    if args.list:
        print("Available analyzers:")
        for name, analyzer in ANALYZERS.items():
            result_file = output_dir / analyzer.name.lower().replace(" ", "_") / "result.json"
            status = "✓" if result_file.exists() else " "
            print(f"  [{status}] {name:20s} — {analyzer.name}")
        return

    if args.show:
        show_saved_results(analyzer_names, output_dir)
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    # Load data
    print("Loading dataset...")
    t0 = time.time()
    bugs = load_all_bugs()
    elapsed = time.time() - t0
    print(f"  Loaded {len(bugs)} bugs in {elapsed:.1f}s")

    if args.sample:
        bugs = random.sample(bugs, min(args.sample, len(bugs)))
        print(f"  Sampled {len(bugs)} bugs for analysis")

    multi = bugs_with_evolution(bugs)
    disc = bugs_with_discussion(bugs)
    print(f"  Bugs with multiple patch versions: {len(multi)}")
    print(f"  Bugs with discussion: {len(disc)}")

    # Run analyzers
    run_analysis(analyzer_names, bugs, output_dir)


if __name__ == "__main__":
    main()

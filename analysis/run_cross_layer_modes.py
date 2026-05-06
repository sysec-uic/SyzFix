#!/usr/bin/env python3
"""Cross-layer classification under selectable strict and relax modes.

Reads `analysis/results/cross-layer_analysis/result.json` (or recomputes
if --recompute) and re-classifies every bug under a user-selected
(strict, relax_window) pair. Optional domain and direction filters slice
the dataset by the existing per-record fields.

Examples:

    # Default — combined-strict, relax-window 1 (≡ historic is_cross_layer ∩ fix_off_stack
    # plus all cross-domain bugs that are also off-stack)
    python -m analysis.run_cross_layer_modes

    # Layer-only, top-2 frame window, fix in upper layer, filesystem only
    python -m analysis.run_cross_layer_modes \\
        --strict layer --relax-window 2 \\
        --direction fix_in_upper_layer --domain filesystem

    # Side-by-side comparison grid over the full dataset
    python -m analysis.run_cross_layer_modes --compare
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.analyzers.cross_layer import classify_under_mode

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULT = (
    PROJECT_ROOT / "analysis" / "results" / "cross-layer_analysis" / "result.json"
)
DEFAULT_BY_MODE_DIR = (
    PROJECT_ROOT / "analysis" / "results" / "cross-layer_analysis" / "by_mode"
)

STRICT_CHOICES = ("stack", "layer", "combined", "off")
DIRECTION_CHOICES = ("fix_in_upper_layer", "fix_in_lower_layer", "any")


def _parse_relax_window(s: str) -> int | str:
    if s == "all":
        return "all"
    n = int(s)
    if n < 1:
        raise argparse.ArgumentTypeError("--relax-window must be >=1 or 'all'")
    return n


def _record_passes_filters(
    record: dict, *, direction: str, domain: str | None
) -> bool:
    if direction != "any":
        if record.get("direction") != direction:
            return False
    if domain and domain != "any":
        rel = record.get("relation")
        if rel == "cross_layer":
            if record.get("domain") != domain:
                return False
        elif rel == "cross_domain":
            # accept if either the crash or fix domain matches
            if (
                record.get("crash_domain") != domain
                and record.get("fix_domain") != domain
            ):
                return False
        else:  # same_layer
            if domain not in (record.get("shared_domains") or []):
                return False
    return True


def _summarize(
    details: list[dict], *, strict: str, relax_window, direction: str, domain: str
) -> dict:
    """Compute summary stats and labelled subset for a (strict, window) pair."""
    total = 0
    positive = 0
    by_domain = Counter()
    by_relation = Counter()
    by_direction = Counter()
    positives: list[dict] = []
    for r in details:
        if not _record_passes_filters(r, direction=direction, domain=domain):
            continue
        total += 1
        out = classify_under_mode(r, strict=strict, relax_window=relax_window)
        if out["label"]:
            positive += 1
            by_relation[r.get("relation", "")] += 1
            for d in (
                r.get("domain"),
                r.get("crash_domain"),
                r.get("fix_domain"),
            ):
                if d:
                    by_domain[d] += 1
                    break
            if r.get("direction"):
                by_direction[r["direction"]] += 1
            positives.append(
                {
                    "bug_id": r.get("bug_id"),
                    "title": r.get("title"),
                    "relation": r.get("relation"),
                    "domain": r.get("domain") or r.get("fix_domain"),
                    "crash_layer": r.get("crash_layer"),
                    "fix_layer": r.get("fix_layer"),
                    "direction": r.get("direction"),
                    "stack_overlap": r.get("stack_overlap"),
                    "fix_files": r.get("fix_off_stack_files", [])
                    + r.get("fix_on_stack_files", []),
                    "reason": out["reason"],
                }
            )
    return {
        "mode": f"strict={strict};relax={relax_window}",
        "filters": {"direction": direction, "domain": domain},
        "total_considered": total,
        "positive": positive,
        "prevalence": (positive / total) if total else 0.0,
        "by_relation": dict(by_relation),
        "by_domain": dict(by_domain.most_common()),
        "by_direction": dict(by_direction.most_common()),
        "positives": positives,
    }


def _print_summary(summary: dict, *, top_examples: int = 10) -> None:
    print()
    print(f"Mode    : {summary['mode']}")
    print(f"Filters : {summary['filters']}")
    print(
        f"Result  : {summary['positive']}/{summary['total_considered']} "
        f"= {summary['prevalence']:.1%} cross-layer"
    )
    if summary["by_relation"]:
        print("By relation:")
        for k, v in summary["by_relation"].items():
            print(f"  {k:14s} {v:5d}")
    if summary["by_domain"]:
        print("By domain:")
        for k, v in summary["by_domain"].items():
            print(f"  {k:14s} {v:5d}")
    if summary["by_direction"]:
        print("By direction:")
        for k, v in summary["by_direction"].items():
            print(f"  {k:24s} {v:5d}")
    if summary["positives"][:top_examples]:
        print(f"Top {min(top_examples, len(summary['positives']))} examples:")
        for ex in summary["positives"][:top_examples]:
            line = (
                f"  {ex['bug_id'][:20]}  {ex.get('relation','')[:14]:14s}  "
                f"{(ex.get('domain') or ''):14s}  {ex.get('title','')[:60]}"
            )
            print(line)


def _print_compare(details: list[dict], *, direction: str, domain: str) -> None:
    """Side-by-side count table over (strict × relax_window) grid."""
    strict_keys = ("stack", "layer", "combined")
    windows = (1, 2, 3, "all")
    print()
    print("Cross-layer prevalence under each mode (positive count):")
    header = f"{'relax_window':<14s}" + "".join(
        f"{'strict='+s:>16s}" for s in strict_keys
    )
    print(header)
    print("-" * len(header))
    for w in windows:
        row = f"{str(w):<14s}"
        for s in strict_keys:
            n = sum(
                1
                for r in details
                if _record_passes_filters(r, direction=direction, domain=domain)
                and classify_under_mode(r, strict=s, relax_window=w)["label"]
            )
            row += f"{n:>16d}"
        print(row)
    n_considered = sum(
        1
        for r in details
        if _record_passes_filters(r, direction=direction, domain=domain)
    )
    print(f"\nTotal considered: {n_considered}")
    print(f"Filters: direction={direction}, domain={domain}")


def _load_details(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(
            f"[run_cross_layer_modes] Missing {path}. "
            f"Run `python -m analysis.run_all --analyzer crosslayer` first."
        )
    return json.loads(path.read_text()).get("details", [])


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Cross-layer classification under selectable strict/relax modes",
    )
    ap.add_argument(
        "--strict", choices=STRICT_CHOICES, default="combined",
        help="Strict-mode component (default: combined)",
    )
    ap.add_argument(
        "--relax-window", default="1", type=_parse_relax_window,
        help="Top-N non-inline frames in the fix domain to compare against. "
             "Integer N>=1 or 'all'. Default 1.",
    )
    ap.add_argument(
        "--direction", choices=DIRECTION_CHOICES, default="any",
        help="Filter by cross-layer direction (default: any)",
    )
    ap.add_argument(
        "--domain", default="any",
        help="Filter by domain name (filesystem, networking, block, device, "
             "graphics, sound, virt, mm, kernel, bpf, crypto, security, arch, "
             "or 'any'). Default 'any'.",
    )
    ap.add_argument(
        "--input", default=str(DEFAULT_RESULT), type=Path,
        help="Path to cross-layer analyzer result.json",
    )
    ap.add_argument(
        "--compare", action="store_true",
        help="Print side-by-side comparison grid over (strict × relax_window)",
    )
    ap.add_argument(
        "--save", action="store_true",
        help=f"Persist per-mode classification to {DEFAULT_BY_MODE_DIR}",
    )
    ap.add_argument(
        "--top-examples", type=int, default=10,
        help="Number of example positives to print (default: 10)",
    )
    args = ap.parse_args()

    details = _load_details(args.input)

    if args.compare:
        _print_compare(details, direction=args.direction, domain=args.domain)
        return

    summary = _summarize(
        details,
        strict=args.strict,
        relax_window=args.relax_window,
        direction=args.direction,
        domain=args.domain,
    )
    _print_summary(summary, top_examples=args.top_examples)

    if args.save:
        DEFAULT_BY_MODE_DIR.mkdir(parents=True, exist_ok=True)
        slug = (
            f"strict={args.strict}__relax={args.relax_window}"
            f"__dir={args.direction}__dom={args.domain}"
        )
        out = DEFAULT_BY_MODE_DIR / f"{slug}.json"
        out.write_text(json.dumps(summary, indent=2, default=str))
        print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()

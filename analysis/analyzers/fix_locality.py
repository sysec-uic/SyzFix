"""
Analyzer: Fix locality — distance from crash site to fix site.

Compares where the bug manifests (from crash report stack trace) to where
the fix is applied (from patch diff file paths and hunk headers).
"""

import os
import re
from collections import Counter, defaultdict
from typing import Optional

from ..loader import BugEntry
from ..filters import parse_stack_trace, StackFrame
from .base import BaseAnalyzer, AnalysisResult


# Locality levels from most local to most distant
LOCALITY_LEVELS = [
    "same-function",
    "same-file",
    "same-directory",
    "same-subsystem",
    "different-subsystem",
]


def _extract_fix_files(patch_diff: str) -> list[str]:
    """Extract file paths modified by the patch."""
    return re.findall(r'diff --git a/(\S+)', patch_diff)


_FUNC_DEF_RE = re.compile(
    # Hunk-body line that looks like a C function *definition* header.
    # Anchors: starts with diff marker (+, -, space), ends with ')' with
    # no trailing ';' (so declarations are skipped), and must contain at
    # least one whitespace-separated token before `name(` so naked macros
    # or calls don't match.
    r'^[+\- ]\s*'
    r'(?:static\s+|inline\s+|extern\s+|__\w+\s+)*'
    r'[\w\s\*]{1,80}?\s+\*?(\w+)\s*\([^;]*\)\s*$'
)


def _extract_hunk_functions(patch_diff: str) -> set[str]:
    """Extract function names associated with a patch diff.

    Primary source: the `@@ -a,b +c,d @@ func_name` context produced by
    `git diff --function-context`. Fallback: scan hunk bodies for lines
    that look like C function definitions, so hunks with blank `@@` context
    still contribute a function name.
    """
    # Note: `[ \t]*` rather than `\s*` so we don't cross a newline and
    # accidentally capture the first word of the next line when the
    # hunk context is blank (`@@ -1,3 +1,4 @@\n static int\n...`).
    funcs = set(re.findall(r'^@@[^@]*@@[ \t]*(\w+)', patch_diff, re.MULTILINE))
    for line in patch_diff.splitlines():
        if not line or line[0] not in "+- ":
            continue
        if line.startswith(("+++", "---")):
            continue
        m = _FUNC_DEF_RE.match(line)
        if m:
            funcs.add(m.group(1))
    return funcs


def _file_directory(path: str) -> str:
    """Get directory of a file path."""
    return os.path.dirname(path)


def _file_subsystem(path: str) -> str:
    """Get top-level subsystem directory (e.g., 'net', 'drivers', 'fs')."""
    parts = path.split("/")
    return parts[0] if parts else ""


def compute_locality(crash_report: str, patch_diff: str) -> Optional[dict]:
    """Compute the locality between crash site and fix site.

    Returns a dict with locality level and supporting data, or None if
    we can't determine locality (e.g., no stack trace or no diff).
    """
    if not crash_report or not patch_diff:
        return None

    frames = parse_stack_trace(crash_report)
    if not frames:
        return None

    fix_files = _extract_fix_files(patch_diff)
    if not fix_files:
        return None

    fix_functions = _extract_hunk_functions(patch_diff)

    # Normalize: stack trace files may or may not have leading path components
    # Fix files are relative to kernel root (e.g., "net/core/sock.c")
    crash_functions = [f.function for f in frames]
    crash_files = [f.file for f in frames if f.file]
    crash_dirs = {_file_directory(f) for f in crash_files}
    crash_subsystems = {_file_subsystem(f) for f in crash_files}

    fix_dirs = {_file_directory(f) for f in fix_files}
    fix_subsystems = {_file_subsystem(f) for f in fix_files}

    # Determine locality level (check most specific first)

    # 1. Same function: any crash function appears in hunk headers
    for func in crash_functions:
        if func in fix_functions:
            return {
                "locality": "same-function",
                "matched_function": func,
                "crash_files": crash_files[:5],
                "fix_files": fix_files,
                "stack_depth": len(frames),
            }

    # 2. Same file: any crash file matches a fix file
    crash_file_basenames = {os.path.basename(f) for f in crash_files}
    fix_file_basenames = {os.path.basename(f) for f in fix_files}

    # Try exact path match first, then basename match
    crash_file_set = set(crash_files)
    fix_file_set = set(fix_files)

    if crash_file_set & fix_file_set:
        return {
            "locality": "same-file",
            "matched_files": sorted(crash_file_set & fix_file_set),
            "crash_files": crash_files[:5],
            "fix_files": fix_files,
            "stack_depth": len(frames),
        }

    # Basename fallback: only trust it when the crash and fix files share
    # a top-level subsystem, otherwise `fs/foo.c` and `drivers/foo.c`
    # would collide as "same-file".
    shared_basenames = crash_file_basenames & fix_file_basenames
    if shared_basenames:
        crash_fix_by_base: dict[str, tuple[set[str], set[str]]] = {}
        for f in crash_files:
            crash_fix_by_base.setdefault(os.path.basename(f), (set(), set()))[0].add(
                _file_subsystem(f)
            )
        for f in fix_files:
            crash_fix_by_base.setdefault(os.path.basename(f), (set(), set()))[1].add(
                _file_subsystem(f)
            )
        confirmed = sorted(
            base for base in shared_basenames
            if crash_fix_by_base[base][0] & crash_fix_by_base[base][1]
        )
        if confirmed:
            return {
                "locality": "same-file",
                "matched_files": confirmed,
                "crash_files": crash_files[:5],
                "fix_files": fix_files,
                "stack_depth": len(frames),
                "note": "matched by basename within same subsystem",
            }

    # 3. Same directory
    if crash_dirs & fix_dirs:
        return {
            "locality": "same-directory",
            "matched_dirs": sorted(crash_dirs & fix_dirs),
            "crash_files": crash_files[:5],
            "fix_files": fix_files,
            "stack_depth": len(frames),
        }

    # 4. Same subsystem
    if crash_subsystems & fix_subsystems:
        return {
            "locality": "same-subsystem",
            "matched_subsystems": sorted(crash_subsystems & fix_subsystems),
            "crash_files": crash_files[:5],
            "fix_files": fix_files,
            "stack_depth": len(frames),
        }

    # 5. Different subsystem
    return {
        "locality": "different-subsystem",
        "crash_subsystems": sorted(crash_subsystems),
        "fix_subsystems": sorted(fix_subsystems),
        "crash_files": crash_files[:5],
        "fix_files": fix_files,
        "stack_depth": len(frames),
    }


class FixLocalityAnalyzer(BaseAnalyzer):
    """Analyze distance from crash site to fix site."""

    @property
    def name(self) -> str:
        return "Fix Locality Analysis"

    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        locality_counter = Counter()
        locality_examples: dict[str, list] = defaultdict(list)
        details = []
        analyzed = 0

        # Per-locality iteration counts
        locality_iterations: dict[str, list[int]] = defaultdict(list)
        # Fix file count distribution
        fix_file_counts = []
        # Stack depth distribution
        stack_depths = []

        for bug in bugs:
            crash_report = bug.crash_report
            patch_diff = ""
            for fc in bug.fix_commits:
                if fc.patch_diff:
                    patch_diff = fc.patch_diff
                    break

            result = compute_locality(crash_report, patch_diff)
            if result is None:
                continue

            analyzed += 1
            locality = result["locality"]
            locality_counter[locality] += 1

            # Track iterations per locality
            num_v = bug.num_patch_versions
            if num_v > 0:
                locality_iterations[locality].append(num_v)

            fix_file_counts.append(len(result.get("fix_files", [])))
            stack_depths.append(result.get("stack_depth", 0))

            details.append({
                "bug_id": bug.bug_id,
                "title": bug.title,
                "locality": locality,
                "num_fix_files": len(result.get("fix_files", [])),
                "stack_depth": result.get("stack_depth", 0),
            })

            if len(locality_examples[locality]) < 3:
                locality_examples[locality].append({
                    "bug_id": bug.bug_id,
                    "title": bug.title,
                    **{k: v for k, v in result.items() if k != "locality"},
                })

        # Summary
        summary = {
            "Bugs analyzed": analyzed,
            "Bugs skipped (no stack trace or diff)": len(bugs) - analyzed,
        }
        for level in LOCALITY_LEVELS:
            count = locality_counter.get(level, 0)
            summary[f"  {level}"] = f"{count} ({count / max(analyzed, 1) * 100:.1f}%)"

        if fix_file_counts:
            summary["Median fix files touched"] = sorted(fix_file_counts)[len(fix_file_counts) // 2]
        if stack_depths:
            summary["Median stack depth"] = sorted(stack_depths)[len(stack_depths) // 2]

        # Distribution table
        dist_table = []
        for level in LOCALITY_LEVELS:
            count = locality_counter.get(level, 0)
            iters = locality_iterations.get(level, [])
            median_iters = sorted(iters)[len(iters) // 2] if iters else None
            dist_table.append({
                "locality": level,
                "count": count,
                "pct": f"{count / max(analyzed, 1) * 100:.1f}%",
                "median_iterations": median_iters,
            })

        return AnalysisResult(
            name=self.name,
            summary=summary,
            details=details,
            tables={
                "distribution": dist_table,
                "examples": {l: exs for l, exs in locality_examples.items()},
            },
        )

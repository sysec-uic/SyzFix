# Dataset Analysis

All analysis tools run locally — no LLM APIs required.
Run from the **project root** with the venv activated.

## Heuristic analyzers

```bash
# Run all analyzers
python -m analysis.run_all

# Single analyzer
python -m analysis.run_all --analyzer revision

# Quick test on a sample
python -m analysis.run_all --sample 500

# List available analyzers — shows [✓] next to ones with saved results
python -m analysis.run_all --list

# Print previously saved results without re-running (instant)
python -m analysis.run_all --show
python -m analysis.run_all --show --analyzer revision
```

| Analyzer | What it answers |
|----------|----------------|
| `revision` | Why do patches need revision? (12 categories: correctness, incomplete fix, race condition, style, …) |
| `discussion` | Top reviewers, discussion depth, feedback themes, subsystem breakdown |
| `nonfunctional` | Revisions purely for non-feature issues (performance, style, commit hygiene, build/config) |
| `patchdiff` | How patches change structurally from v1→v2 (size, file scope, growth vs shrink) |
| `bugtype` | Bug type / vulnerability class taxonomy (UAF, null-ptr-deref, OOB, race, info-leak, …) |
| `fixpattern` | What the fix patch actually does (add null check, add lock, add bounds check, fix refcount, …) |
| `locality` | Distance from crash site to fix site (same function, same file, same directory, different subsystem) |
| `difficulty` | Composite difficulty score per bug → easy / medium / hard tiers |
| `infosuff` | Information sufficiency: reproducer impact, crash report truncation, token overlap, file path prediction |

Results are saved to `analysis/results/` as JSON and CSV — use `--show` to re-display them without re-running.

### Bug characterization analyzers

**Bug Type Classification** (`bugtype`) parses the bug title and crash report to
classify each bug into one of ~17 vulnerability/error types (use-after-free,
null-ptr-deref, out-of-bounds-read/write, memory-leak, data-race, deadlock,
info-leak, UBSAN, etc.). Outputs per-type median patch size, iterations, and
time-to-fix.

**Fix Pattern Taxonomy** (`fixpattern`) classifies *what the patch does* by
analyzing the diff content: add-null-check, add-lock, add-bounds-check,
add-missing-free, fix-refcount, add-init, fix-order, add-return-check,
type-change, remove-code. Also reports cumulative coverage (e.g., top-5 patterns
cover X% of fixes) and co-occurrence between patterns.

**Fix Locality** (`locality`) compares the crash site (parsed from the stack
trace in the crash report) against the fix site (from the patch diff). Reports
what fraction of fixes are in the same function, same file, same directory, same
subsystem, or a different subsystem. Uses `parse_stack_trace()` from
`analysis/filters.py` to extract function names and file paths from kernel
stack traces.

**Difficulty Stratification** (`difficulty`) computes a composite difficulty
score per bug using: patch size, number of files modified, patch iterations,
fix locality, time-to-fix, and reproducer availability. Bugs are grouped into
easy / medium / hard tiers. Reports per-tier statistics and feature contribution
breakdown.

**Information Sufficiency** (`infosuff`) analyzes what input signals are
available and how they correlate with fix properties:
- Reproducer availability (C + syz, syz-only, none) vs. fix time and iterations
- Crash report truncation analysis (how many stack frames are retained in first N lines)
- Token overlap (Jaccard) between crash report / reproducer and patch diff
- File path prediction accuracy (can the fix file be predicted from the stack trace?)

### Adding a new analyzer

```python
# analysis/analyzers/my_analyzer.py
from analysis.analyzers.base import BaseAnalyzer, AnalysisResult

class MyAnalyzer(BaseAnalyzer):
    @property
    def name(self) -> str:
        return "My Custom Analysis"

    def analyze(self, bugs: list) -> AnalysisResult:
        return AnalysisResult(name=self.name, summary={...})
```

Then register it in `analysis/run_all.py`.

## Iteration timeline plot

Produces a stacked area chart of average days between patch iterations, by year —
matching the style of Figure 1 in the paper.

```bash
# Save as PDF (recommended for papers)
python -m analysis.plot_iteration_timeline --out analysis/results/figure1.pdf

# Save as PNG
python -m analysis.plot_iteration_timeline --out analysis/results/figure1.png

# Filter year range
python -m analysis.plot_iteration_timeline --min-year 2018 --max-year 2025 \
    --out analysis/results/figure1.pdf

# Interactive window
python -m analysis.plot_iteration_timeline --no-save
```

The chart shows:

| Layer | Meaning |
|-------|---------|
| `Report→Iter1` | Days from first crash report to v1 patch submission |
| `Iter1→Iter2` | Days between v1 and v2 |
| `Iter2→Iter3` | … and so on up to Iter5+ |
| Bug count line | Number of bugs fixed that year (right y-axis) |

**Key finding from the data:** `Report→Iter1` dropped from ~440 days (2017) to
~7 days (2026), showing the kernel community has become dramatically faster at
responding to syzbot reports over time.

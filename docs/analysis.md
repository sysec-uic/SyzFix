# Dataset Analysis

All analysis tools run locally — no LLM APIs required.
Run from the **project root** with the venv activated.

To inspect individual bugs before or alongside running the analyzers, see **[exploring.md](exploring.md)**.

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
| `casestudy` | Case study finder: ranks bugs by composite "interestingness" score across 7 dimensions; surfaces paper-friendly examples |
| `insights` | Insight clusters: cross-references bug type × fix pattern × locality × revision reasons to find named categories of interesting bugs |

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

**Case Study Finder** (`casestudy`) ranks every bug with a patch diff by a
composite *interestingness* score (max 21) across seven dimensions:

| Dimension | Signal | Max pts |
|-----------|--------|---------|
| Patch iterations | `num_patch_versions` | 3 |
| Discussion depth | Human review count (bots and stable-backport threads excluded) | 3 |
| Structural change | abs(v2 lines − v1 lines) | 3 |
| Fix time | Days from first crash to merged fix | 3 |
| Fix locality | Crash site → fix site distance | 3 |
| Scope change | abs(v2 files − v1 files) | 3 |
| Info scarcity | Missing C reproducer / syz reproducer / stack trace | 3 |

Each bug entry reports all dimension scores, per-version patch sizes (`v1_lines`,
`v2_lines`), the final merged patch size, and a `paper_friendly` flag (True when
the final patch is ≤ 50 lines — small enough to include in a paper figure).
Auto-generated narrative hooks summarise what makes each case compelling.

Three result tables are saved:
- `ranked_candidates`: top 50 by composite score, all metrics
- `top_paper_friendly`: top 20 filtered to paper-friendly cases
- `top_by_dimension`: top 5 per dimension (for picking diverse case studies)

After running the analyzer, generate paper-ready markdown narratives for your
chosen bugs with:

```bash
# Run the analyzer
python -m analysis.run_all --analyzer casestudy

# Top 4 paper-friendly narratives (reads saved results)
python -m analysis.generate_case_study --from-results --paper-friendly --top 4

# Narratives for specific bug IDs (partial IDs supported)
python -m analysis.generate_case_study 0438378d6f157baae1a2 94cc2a66fc228b23f360
```

Each narrative includes: overview, per-version patch complexity table, first
20 lines of the crash report, patch version timeline, top review highlights
per version, and the full final fix diff (truncated to 30 lines if > 60 lines).

**Insight Clusters** (`insights`) cross-references bug type, fix pattern,
locality, difficulty, and revision reasons to identify eight named categories
of bugs that share interesting characteristics. Each cluster is defined by a
predicate over per-bug features, and the analyzer reports statistics,
representative examples, overlap analysis, and a "paper insight" text.

| Cluster | Rule | Description |
|---------|------|-------------|
| Misleading Symptoms | Bug type suggests pattern X, fix uses pattern Y | Surface symptom misleads diagnosis |
| Deceptively Simple | Final patch ≤ 10 lines, but > 180 days or ≥ 3 iterations | Difficulty is in understanding, not code |
| Approach Revolution | > 50% structural change between v1 and v2 | Developer completely changed approach |
| Cross-Subsystem Root Cause | Fix in different subsystem from crash | Requires deep architectural knowledge |
| Review-Rescued | Revision reasons include correctness / incomplete fix | Community review caught critical issues |
| Long-Lived (> 1 year) | fix_days > 365 | What makes some bugs fundamentally harder |
| Concurrency Labyrinth | Deadlock/data-race type, add-lock fix, or race revision | Concurrency as a distinct challenge class |
| Information Desert | No C or syz reproducer | Fixed from crash report alone |

Three result tables:
- `cluster_overview`: per-cluster count, statistics, top bug types and fix patterns
- `cluster_overlap`: pairwise overlap between clusters (bugs in multiple categories)
- `membership_distribution`: how many clusters each bug belongs to

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

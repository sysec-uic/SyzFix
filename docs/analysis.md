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

# List available analyzers
python -m analysis.run_all --list
```

| Analyzer | What it answers |
|----------|----------------|
| `revision` | Why do patches need revision? (12 categories: correctness, incomplete fix, race condition, style, …) |
| `discussion` | Top reviewers, discussion depth, feedback themes, subsystem breakdown |
| `nonfunctional` | Revisions purely for non-feature issues (performance, style, commit hygiene, build/config) |
| `patchdiff` | How patches change structurally from v1→v2 (size, file scope, growth vs shrink) |

Results are saved to `analysis/results/` as JSON and CSV.

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

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
| `evolution` | **Patch evolution causal analysis**: links reviewer feedback on vN to structural changes in vN+1, across all consecutive version transitions |
| `backport` | **Patch downstream propagation**: stable-targeting intent (Cc:stable, Fixes: tag), LTS version coverage, backport lag, and subsystem backport rates |
| `backport-gt` | **Ground-truth backport comparison**: matches syzbot fix commits against 324k cherry-picks in linux-stable.git; compares coverage depth and lag against all-kernel baseline |

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

### Patch Evolution Causal Analysis (`evolution`)

This is the **key differentiator** of SyzFix over prior datasets — it captures not just *that* patches evolve, but *why* and *how*: which reviewer feedback drove each revision, and what changed as a result.

```bash
# Run on full dataset (~17 seconds)
python -m analysis.run_all --analyzer evolution

# Quick test on a small sample
python -m analysis.run_all --analyzer evolution --sample 200

# Re-display saved results without re-running
python -m analysis.run_all --show --analyzer evolution
```

For every bug with ≥ 2 patch versions, it analyzes **every consecutive vN → vN+1 transition**:

1. Extracts human reviews from vN's discussion thread (filters out bots, stable-backport noise, trivial tag-only replies)
2. Classifies each review into the same 12 feedback categories used by the `revision` analyzer (correctness, incomplete_fix, race_condition, …)
3. Extracts actionable feedback snippets (imperative verbs, requests, suggestions)
4. Extracts "Changes since vN" changelog notes from the vN+1 patch submission
5. Classifies the changelog text with the same taxonomy to detect which feedback was explicitly acknowledged
6. Computes the structural diff delta between vN and vN+1 (line count change, files added/removed, scope change)
7. Measures time from last review on vN to first submission of vN+1

Four CSV tables are saved to `analysis/results/patch_evolution_causal_analysis/`:

| File | Rows | What it contains |
|------|------|-----------------|
| `iteration_transitions.csv` | One per vN→vN+1 | bug_id, from/to version, num_reviews, feedback_categories, changelog_categories, line_delta, scope_change, time_to_next_hours, responsiveness_score |
| `feedback_impact.csv` | One per category | How often each feedback category appears, average structural change it causes, how often it appears in changelogs |
| `evolution_summary.csv` | One per bug | Total reviews, feedback items, line delta, avg responsiveness, across all transitions |
| `response_patterns.csv` | One per category | How often each category is addressed in the next version's changelog, median response time |

**Key findings from the full dataset (1,099 bugs, 1,600 transitions):**

- 78% of transitions have substantive human reviewer feedback
- 36% of transitions have explicit "Changes since vN" changelog notes
- Top feedback categories: `correctness` (42%), `commit_message` (40%), `api_design` (37%)
- Highest structural impact: `performance` (avg 53 lines changed), `style_convention` (51), `race_condition` (48)
- Best changelog alignment: `commit_message` (21%), `api_design` (15%), `style_convention` (13%) — most feedback is implicit, not written into changelogs

### Backport Downstream Propagation (`backport`)

Linux kernel fixes follow a lifecycle unique among open-source projects: a patch
lands on mainline first, then gets cherry-picked into active stable/LTS branches
(4.14, 4.19, 5.4, 5.10, 5.15, 6.1, …) by stable maintainers. This analyzer
extracts and quantifies that downstream propagation.

```bash
# Run on full dataset
python -m analysis.run_all --analyzer backport

# Quick test on a sample
python -m analysis.run_all --analyzer backport --sample 500

# Re-display saved results
python -m analysis.run_all --show --analyzer backport
```

**Signals extracted per bug:**

| Signal | Source | What it means |
|--------|--------|---------------|
| `cc_stable` | Fix commit diff / patch submission email | Author explicitly targeted stable trees |
| `fixes_tag` | Fix commit diff | `Fixes: <hash>` tag — stable maintainers auto-pick these |
| `target_versions` | Stable review thread subjects | Which LTS branches actually received the fix |
| `num_lts_versions` | Counted from above | Breadth of downstream coverage |
| `backport_lag_days` | Upstream commit date → first stable review thread | How quickly a fix propagates downstream |

**Six output tables** saved to `analysis/results/backport_downstream_propagation/`:

| Table | What it shows |
|-------|--------------|
| `lts_version_distribution` | Bug count backported to each LTS version (4.19, 4.14, 4.9, 5.4 dominate) |
| `stable_intent_signals` | Breakdown: Cc:stable only / Fixes: only / both / neither |
| `backport_lag_distribution` | Time-to-backport in buckets (0–3, 4–7, 8–14, 15–30, 31–60, 60+ days) |
| `subsystem_backport_rates` | Which subsystems get backported most (drivers/kernel ~65–70%) |
| `coverage_tiers` | How many LTS versions each bug reaches (0 / 1 / 2–3 / 4–5 / 6+) |
| `top_backport_coverage` | Examples of bugs with the widest LTS backport coverage |

**Key findings from the full dataset (5,043 bugs with fix diffs):**
- 40.0% of fixes (2,018) have stable backport threads
- Each backported fix reaches an average of **3.9 LTS versions**
- Median backport lag is **16.5 days** after upstream merge
- `Fixes:` tag is present on 61.6% of fixes; `Cc: stable` on only 17.2% — backporting is largely implicit via automation
- 33.0% of fixes have neither signal and never reach stable kernels
- Top LTS recipients: 4.14 (49.3%), 4.19 (45.8%), 4.9 (41.2%), 5.4 (37.2%)
- `sound/` (65.7%), `crypto/` (57.9%), and `drivers/` (54.1%) have the highest backport rates; `io_uring/` (14.8%) the lowest

### Backport Ground Truth Comparison (`backport-gt`)

This analyzer provides a **ground-truth comparison** of syzbot fix backport patterns
against the full Linux kernel baseline, using the actual cherry-pick history extracted
from `linux-stable.git`.  Rather than inferring backport coverage from discussion
threads (as `backport` does), it directly looks up each syzbot fix commit hash in the
stable tree's 10+ year cherry-pick history.

#### Prerequisites

You need a bare clone of `linux-stable.git` (~6 GB) and the extracted cherry-pick map:

```bash
# 1. Clone linux-stable (one-time, ~6 GB)
git clone --bare \
    git://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable.git \
    syzbot-dataset/data/raw/linux-stable.git

# 2. Extract cherry-pick mappings (~5–15 min)
python -m syzbot-dataset.scraper.stable_cherrypick

# 3. Run the analyzer
python -m analysis.run_all --analyzer backport-gt
```

The extraction produces `syzbot-dataset/data/processed/cherrypick_map.json` (~52 MB),
containing 324,968 cherry-picks across 81 stable branches.  Once extracted, the
analyzer runs in seconds.

```bash
# Re-display saved results without re-running
python -m analysis.run_all --show --analyzer backport-gt
```

#### How the cherry-pick extractor works

`syzbot-dataset/scraper/stable_cherrypick.py` walks every `linux-X.Y.y` branch in the
bare repo and parses two upstream-reference patterns from commit bodies:

| Pattern | Example | Used by |
|---------|---------|---------|
| `(cherry picked from commit HASH)` | Standard `git cherry-pick -x` | Most stable commits |
| `[ Upstream commit HASH ]` | `[ Upstream commit a1b2c3... ]` | Stable maintainer format |

For each match, it records the upstream hash, stable hash, branch, and commit date.
Upstream commit dates are pulled from the stable repo's `master` branch so backport
lag can be computed without requiring a separate torvalds/linux clone.

#### Six output tables

Saved to `analysis/results/backport_ground_truth_comparison/`:

| Table | What it shows |
|-------|--------------|
| `backport_rate_comparison` | Side-by-side syzbot vs. all-kernel: commits, backport rate, avg branches, median lag |
| `per_branch_rates` | Per-LTS-version: all-kernel picks vs. syzbot picks for every branch from 3.0 to 6.x |
| `lag_comparison` | Median backport lag per branch for syzbot vs. all-kernel |
| `coverage_tiers_comparison` | Distribution of LTS coverage breadth (0 / 1 / 2–3 / 4–5 / 6+ branches) |
| `subsystem_comparison` | Per-subsystem syzbot backport rate (ground-truth) |
| `syzbot_missing_backports` | Syzbot fixes whose upstream hash is absent from all stable branches |

#### Key findings (full dataset, 6,946 bugs)

| Metric | Syzbot fixes | All-kernel baseline |
|--------|-------------|---------------------|
| Commits examined | 6,949 | 92,414 unique upstream |
| Found in stable trees | **2,694 (38.8%)** | 92,414 (100%, by definition) |
| Avg stable branches per backported fix | **3.72** | 3.52 |
| Median backport lag | **40.5 days** | 35.0 days |

Coverage tiers (syzbot vs. all-kernel):

| Branches reached | Syzbot | All-kernel |
|-----------------|--------|------------|
| 0 (not backported) | 61.2% | — |
| 1 | 9.2% | 33.9% |
| 2–3 | 11.1% | 36.4% |
| 4–5 | 7.7% | 15.2% |
| 6+ | 10.8% | 14.5% |

**Interpretation:** When a syzbot fix *is* backported, it propagates slightly more
broadly than the average kernel fix (3.72 vs 3.52 branches) and takes slightly longer
to appear (40.5 vs 35.0 days median lag) — consistent with syzbot fixes targeting
security-sensitive memory-safety bugs that require careful review before backporting.
The 61.2% of syzbot fixes absent from stable trees represents fixes that either (a)
lack `Fixes:`/`Cc:stable` signals, (b) address mainline-only subsystems (e.g.,
`io_uring`), or (c) are too recent to have been backported yet.

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

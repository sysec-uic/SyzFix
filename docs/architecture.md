# SyzFix Architecture

## Mermaid Diagrams

### End-to-End Pipeline

```mermaid
flowchart LR
    subgraph Collect ["1. Collect (syzbot-dataset/)"]
        SYZ[syzbot API] --> PIPE[pipeline.py]
        GIT[git.kernel.org] --> PIPE
        LORE[lore.kernel.org] --> PIPE
        PW[patchwork.kernel.org] --> PIPE
        STABLE[linux-stable.git] --> PIPE
        PIPE --> PROC[(data/processed/<br/>~6,900 bug JSONs)]
    end

    subgraph Analyze ["2. Analyze (analysis/)"]
        PROC --> RUN[run_all.py]
        RUN --> A1[bug_type_classifier]
        RUN --> A2[fix_patterns]
        RUN --> A3[revision_reasons<br/>12 categories]
        RUN --> A4[patch_evolution<br/>v1→v2 causal chains]
        RUN --> A5[fix_locality]
        RUN --> A6[difficulty_stratification]
        RUN --> A7[9 more analyzers...]
        A1 & A2 & A3 & A4 & A5 & A6 & A7 --> RES[(analysis/results/)]
    end

    subgraph Memorize ["3. Memorize (memory/)"]
        PROC --> BUILD[build.py]
        RES --> BUILD
        BUILD --> L1[Layer 1: Instance Memory<br/>~5,000 per-bug records<br/>46 MB JSONL]
        BUILD --> L2[Layer 2: Pattern Memory<br/>141 fix strategies<br/>12 review lessons]
        BUILD --> L3[Layer 3: Distilled Rules<br/>140 actionable rules]
        BUILD --> IDX[FAISS Indices<br/>crash + patch embeddings<br/>inverted indices]
    end

    subgraph Train ["4. Train (prepare_training.py)"]
        PROC --> PREP[prepare_training.py]
        PREP --> T1[bug_to_patch<br/>4,217 train]
        PREP --> T2[patch_review<br/>5,021 train]
        PREP --> T3[patch_improvement<br/>3,251 train]
        PREP --> T4[dpo pairs<br/>3,724 train]
        PREP --> T5[commit_message<br/>4,256 train]
    end

    subgraph Agent ["5. Agent Inference"]
        CRASH[New syzbot crash] --> RET[MemoryRetriever]
        L1 & L2 & L3 & IDX --> RET
        RET --> CTX[Memory Context<br/>similar bugs + strategies<br/>+ rules + lessons]
        CTX --> LLM[LLM<br/>Claude / fine-tuned]
        LLM --> PATCH[Generated Patch<br/>/ Review]
    end

    PROC --> Train
    Memorize --> Agent
```

### Memory Retrieval Flow

```mermaid
flowchart TD
    INPUT["Crash Report + v1 Patch"] --> CLASSIFY["Classify crash type<br/>(UAF, null-ptr, deadlock, ...)"]

    CLASSIFY --> FAISS["FAISS Semantic Search<br/>crash_embeddings → top-k"]
    CLASSIFY --> STRUCT["Structured Lookup<br/>by bug_type in pattern_memory"]
    CLASSIFY --> COOC["Co-occurrence Lookup<br/>fix_pattern → review_lessons"]
    CLASSIFY --> RULES["Distilled Rules Match<br/>bug_type × fix_pattern<br/>→ known pitfalls"]

    FAISS --> SIM["Similar Past Bugs<br/>(crash + fix + outcome)"]
    STRUCT --> STRAT["Fix Strategies<br/>(pattern, median size,<br/>common pitfalls)"]
    COOC --> LESSONS["Review Lessons<br/>(correctness, error_handling,<br/>scope, ...)"]
    RULES --> TIPS["Actionable Tips<br/>('Include Fixes: tag',<br/>'Test allmodconfig', ...)"]

    SIM & STRAT & LESSONS & TIPS --> FORMAT["Format as Markdown<br/>memory/context.py"]
    FORMAT --> PROMPT["Inject into LLM Prompt<br/>as system context"]
```

### Evaluation Pipeline

```mermaid
flowchart TD
    SPLIT["split.json<br/>126 eval bugs"] --> LOOP["For each eval bug"]

    LOOP --> EXTRACT["Extract crash_report<br/>+ v1 patch diff"]
    EXTRACT --> GT["Ground Truth<br/>actual revision categories<br/>from v1→v2 discussion"]
    EXTRACT --> PREDICT["predict_review()<br/>memory context + Claude CLI"]

    PREDICT --> PRED_CAT["Predicted Categories<br/>[correctness, scope, ...]"]
    GT --> COMPARE["Compare"]
    PRED_CAT --> COMPARE

    COMPARE --> METRICS["Per-bug Metrics<br/>precision / recall / F1"]
    METRICS --> AGG["Aggregate<br/>macro-avg & micro-avg"]

    AGG --> WITH["eval_results.json<br/>(with memory)"]
    AGG --> WITHOUT["eval_results_baseline.json<br/>(without memory)"]

    WITH & WITHOUT --> DIFF["Δ shows memory impact"]
```

### Data Model

```mermaid
erDiagram
    BugEntry ||--o{ PatchVersion : has
    BugEntry ||--o{ Discussion : has
    BugEntry {
        string bug_id
        string title
        string crash_report
        string c_reproducer
        string syz_reproducer
        string fix_commit
        string patch_diff
    }
    PatchVersion ||--|{ Email : contains
    PatchVersion {
        int version_num
        string diff
    }
    Discussion ||--|{ Email : contains
    Email {
        string author
        string date
        string subject
        string body
    }

    BugMemoryEntry {
        string bug_id
        string bug_type
        string fix_pattern
        string subsystem
        string difficulty_tier
        string fix_locality
        list revision_categories
        list insight_clusters
    }

    PatternMemory ||--|{ FixStrategy : contains
    PatternMemory ||--|{ ReviewLesson : contains
    FixStrategy {
        string bug_type
        string fix_pattern
        int frequency
        int median_patch_lines
        list common_pitfalls
    }
    ReviewLesson {
        string category
        int frequency
        string actionable_advice
    }

    DistilledRule {
        string bug_type
        string fix_pattern
        string revision_category
        int frequency
        string rule_text
        string example_snippet
    }
```

### Knowledge Layers

```mermaid
graph TB
    subgraph "Layer 3: Distilled Rules (140 rules, 109 KB)"
        R1["'When fixing null-ptr-deref with fix-order,<br/>reviewers flag commit_message (17 cases).<br/>Include Fixes: tag.'"]
        R2["'When fixing UAF with add-lock,<br/>reviewers flag scope (12 cases).<br/>Keep patch focused on one change.'"]
        R3["...138 more rules"]
    end

    subgraph "Layer 2: Pattern Memory (141 strategies + 12 lessons, 84 KB)"
        S1["warning × fix-order<br/>186 cases, median 37 lines<br/>pitfalls: correctness, error_handling"]
        S2["UAF × add-lock<br/>108 cases, median 32 lines<br/>pitfalls: scope, commit_message"]
        L1["correctness (21 cases):<br/>Double-check logic"]
        L2["error_handling (18 cases):<br/>Audit every goto"]
    end

    subgraph "Layer 1: Instance Memory (~5,000 bugs, 46 MB)"
        I1["bug_abc123: UAF in net/ipv4<br/>fix: add-lock, hard, cross-file<br/>revisions: correctness, scope"]
        I2["bug_def456: null-ptr in drivers/usb<br/>fix: add-null-check, easy, same-fn<br/>revisions: commit_message"]
        I3["...~4,998 more"]
    end

    R1 & R2 & R3 -.->|"distilled from"| S1 & S2 & L1 & L2
    S1 & S2 & L1 & L2 -.->|"aggregated from"| I1 & I2 & I3
```

---

## System Overview (ASCII)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              SyzFix Pipeline                                │
│                                                                             │
│  ┌───────────────┐    ┌───────────────┐    ┌───────────────┐                │
│  │  1. COLLECT    │───▶│  2. ANALYZE   │───▶│  3. MEMORIZE  │──┐            │
│  │  (syzbot-      │    │  (analysis/)   │    │  (memory/)     │  │            │
│  │   dataset/)    │    │               │    │               │  │            │
│  └───────────────┘    └───────────────┘    └───────────────┘  │            │
│         │                                         │            │            │
│         ▼                                         ▼            ▼            │
│  ┌───────────────┐                         ┌───────────────────────┐       │
│  │  4. TRAIN      │                         │  5. AGENT (inference) │       │
│  │  (SFT / DPO)   │                         │  Claude + Memory RAG  │       │
│  └───────────────┘                         └───────────────────────┘       │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Stage 1: Data Collection (`syzbot-dataset/`)

Async multi-source crawler that builds the raw dataset.

```
                    ┌─────────────────────┐
                    │   syzbot API        │
                    │ syzkaller.appspot   │
                    │ .com                │
                    └────────┬────────────┘
                             │  bug list, crash reports,
                             │  reproducers, fix commits
                             ▼
┌──────────────┐    ┌─────────────────────┐    ┌──────────────┐
│ git.kernel   │    │                     │    │  lore.kernel  │
│ .org         │───▶│    pipeline.py      │◀───│  .org         │
│ (patch diffs)│    │   (orchestrator)    │    │ (discussions) │
└──────────────┘    │                     │    └──────────────┘
                    │  process_single_bug │
┌──────────────┐    │                     │    ┌──────────────┐
│ patchwork    │───▶│                     │    │ linux-stable  │
│ .kernel.org  │    └─────────┬───────────┘    │ .git (cherry- │
│ (fallback)   │              │                │  pick map)    │
└──────────────┘              │                └──────┬───────┘
                              ▼                       │
                    ┌─────────────────────┐           │
                    │   data/processed/   │◀──────────┘
                    │   ~6,900 bug JSONs  │
                    └─────────┬───────────┘
                              │
                    ┌─────────▼───────────┐
                    │   data/dataset/     │──────▶  HuggingFace Hub
                    │   JSONL export      │        xiaoguangwang/
                    └─────────────────────┘        syzfix-dataset
```

### Per-Bug Data Model

```
BugEntry
├── bug_id              # syzbot hash
├── title               # e.g. "WARNING in sk_stream_kill_queues"
├── crash_report        # full kernel crash log
├── c_reproducer        # C code that triggers the bug
├── syz_reproducer      # syzkaller program
├── fix_commit          # upstream commit SHA + message
├── patch_diff          # the accepted patch diff
├── patch_versions[]    # v1, v2, ... vN patch submissions
│   ├── version_num
│   ├── messages[]      # email thread (author, date, body)
│   └── diff            # patch diff for this version
└── discussions[]       # mailing list threads
    └── messages[]      # reviewer feedback emails
```

## Stage 2: Analysis (`analysis/`)

14 heuristic analyzers that characterize every bug across multiple dimensions.

```
                         analysis/run_all.py
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
        ▼                      ▼                      ▼
  ┌───────────┐         ┌───────────┐          ┌───────────┐
  │  Bug-Level │         │Patch-Level│          │ Meta-Level│
  │  Analyzers │         │ Analyzers │          │ Analyzers │
  └─────┬─────┘         └─────┬─────┘          └─────┬─────┘
        │                     │                      │
        ▼                     ▼                      ▼
  ┌──────────┐         ┌──────────┐          ┌──────────────┐
  │bug_type  │         │patch_diff│          │revision_     │
  │classifier│         │_analysis │          │reasons (12   │
  │(UAF,null,│         │(size,    │          │categories)   │
  │OOB,dead- │         │ files,   │          │              │
  │lock,...) │         │ delta)   │          │discussion_   │
  │          │         │          │          │lessons       │
  │fix_      │         │patch_    │          │              │
  │patterns  │         │evolution │          │non_          │
  │(add-null,│         │(v1→v2    │          │functional    │
  │add-lock, │         │ causal   │          │              │
  │fix-order)│         │ chains)  │          │case_study_   │
  │          │         │          │          │finder        │
  │fix_      │         │backport_ │          │              │
  │locality  │         │downstream│          │insight_      │
  │(same-fn, │         │          │          │clusters      │
  │same-file)│         │backport_ │          └──────┬───────┘
  │          │         │comparison│                 │
  │difficulty│         └────┬─────┘                 │
  │stratific-│              │                       │
  │ation     │              │                       │
  │          │              │                       │
  │informati-│              │                       │
  │on_suffic-│              │                       │
  │iency     │              │                       │
  └────┬─────┘              │                       │
       │                    │                       │
       └────────────────────┼───────────────────────┘
                            ▼
                   analysis/results/
                   (one dir per analyzer
                    with result.json)
```

### 12 Revision Categories (from `revision_reasons.py`)

```
correctness ──────── Logic errors, wrong behavior
incomplete_fix ───── Doesn't fix all affected paths
race_condition ───── Missing synchronization
error_handling ───── Resource leaks on error paths
style_convention ─── checkpatch.pl, naming conventions
commit_message ───── Missing Fixes: tag, Signed-off-by
performance ──────── Unnecessary overhead
api_design ───────── Wrong API usage, deprecation
scope ────────────── Patch too broad / too narrow
memory_safety ────── UAF, double-free, buffer issues
documentation ────── Missing comments, kernel-doc
config_build ─────── Kconfig, build warnings
```

## Stage 3: Memory System (`memory/`)

Three-layer knowledge architecture built from the training split.

```
                    memory/build.py
                         │
         ┌───────────────┼───────────────┐
         │               │               │
         ▼               ▼               ▼
  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
  │  Layer 1:   │ │  Layer 2:   │ │  Layer 3:   │
  │  Instance   │ │  Pattern    │ │  Distilled  │
  │  Memory     │ │  Memory     │ │  Rules      │
  └──────┬──────┘ └──────┬──────┘ └──────┬──────┘
         │               │               │
         ▼               ▼               ▼
  instance_       pattern_         distilled_
  memory.jsonl    memory.json      rules.json
  (46 MB,         (84 KB,          (109 KB,
   ~5000 bugs)    141 strategies   140 rules)
                  12 lessons)

  ┌─────────────────────────────────────────────┐
  │            Retrieval Indices                │
  │                                             │
  │  crash_embeddings.npy ──▶ faiss_crash.index │
  │  patch_embeddings.npy ──▶ faiss_patch.index │
  │  inverted_indices.json (type/pattern/subsys)│
  │  bug_id_map.json (row → bug_id)             │
  └─────────────────────────────────────────────┘
```

### Layer Details

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 1: Instance Memory (per-bug)                          │
│                                                             │
│  For each of ~5000 bugs:                                    │
│  ├── bug_id, title, bug_type, subsystem                     │
│  ├── fix_pattern (add-null-check, fix-order, ...)           │
│  ├── crash_summary (first 500 chars)                        │
│  ├── patch_summary (files changed, lines added/removed)     │
│  ├── difficulty_tier (easy / medium / hard)                 │
│  ├── fix_locality (same-function / same-file / cross-file)  │
│  ├── revision_categories[] from reviewer feedback           │
│  └── insight_clusters[] (misleading symptoms, etc.)         │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ Layer 2: Pattern Memory (aggregated)                        │
│                                                             │
│  fix_strategies[141]:                                       │
│  ├── "For UAF bugs, add-lock pattern (108 cases)"           │
│  ├── common_pitfalls: ["scope", "commit_message"]           │
│  └── median_patch_lines: 32                                 │
│                                                             │
│  review_lessons[12]:                                        │
│  ├── "correctness (21 cases): Double-check logic"           │
│  └── "error_handling (18 cases): Audit every goto"          │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ Layer 3: Distilled Rules (actionable)                       │
│                                                             │
│  140 rules, each:                                           │
│  ├── bug_type + fix_pattern + revision_category             │
│  ├── frequency (how often this combo was flagged)           │
│  ├── rule_text: "When fixing null-ptr-deref with            │
│  │    fix-order, reviewers commonly flag commit_message      │
│  │    issues. Include Fixes: tag..."                        │
│  └── example_snippet from real review                       │
└─────────────────────────────────────────────────────────────┘
```

## Stage 4: Training Data (`syzbot-dataset/prepare_training.py`)

Five task-specific datasets for fine-tuning.

```
  processed bugs
       │
       ▼
  prepare_training.py
       │
       ├──▶ bug_to_patch.jsonl      (crash → patch generation)
       │    4,217 train / 503 val / 245 test
       │
       ├──▶ patch_review.jsonl      (patch → reviewer feedback)
       │    5,021 / 609 / 311
       │
       ├──▶ patch_improvement.jsonl (v1+feedback → v2 patch)
       │    3,251 / 415 / 191
       │
       ├──▶ dpo.jsonl               (chosen=final vs rejected=v1)
       │    3,724 / 463 / 216
       │
       └──▶ commit_message.jsonl    (patch → commit message)
            4,256 / 506 / 246
```

## Stage 5: Agent Inference (Memory-Augmented)

How the memory system serves an LLM agent at inference time.

```
  New syzbot crash
       │
       ▼
  ┌──────────────────┐
  │ Classify crash   │──▶ bug_type (e.g., "use-after-free")
  │ type             │
  └────────┬─────────┘
           │
  ┌────────▼─────────┐     ┌────────────────────────┐
  │ FAISS semantic   │────▶│ Top-k similar crashes   │
  │ search           │     │ from instance memory    │
  └────────┬─────────┘     └────────────────────────┘
           │
  ┌────────▼─────────┐     ┌────────────────────────┐
  │ Structured       │────▶│ Fix strategies for      │
  │ lookup           │     │ this bug_type           │
  └────────┬─────────┘     └────────────────────────┘
           │
  ┌────────▼─────────┐     ┌────────────────────────┐
  │ Co-occurrence    │────▶│ Review lessons for      │
  │ lookup           │     │ top fix pattern         │
  └────────┬─────────┘     └────────────────────────┘
           │
  ┌────────▼─────────┐     ┌────────────────────────┐
  │ Distilled rules  │────▶│ "When fixing UAF with   │
  │ matching         │     │  add-lock, watch for..."│
  └────────┬─────────┘     └────────────────────────┘
           │
           ▼
  ┌──────────────────────────────────────────┐
  │        Memory Context (Markdown)         │
  │                                          │
  │  ## Similar Past Bugs                    │
  │  ## Recommended Fix Strategies           │
  │  ## Common Review Feedback               │
  │  ## Distilled Rules                      │
  └────────────────┬─────────────────────────┘
                   │
                   ▼
  ┌──────────────────────────────────────────┐
  │         LLM (Claude / fine-tuned)        │
  │                                          │
  │  System: You are a kernel patch expert.  │
  │  Context: {memory_context}               │
  │  Task: Fix this crash / Review patch     │
  └────────────────┬─────────────────────────┘
                   │
                   ▼
           Generated Patch / Review
```

## Evaluation Pipeline (`memory/evaluate.py`)

```
  126 eval bugs (held-out from training)
       │
       ▼
  ┌───────────────────────────────────────┐
  │  For each eval bug:                   │
  │                                       │
  │  1. Extract crash_report + v1 patch   │
  │  2. Compute ground truth from v1→v2   │
  │     discussion (actual categories)    │
  │  3. Retrieve memory context           │
  │  4. Claude predicts categories        │
  │  5. Compare: precision / recall / F1  │
  └───────────────────┬───────────────────┘
                      │
                      ▼
  ┌───────────────────────────────────────┐
  │  eval_results.json (with memory)     │
  │  eval_results_baseline.json (w/o)    │
  │                                       │
  │  Macro-avg P / R / F1                 │
  │  Micro-avg P / R / F1                 │
  └───────────────────────────────────────┘
```

## File / Directory Map

```
SyzFix/
├── README.md
├── requirements.txt
├── test_memory_retrieval.py
│
├── syzbot-dataset/                # Stage 1: Collection
│   ├── main.py                    #   CLI: collect, export, stats
│   ├── pipeline.py                #   Orchestrator
│   ├── models.py                  #   Data models
│   ├── storage.py                 #   SQLite + JSON persistence
│   ├── scraper/                   #   Web scrapers
│   │   ├── syzbot.py              #     syzbot API
│   │   ├── git_kernel.py          #     git.kernel.org patches
│   │   ├── lore.py                #     lore.kernel.org discussions
│   │   ├── patchwork.py           #     patchwork.kernel.org fallback
│   │   └── stable_cherrypick.py   #     linux-stable cherry-picks
│   ├── export.py                  #   JSONL export
│   ├── prepare_training.py        #   SFT/DPO training data
│   ├── upload_hf.py               #   HuggingFace upload
│   ├── view.py                    #   Interactive explorer
│   └── data/                      #   All collected data (not in git)
│       ├── raw/
│       ├── processed/             #   ~6,900 bug JSONs
│       ├── dataset/               #   JSONL export
│       └── training/              #   Task-specific JSONL
│
├── analysis/                      # Stage 2: Analysis
│   ├── run_all.py                 #   CLI: run analyzers
│   ├── loader.py                  #   Shared data loader
│   ├── filters.py                 #   Noise filtering
│   ├── analyzers/                 #   14 heuristic analyzers
│   │   ├── bug_type_classifier.py
│   │   ├── fix_patterns.py
│   │   ├── fix_locality.py
│   │   ├── revision_reasons.py
│   │   ├── patch_evolution.py
│   │   ├── difficulty_stratification.py
│   │   ├── ... (10 more)
│   │   └── backport_comparison.py
│   └── results/                   #   Per-analyzer results (not in git)
│
├── memory/                        # Stage 3: Memory System
│   ├── build.py                   #   Build all memory artifacts
│   ├── schemas.py                 #   Data models
│   ├── store.py                   #   Persistence layer
│   ├── retrieve.py                #   MemoryRetriever API
│   ├── embeddings.py              #   sentence-transformers
│   ├── context.py                 #   Context formatter for prompts
│   ├── trajectories.py            #   v1→v2→...→vN chains
│   ├── rules.py                   #   Distilled rule extraction
│   ├── split.py                   #   Train/eval split
│   ├── evaluate.py                #   Evaluation pipeline
│   ├── review.py                  #   Review prediction (Claude CLI)
│   ├── download.py                #   Download pre-built from HF
│   ├── agent_prompt.md            #   Agent system prompt
│   ├── knowledge/                 #   Git-tracked knowledge (212 KB)
│   │   ├── pattern_memory.json
│   │   ├── distilled_rules.json
│   │   └── pattern_knowledge.md
│   └── data/                      #   Built artifacts (not in git)
│       ├── instance_memory.jsonl  #     46 MB
│       ├── pattern_memory.json    #     84 KB
│       ├── distilled_rules.json   #     109 KB
│       ├── faiss_crash.index      #     15 MB
│       ├── faiss_patch.index      #     15 MB
│       ├── trajectories.jsonl     #     3.7 MB
│       ├── split.json
│       └── eval_results.json
│
├── docs/                          # Documentation
│   ├── architecture.md            #   This file
│   ├── collection.md              #   Crawl pipeline reference
│   ├── reproducing.md             #   Quick-start with HF data
│   ├── training.md                #   Fine-tuning guide
│   ├── analysis.md                #   Analyzer reference
│   ├── memory.md                  #   Memory system guide
│   └── exploring.md               #   Dataset explorer guide
│
└── paper/                         # Research paper
    ├── main.tex
    ├── main.pdf
    └── Makefile
```

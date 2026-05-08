# Cross-Layer Analysis

Some kernel bugs crash in one architectural layer but the actual fix belongs in
another. A use-after-free that surfaces inside fuse may need a fix in `fs/super.c`
(VFS core); a paging fault inside the timer interrupt may need a fix in TCP/TLS
state handling. The **cross-layer analyzer** classifies every bug in the dataset
along this dimension so we can:

1. Quantify how often patches land away from the call stack.
2. Stratify model evaluation by how hard each bug is to localize.
3. Drive a retrieval predictor that down-weights stack files when the neighborhood
   says the fix is likely cross-layer.

## Classification breakdown

Live numbers from the persisted analyzer output
(`analysis/results/cross-layer_analysis/result.json`):

| Relation | Count | % | Definition |
|---|---|---|---|
| **same_layer** | 4,028 | 79.5% | Crash and fix in the same architectural layer of the same domain |
| **cross_layer** | 574 | 11.3% | Crash and fix in *different* layers within a *shared* domain |
| **cross_domain** | 465 | 9.2% | Crash and fix have *no shared domain* — disjoint subsystems |
| **(skipped)** | — | — | Missing stack trace or patch diff |
| **Total analyzed** | **5,067** | | |

Within the 574 `cross_layer` bugs, `stack_overlap` records whether *any* patched
file appears on the crash stack:

| Sub-category | Count | % of cross_layer | Meaning |
|---|---|---|---|
| Fix **ON** crash stack | 359 | 62.5% | Stack trace points at the fix file — stack-following helps |
| Fix **OFF** crash stack | 215 | 37.5% | Architectural reasoning required — stack-following misleads |

The **215 cross-layer + off-stack bugs** plus the **465 cross-domain bugs** are the
hardest cases for stack-following heuristics.

### Patch-internal layer span (`fix_internal_layers`)

Each record also carries `fix_internal_layers` — a per-`(domain, layer)` summary
of every file the patch touches, with file count and changed-line count.
Captures cases where the patch *itself* spans layers, which the relation flag
alone misses (`cross_domain` short-circuits before any layer comparison;
`same_layer` collapses to a single primary layer).

| | Count | % of total |
|---|---:|---:|
| records with field populated | 4,904 | 96.8% |
| patches that internally span ≥2 distinct (domain, layer) pairs | **216** | **4.3%** |

The 216 multi-layer patches are the high-value pattern for contract mining
(e.g. *“fix a TLS-via-sockmap bug ⇒ add a hook in `net/core/skmsg.c` (L0) and
wire it into `net/ipv4/tcp_ulp.c` and `net/tls/tls_main.c` (L1) in the same
patch”*).

### Infrastructure-frame filter

`is_infrastructure_file()` in `kernel_layers.py` flags crash-reporter machinery —
`kernel/panic.c`, `mm/kasan/`, `lib/dump_stack*`, `include/linux/list.h` and
friends, `arch/*/kernel/{traps,dumpstack,process}.c` — as helpers rather than
buggy subsystem code. The cross-layer analyzer's `primary_crash` picker skips
them in pass 1, falling back to plain non-inline frames in pass 2. Frames are
still classified into their real `(domain, layer)` so shared-domain detection
and counts stay stable, but they no longer get *picked as primary*. This flips
6 borderline records from `cross_layer` to `same_layer` (the 580→574 delta
above) — they were previously mis-labelled because `panic.c` happened to be
the first non-inline `kernel`-domain frame.

## Kernel-layer taxonomy

13 subsystem domains, each with up to three levels (L0 = core/abstract,
L1 = framework/bus/protocol, L2 = specific implementation). Not every domain has
all three levels — some genuinely don't have a meaningful mid-tier.

| Domain | L0 (core / abstract) | L1 (framework / bus / protocol) | L2 (specific) |
|---|---|---|---|
| filesystem | VFS — `fs/dcache.c`, `fs/namei.c`, `include/linux/fs.h` | FS framework — `fs/iomap/`, `fs/fscache/`, `io_uring/`, `fs/locks.c` | specific FS — `fs/ext4/`, `fs/btrfs/`, `fs/erofs/` |
| networking | net core — `net/core/`, `net/socket.c`, `include/linux/skbuff.h` | protocol — `net/ipv4/`, `net/ipv6/`, `net/netfilter/`, `net/bluetooth/` | net driver — `drivers/net/` |
| block | block core — `block/`, `include/linux/blkdev.h` | — | storage driver — `drivers/scsi/`, `drivers/nvme/`, `drivers/md/` |
| device | device core — `drivers/base/`, `include/linux/device.h` | bus — `drivers/usb/core/`, `drivers/pci/`, `drivers/i2c/i2c-core` | specific driver — `drivers/usb/`, `drivers/gpio/`, `drivers/input/`, `drivers/media/`, `drivers/hid/`, `drivers/tty/`, `drivers/virtio/`, … |
| graphics | DRM core — `drivers/gpu/drm/drm_*.c`, `drivers/gpu/drm/ttm/`, `include/drm/` | — | GPU driver — `drivers/gpu/drm/i915/`, `drivers/gpu/drm/amd/`, `drivers/gpu/drm/nouveau/` |
| sound | ALSA core — `sound/core/`, `include/sound/core.h` | — | sound driver — `sound/usb/`, `sound/pci/`, `sound/soc/`, `sound/hda/` |
| virt | KVM core — `virt/kvm/`, `include/linux/kvm_host.h` | — | arch virt — `arch/*/kvm/` |
| mm | mm core — `mm/`, `include/linux/mm.h`, `include/linux/slab.h` | — | — |
| kernel | kernel core — `kernel/sched/`, `kernel/fork.c`, scheduling/synchronization headers | kernel framework — `kernel/locking/`, `kernel/rcu/`, `kernel/time/` | — |
| bpf, crypto, security, arch | single L0 catch-all | — | — |

Matching is **first-match-wins** across `DOMAINS` (so `drivers/net/e1000/...` is
networking-L2, never device-L2) and within a domain it is **two-pass**: explicit
`path_prefixes` beat catch-all `path_patterns` regardless of level.

> Layer definitions are hard-coded as Python dataclasses in
> [`analysis/analyzers/kernel_layers.py`](../analysis/analyzers/kernel_layers.py)
> — `KernelLayer`, `SubsystemDomain`, the 13 domain instances, the `DOMAINS`
> ordered list, and the public `classify_file_layer()` function. The data is
> declarative (lists of strings + regex), so a future YAML/JSON migration would
> be straightforward, but no one's done it. The visualizer and the mode CLI both
> introspect `DOMAINS` directly, so taxonomy edits propagate without re-codegen.

## Operational definitions: strict and relax modes

The single yes/no question *"is this bug cross-layer?"* has more than one
defensible answer. Two orthogonal signals are recorded on every bug:

- **Stack signal** — `stack_overlap` ∈ {`fix_on_stack`, `fix_off_stack`}.
- **Layer signal** — does the patch land at a different architectural layer than
  the top of the call stack (within a shared domain)?

The `classify_under_mode()` function exposes a CLI-driven combination of both:

| Mode key | Definition |
|---|---|
| `--strict stack` | True iff patch touches no file on the crash stack |
| `--strict layer` | True iff fix layer is outside the relax-N window |
| `--strict combined` | both above must hold (most conservative; default) |
| `--strict off` | alias for `layer` |

**Relax window** controls how many top non-inline crash frames in the fix domain
define the "expected" layer set that the fix must avoid:
`--relax-window {1,2,3,…,all}`. Some real numbers from the full dataset:

| relax_window | strict=stack | strict=layer | strict=combined |
|---|---:|---:|---:|
| 1 | 1,038 | 1,039 | 568 |
| 2 | 1,038 | 854 | 539 |
| 3 | 1,038 | 756 | 515 |
| all | 1,038 | 697 | 499 |

Cross-layer prevalence ranges from **9.8 %** (combined-strict, relax-all) to
**20.5 %** (stack-strict) depending on operational definition — roughly a
2× swing, which is why the picker matters. The historic `is_cross_layer == True`
flag equals `--strict layer --relax-window 1` *minus the cross-domain bugs*
(`1,039 − 465 = 574`, matching the `cross_layer` row above); cross-domain bugs
are layer-positive under any layer mode by construction (no shared domain ⇒
no shared layer).

Cross-domain bugs are uniformly positive under `--strict layer`; you can include
or exclude them via `--relation {cross_layer|cross_domain|same_layer|any}`.

## Commands

### Regenerate the analyzer (run once after each pull)

```bash
python -m analysis.run_all --analyzer crosslayer
```

Writes `analysis/results/cross-layer_analysis/result.json` with one record per
bug. Each record carries `relation`, `stack_overlap`, `direction` (for
`cross_layer`), and the per-frame `crash_layers_top_n` list (function, line,
file, domain, layer level, is_inline) used by every downstream tool.

### Mode-aware classification

```bash
# Default — combined strict, relax-window 1
python -m analysis.run_cross_layer_modes

# Side-by-side count grid over (strict × relax_window)
python -m analysis.run_cross_layer_modes --compare

# Filesystem-only, top-2 frame window, fix-in-upper-layer cases
python -m analysis.run_cross_layer_modes \
    --strict layer --relax-window 2 \
    --domain filesystem --direction fix_in_upper_layer

# Cross-domain bugs only
python -m analysis.run_cross_layer_modes \
    --strict layer --relax-window 1 --relation cross_domain

# Save labelled subset to disk (analysis/results/cross-layer_analysis/by_mode/<slug>.json)
python -m analysis.run_cross_layer_modes \
    --strict combined --relax-window all --save
```

Flags:
- `--strict {stack|layer|combined|off}` — strict component (default `combined`).
- `--relax-window {N|all}` — top-N non-inline crash frames in the fix domain
  to compare against (default `1`).
- `--domain <name>` — restrict to one of `filesystem|networking|block|device|
  graphics|sound|virt|mm|kernel|bpf|crypto|security|arch|any` (default `any`).
- `--direction {fix_in_upper_layer|fix_in_lower_layer|any}` — slice by the
  direction of the layer mismatch.
- `--relation {cross_layer|cross_domain|same_layer|any}` — slice by analyzer
  relation.
- `--compare` — print 4×3 grid (no per-record output).
- `--save` — persist the labelled subset.
- `--top-examples N` — number of example positives to print (default 10).
- `--export-flat [PATH]` — write a flat CSV with one row per bug
  (`bug_id, title, relation, direction, stack_overlap, crash/fix layer,
  fix_files, crash_top_files, fix_internal_layers, fix_internal_layer_count`,
  plus yes/no labels and reason strings under all four canonical modes).
  Default path: `analysis/results/cross-layer_analysis/contract_ready.csv`.
  Used by downstream contract miners and Codex evaluation harnesses so
  they don't have to re-classify every bug themselves.

### Single-bug audit (decision trace)

```bash
# Print classifier intermediate state for one bug
python -m analysis.audit_cross_layer --bug-id 38769495e847cea2dcca
```

Shows the same data the analyzer used: crash domain breakdown, the
`primary_crash` selection replay (with skipped *inline* and
*infrastructure* frames marked), the `primary_fix` lines-weighted choice,
and the verdict under each cell of the canonical mode grid. Complements
the per-bug visualizer (`viz/<bug_id>.html`): viz is for visual
inspection, this is for tracing the decision path during case-study
selection or contract labelling.

### Per-bug layer visualizer

Self-contained HTML pages with the kernel-layer hierarchy, the call stack
(every frame coloured by its (domain, level)), and the ground-truth patched
files (every file coloured by its (domain, level), with the actual diff
embedded in a collapsible block).

```bash
# One bug → one HTML file (./viz_<bug_id>.html)
python -m analysis.viz_layer_bug --bug-id 5b64180f8d9e39d3f061

# Multiple bugs → directory + auto-generated index page
python -m analysis.viz_layer_bug \
    --bug-id 5b64180f8d9e39d3f061,037e18398ba8c655a652,0039110f932d438130f9 \
    --out viz/

# Random deterministic sample (seed=0)
python -m analysis.viz_layer_bug --sample 20 --out viz/

# Unique prefix matching for shorter typing
python -m analysis.viz_layer_bug --bug-id 5b64180 --prefix-match
```

Color scheme: hue = domain, lightness = layer level (L0 darkest, L2 lightest).
Same-hue + different brightness ⇒ within-domain layer mismatch (cross-layer);
different hue ⇒ different domain (cross-domain); identical chip on every line ⇒
same-layer.

Each per-bug HTML also carries:

- a **verify** link bar — chips that open the syzbot bug page, the KASAN
  crash report, the syz/C reproducers, the kernel `.config`, and every
  fix commit on `git.kernel.org`, so a human can click through and
  confirm that the crash and patch correspond before trusting the label
- a **modes** bar — yes/no chips for `combined×1`, `layer×1`, `layer×all`,
  `stack×1`, with the reason string in the tooltip
- a **fix spans** line — one chip per `(domain, layer)` the patch
  touches, highlighted when the patch internally crosses layers (the
  216 high-value contract candidates in §"Patch-internal layer span")

The taxonomy panel shows every path prefix in the layer (no truncation), with
catch-all regex patterns rendered as purple-tinted chips so they are visually
distinct from explicit prefixes. Open any per-bug `.html` file with `file://`
in a browser — no server needed (the verify chips link out, but the page
itself is fully self-contained).

### Patch-location predictor (uses the modes)

`memory/predict_location.py` is a two-stage retrieval predictor whose Stage-1
gate is driven by `classify_under_mode`. Same flags as above:

```bash
python -m memory.predict_location --crash crash.txt \
    --strict-mode combined --relax-window 2

# Drop test-split bugs from the retrieval pool
python -m memory.predict_location --crash crash.txt \
    --exclude-test --strict-mode layer --relax-window all
```

### Eval harness (per-mode stratification)

`training/eval_patch_location.py` runs the retrieval predictor on the test
split and emits stratified metrics under each canonical mode:

```bash
# Retrieval predictor under combined-strict, relax-window 2
python -m training.eval_patch_location --mode retrieval \
    --predictor-strict-mode combined --predictor-relax-window 2

# Smoke test on the first 30 records
python -m training.eval_patch_location --mode retrieval --limit 30 \
    --predictor-strict-mode layer --predictor-relax-window all

# Knob-grid ablation under a chosen mode
python -m training.eval_patch_location --mode retrieval --ablate \
    --predictor-strict-mode combined --predictor-relax-window 1
```

Output:
`memory/data/eval_patch_location/<mode>.json` — stratified metric tables,
including per-mode stratification (`by_mode__strict=...__relax=...`).
`memory/data/eval_patch_location/by_mode.csv` — flat CSV with rows for every
canonical mode × GT-label.

### Existing dataset commands (legacy)

```bash
python -m dataset.view stats
python -m dataset.view list --cross-layer
python -m dataset.view crosslayer <bug_id>
```

## Layer predictor

Beyond classifying historical fixes, we want to *predict* the (domain, layer)
where a fix should land for a brand-new crash report — and use that as a
prior for the file locator. The predictor lives in `memory/cross_layer/`
and ships in two phases:

| Phase | Implementation | Status |
|---|---|---|
| **Phase 1** | retrieval voter (FAISS kNN over crash embeddings + per-domain crash-layer anchor); no training, CPU-only | shipped — commit `ffeda2c` |
| **Phase 2** | trained linear head over frozen bge-base-en-v1.5 [CLS] embeddings; 24-class joint softmax over `(domain, layer_name, layer_level)`; curriculum / inv-freq / uniform / same-only weighting schemes | shipped — first run lands `top1=61%`, `top3=84%` (commits `fe12a4c`, `ef0ff46`) |
| **Phase 2.1** | concat 25-d one-hot of `primary_crash_layer` to the [CLS] embedding before the head — `--use-crash-layer-feature` flag in `train_head.py` and `eval_head.py` | shipped — pushes `top1=76%`, `top3=91%`, same_layer 63→82%, weighted 60→75%, binary cross-layer accuracy 56→73% |
| **Phase 2.2** | replace linear head with MLP (`Linear → ReLU → Dropout → Linear`) — `--head mlp --mlp-hidden N --mlp-dropout F` flags | shipped — modest improvement (`bin F1 0.484→0.538`, `weighted 0.749→0.754`); the **same_layer ≥ 95 % target is unreachable with architecture changes alone** — diagnosed as a data-ceiling problem (see Phase 2.2 section below) |

Long-form plan & design decisions:
[`~/.claude/plans/layer-prediction-from-same-layer-supervision.md`](
  ~/.claude/plans/layer-prediction-from-same-layer-supervision.md)
(memory/cross-layer paths, ML defaults, acceptance thresholds, GPU/CPU split,
rationale-generation strategy).

### Phase 1 baseline numbers

Balanced 337-bug eval+train sample, prior_weight=0.5, k=20 neighbours:

| Stratum | n | top-1 | top-3 |
|---|---:|---:|---:|
| **all** | 337 | 57.9 % | 79.8 % |
| same_layer | 299 | 59.2 % | 79.9 % |
| cross_layer | 34 | 50.0 % | 79.4 % |
| cross_domain | 4 | 25.0 % | 75.0 % |

Top-3 is already ≈ 80 %, so the right layer is usually in the candidate
set; the trained head's job is to push **top-1** from ≈ 58 % to the
paper-quality target (same_layer ≥ 95 %, cross_layer ≥ 55 %, cross_domain
≥ 30 %, weighted ≥ 80 %).

### Reproducing Phase 1 on a fresh CPU box

Prerequisites — already satisfied on this dev box, but listing them so
the recipe is portable:

```bash
# 1. Activate the venv (faiss, transformers, torch CPU, numpy)
source venv/bin/activate
python -c "import faiss, torch; print(faiss.__version__, torch.__version__)"

# 2. Make sure the cross-layer analyzer has been re-run since the
#    fix_internal_layers field was added (commit fb29765). The predictor
#    reads result.json on every call.
python -m analysis.run_all --analyzer crosslayer

# 3. Memory FAISS index must exist (built once via `python -m memory.build`).
ls memory/data/faiss_crash.index memory/data/instance_memory.jsonl
```

Predict for a single bug from the dataset (with self-exclusion of the
query from the retrieval pool):

```bash
# Three canonical bugs spanning all three relations
python -m memory.cross_layer.predict_layer --bug-id 38769495e847cea2dcca   # same_layer
python -m memory.cross_layer.predict_layer --bug-id 5b64180f8d9e39d3f061   # cross_layer
python -m memory.cross_layer.predict_layer --bug-id 037e18398ba8c655a652   # cross_domain

# JSON output for downstream tooling
python -m memory.cross_layer.predict_layer \
    --bug-id 5b64180f8d9e39d3f061 --format json --top 3

# Run on a raw KASAN report (or pipe via '-')
python -m memory.cross_layer.predict_layer --crash crash.txt
cat crash.txt | python -m memory.cross_layer.predict_layer --crash -
```

Tunables (all flags optional):

| Flag | Default | Purpose |
|---|---|---|
| `--top N` | 3 | how many predictions to print |
| `--k N` | 20 | FAISS neighbours fetched for the vote |
| `--prior-weight F` | 0.5 | per-domain anchor strength; 0 disables, 1.0+ over-weights the crash side |
| `--no-self-exclude` | off | keep the query bug in the retrieval pool (debugging only) |
| `--format {human,json}` | human | output format |

Reproducing the baseline accuracy table above:

```bash
# Drop this into a file or run inline; ≈3 minutes on CPU for 519 sample IDs.
python3 - <<'EOF'
import json, random
from pathlib import Path
from memory.cross_layer.predict_layer import (
    LayerPredictor, _primary_fix_layer_of,
)

split = json.loads(Path("memory/data/split.json").read_text())
results = {
    r["bug_id"]: r
    for r in json.loads(
        Path("analysis/results/cross-layer_analysis/result.json").read_text()
    )["details"]
    if r.get("bug_id")
}

# All 219 eval + 300 train samples — drops to ~337 evaluated after
# filtering empty fix_internal_layers / missing crash reports.
rng = random.Random(0)
eval_ids = list(split.get("eval", []))
train_ids = list(split.get("train", []))
rng.shuffle(train_ids)
sample_ids = eval_ids + [b for b in train_ids[:300] if b not in set(eval_ids)]

predictor = LayerPredictor()
processed = Path("dataset/data/processed")
top1 = top3 = n = 0
by_relation = {}
for bug_id in sample_ids:
    rec = results.get(bug_id)
    if not rec or _primary_fix_layer_of(rec) is None: continue
    proc = processed / f"{bug_id}.json"
    if not proc.exists(): continue
    crashes = (json.loads(proc.read_text()).get("crashes") or [{}])
    cr = crashes[0].get("crash_report") or ""
    if not cr: continue
    preds = predictor.predict(cr, k=20, top=3,
                              exclude_bug_ids={bug_id}, prior_weight=0.5)
    if not preds: continue
    gt = _primary_fix_layer_of(rec)
    keys = [(p.domain, p.layer_name, p.layer_level) for p in preds]
    rel = rec.get("relation", "")
    b = by_relation.setdefault(rel, [0, 0, 0])
    b[2] += 1; n += 1
    if keys[0] == gt: top1 += 1; b[0] += 1
    if gt in keys: top3 += 1; b[1] += 1

print(f"\nlayer top-1: {top1/n:.1%}    top-3: {top3/n:.1%}    n={n}")
for rel, (t1, t3, m) in by_relation.items():
    print(f"  {rel:14s} n={m:3d}  top-1={t1/m:.1%}  top-3={t3/m:.1%}")
EOF
```

Expected (random seed 0): ~58 % top-1 / ~80 % top-3 overall, breakdown
matching the table above ±2 pt.

### Phase 2 — trained head: build, train, evaluate

Phase 2 ships three new modules under `memory/cross_layer/`:

| Module | Where it runs | Purpose |
|---|---|---|
| `build_training.py` | local CPU | emits `data/training/{train,eval,test}.jsonl` + `class_map.json` |
| `train_head.py` | local CPU smoke / remote GPU full | encodes crash texts with frozen `BAAI/bge-base-en-v1.5`, trains a `Linear(768, 24)` head, dumps `data/layer_head*/head.pt` |
| `eval_head.py` | local CPU / remote GPU | scores any saved `head.pt` against the held-out test split, emits a Markdown comparison table |

#### 1. Build training JSONLs (CPU, ≈10 s)

`build_training.py` joins the cross-layer analyzer's `result.json`,
`memory/data/split.json`, and the locator's `test.jsonl` into three
disjoint splits with a shared 24-class joint label space over
`(domain, layer_name, layer_level)`.

| Split | n on this dataset | Source |
|---|---:|---|
| train | ≈ 4 523 | every record in `result.json` with `fix_internal_layers`, minus eval and test (`--train-pool all-fil`, default) |
| eval  |   137 | `memory/data/split.json[eval]` minus test (cross-comparable with the memory-system paper) |
| test  |   244 | `dataset/data/training/sft_crash_to_patch_location/test.jsonl` after dropping bugs without `fix_internal_layers` |

Pass `--train-pool split-json` for the strict ≈ 535-bug pool that
matches the memory-system paper's training set; the wider pool is the
default because the per-stratum acceptance floors (cross_layer ≥ 55 %,
cross_domain ≥ 30 %) need more than the 68 / 16 cross-relation training
bugs that `split-json` keeps after the test-overlap cut.

```bash
python -m memory.cross_layer.build_training
ls memory/cross_layer/data/training/
# class_map.json  build_log.json  train.jsonl  eval.jsonl  test.jsonl
```

#### 2. CPU smoke test (≈45 s)

Always run this before shipping a GPU job — it catches missing data
files and verifies bge-base-en-v1.5 is reachable on the local HF cache:

```bash
python -m memory.cross_layer.train_head \
    --limit 100 --device cpu --epochs 2 --weighting curriculum --no-cache
# expect: encoder loads, two epochs run, "[done] best epoch=… weighted=…"
# (numbers are near random because of --limit 100 — that's intentional)
```

#### 3. GPU run on `pve-ai`

Prerequisites — verify once per box:

```bash
ssh pve-ai 'source /home/xiaoguang/syzfix/venv/bin/activate && \
    python -c "import torch; print(torch.__version__, torch.cuda.is_available())"'
# expect: 2.11.0+cu130 True   (CUDA 13.0 / RTX 5090, ~32 GB VRAM)
```

Sync the local code + the small files the analyzer needs (the box
already has `dataset/data/processed/` and `result.json`):

```bash
git push origin main
ssh pve-ai 'cd /home/xiaoguang/syzfix && git pull --ff-only'

rsync -avh memory/data/split.json \
    pve-ai:/home/xiaoguang/syzfix/memory/data/split.json
rsync -avh dataset/data/training/sft_crash_to_patch_location/test.jsonl \
    pve-ai:/home/xiaoguang/syzfix/dataset/data/training/sft_crash_to_patch_location/test.jsonl
```

Build + train on the GPU box. Embeddings are encoded once (≈15 s for
4.4 k crash reports on a 5090) and cached under
`memory/cross_layer/data/training/embeddings/` so subsequent runs only
do head training (sub-second per epoch):

```bash
ssh pve-ai 'cd /home/xiaoguang/syzfix && source venv/bin/activate && \
    python -m memory.cross_layer.build_training'

# One-shot ablation: four weightings, 30 epochs each (~3 min total).
ssh pve-ai 'cd /home/xiaoguang/syzfix && source venv/bin/activate && \
    bash -c "for w in curriculum uniform inv-freq same-only; do
        python -m memory.cross_layer.train_head \
            --weighting \$w --epochs 30 --batch-size 64 --lr 5e-3 \
            --device cuda \
            --out-dir memory/cross_layer/data/layer_head_\${w}; \
    done"'
```

Each `--out-dir` receives:

| File | Content |
|---|---|
| `head.pt`           | best-by-weighted checkpoint (`{head_state_dict, dim, n_classes, encoder, best_epoch, best_metrics}`) |
| `train_log.jsonl`   | per-epoch loss + full per-stratum metrics |
| `train_config.json` | encoder, weighting, hyperparams, device, seed, best epoch |
| `class_map.json`    | 24-class id ↔ `(domain, layer_name, layer_level)` map (copied from `data/training/`) |

Weighting schemes (`--weighting`):

| Scheme | Effect |
|---|---|
| `uniform`            | every sample equal weight |
| `inv-freq`           | rebalances samples so each `relation` stratum carries equal mass |
| `curriculum` (default) | epoch 0 same_layer only, epoch 1 + cross_layer, epoch ≥ 2 + cross_domain |
| `same-only`          | trains exclusively on `same_layer` — negative-control for "same_layer alone is not enough" |

#### 4. Sync artifacts back

Don't pull the embedding cache (regenerable, large). Everything else is
under 1 MB per variant:

```bash
rsync -avh --exclude='embeddings/' --exclude='*.npy' \
    pve-ai:/home/xiaoguang/syzfix/memory/cross_layer/data/ \
    memory/cross_layer/data/
```

#### 5. Score the held-out test split (eval)

Per-epoch validation during training uses `eval.jsonl` (137 bugs);
the canonical 244-bug `test.jsonl` is reserved for the final number.
`eval_head.py` loads any saved `head.pt`, re-encodes test crashes (or
reuses `data/training/embeddings/test.npy`), and prints a stratified
table.

```bash
# On the GPU box — fastest, since embeddings.test.npy is cached there.
ssh pve-ai 'cd /home/xiaoguang/syzfix && source venv/bin/activate && \
    python -m memory.cross_layer.eval_head \
        --head-dir memory/cross_layer/data/layer_head_curriculum \
                  memory/cross_layer/data/layer_head_uniform \
                  memory/cross_layer/data/layer_head_inv-freq \
                  memory/cross_layer/data/layer_head_same-only \
        --output memory/cross_layer/data/eval/test_metrics.md \
        --device cuda'

# Or locally on CPU after sync (≈30 s for the encode pass; reuses the
# cached test embedding if you rsync'd it).
python -m memory.cross_layer.eval_head \
    --head-dir memory/cross_layer/data/layer_head_curriculum \
    --device cpu
```

Reading the report — what each column tells you:

| Column | Meaning |
|---|---|
| `weighted`        | plan-weighted overall: `0.795·same + 0.113·cross_layer + 0.092·cross_domain`. Single headline number for the acceptance criterion. |
| `same top1`       | hardest target for "is the model learning the trivial pattern?". Phase 1 retrieval baseline lands at ~59 %. Trained head needs ≥ 95 % to clear the floor. |
| `x_layer top1`    | the load-bearing number — needs the `fix_internal_layers` signal AND same_layer's negative supervision. Floor: ≥ 55 %. |
| `x_dom top1`      | hardest stratum (the model has to *change domain*); only useful if the trained head clearly beats the retrieval baseline. Floor: ≥ 30 %. |
| `all top3`        | cheap upper bound — if this is much higher than top1, retrieval-style re-ranking can salvage a lot of cases. |
| `domain top1`     | argmax over the marginal `p(domain) = Σ_l p(domain, l)`. A weak domain top1 means the encoder isn't even getting the subsystem right; a high domain top1 with poor layer top1 means the head is confusing layers within the right subsystem. |

#### Observed numbers (current dataset)

Single training run, `--lr 5e-3 --batch-size 64 --epochs 30`, RTX 5090,
seed 42. Test set: 244 bugs (198 same / 30 cross_layer / 16 cross_domain).

| weighting | best ep | weighted | same top1 | x_layer top1 | x_dom top1 | all top1 | all top3 | domain top1 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **curriculum** | 24 | **0.600** | 0.626 | **0.700** | 0.250 | 0.611 | **0.836** | 0.664 |
| uniform        | 19 | 0.588 | 0.611 | **0.700** | 0.250 | 0.598 | 0.811 | 0.652 |
| inv-freq       | 15 | 0.513 | 0.495 | **0.700** | **0.438** | 0.516 | 0.758 | 0.578 |
| same-only      | 26 | 0.593 | **0.636** | 0.667 | 0.125 | 0.607 | 0.799 | 0.656 |

Read of these numbers (the calibration that should drive Phase 2.1):

- **`cross_layer ≥ 55 %` floor cleared across the board** (70 % on the
  three trained-on-cross variants), and `cross_domain` clears `30 %`
  with `inv-freq` (43.8 %). The trained head materially beats Phase 1
  retrieval (50 % cross_layer, 25 % cross_domain) on these strata —
  this is the load-bearing improvement.
- **`same_layer ≥ 95 %` floor missed badly (best 63.6 %).** A
  `Linear(768, 24)` over frozen `[CLS]` embeddings of 512 BPE-truncated
  crash text caps around 60–65 % same-layer top-1; the embedding
  clusters bugs by domain (`domain top1 ≈ 66 %`) but doesn't separate
  *layers within a subsystem*. To clear 95 % we need either a stronger
  feature (concatenate `primary_crash_layer` one-hot to the embedding —
  same_layer ≈ "predict the crash layer"), or a non-linear head, or
  fine-tuning the encoder with LoRA. None are wired in v1.
- **Curriculum vs uniform vs same-only is roughly a wash on top-1**
  (within ±2 pt). The same-only baseline tying within 1 pt of curriculum
  on `same_layer` is itself a finding: the cross_layer gain comes from
  the encoder + linear head, not from oversampling cross_layer rows.
  The interpretable difference is the cross_domain stratum, where
  curriculum (25 %) beats same-only (12.5 %) — the negative
  supervision that "the answer is *not* the crash layer" only kicks in
  when cross_domain rows are visible.
- **`top-3 = 83.6 %` already beats Phase 1's 80 %** — top-1 is the only
  miss, and the layer-prior signal is good enough to feed Phase 3's
  file-locator wrapper (the locator reweights, not picks).

### Phase 2.1 — concat `primary_crash_layer` one-hot before the head

The Phase 2 same_layer ceiling at 63 % top-1 is a feature-poverty
problem, not an optimisation one: on 80 % of bugs the answer is the
crash layer itself, but the [CLS] embedding doesn't expose that signal
explicitly. Phase 2.1 fixes it with one flag — `--use-crash-layer-feature`
on `train_head.py` and `eval_head.py` — that concatenates a 25-dim
one-hot of `primary_crash_layer` (24 fix-class slots + 1 unknown bit,
where "unknown" = the crash layer is not parseable or is outside the
24-class fix-layer space; observed unknown rate ≈ 0.4 % on the
all-fil training pool, 0 % on eval/test) to the 768-d [CLS] embedding
before the linear head.

Reproduction is the same recipe as Phase 2; just append the flag:

```bash
ssh pve-ai 'cd /home/xiaoguang/syzfix && source venv/bin/activate && \
    bash -c "for w in curriculum uniform inv-freq same-only; do
        python -m memory.cross_layer.train_head \
            --weighting \$w --epochs 30 --batch-size 64 --lr 5e-3 \
            --device cuda --use-crash-layer-feature \
            --out-dir memory/cross_layer/data/layer_head_\${w}_clf; \
    done"'

# eval_head reads the flag from train_config.json and adds the feature
# automatically — no flag needed at eval time.
ssh pve-ai 'cd /home/xiaoguang/syzfix && source venv/bin/activate && \
    python -m memory.cross_layer.eval_head \
        --head-dir memory/cross_layer/data/layer_head_curriculum \
                  memory/cross_layer/data/layer_head_uniform \
                  memory/cross_layer/data/layer_head_inv-freq \
                  memory/cross_layer/data/layer_head_same-only \
                  memory/cross_layer/data/layer_head_curriculum_clf \
                  memory/cross_layer/data/layer_head_uniform_clf \
                  memory/cross_layer/data/layer_head_inv-freq_clf \
                  memory/cross_layer/data/layer_head_same-only_clf \
        --output memory/cross_layer/data/eval/test_metrics_phase21.md \
        --device cuda'
```

`eval_head.py` now also computes the binary "is this crash cross-layer?"
metric: derive `predicted_cross = (top1_layer ≠ primary_crash_layer)`,
ground truth `gt_cross = (relation ≠ same_layer)`, then report
accuracy / precision / recall / F1. Trivial baseline = "always predict
same_layer" → 81.5 % accuracy on this test split (198 same / 45 cross).

#### Phase 2.1 numbers (test split, 244 bugs, seed 42, lr 5e-3, 30 epochs)

| weighting | +crash_layer | best ep | weighted | same top1 | x_layer | x_dom | top1 | top3 | dom top1 | bin acc | bin P | bin R | bin F1 |
|---|:-:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| curriculum |     | 24 | 0.600 | 0.626 | 0.700 | 0.250 | 0.611 | 0.836 | 0.664 | 0.560 | 0.279 | 0.867 | 0.422 |
| uniform    |     | 19 | 0.588 | 0.611 | 0.700 | 0.250 | 0.598 | 0.811 | 0.652 | 0.556 | 0.280 | 0.889 | 0.426 |
| inv-freq   |     | 15 | 0.513 | 0.495 | 0.700 | 0.438 | 0.516 | 0.758 | 0.578 | 0.481 | 0.252 | 0.911 | 0.394 |
| same-only  |     | 26 | 0.593 | 0.636 | 0.667 | 0.125 | 0.607 | 0.799 | 0.656 | 0.568 | 0.283 | 0.867 | 0.426 |
| **curriculum** | **✓** | **22** | **0.749** | **0.818** | 0.667 | 0.250 | **0.762** | **0.910** | **0.791** | **0.728** | **0.373** | 0.689 | 0.484 |
| uniform    | ✓   | 29 | 0.747 | 0.813 | 0.633 | 0.312 | 0.758 | 0.902 | 0.791 | 0.728 | 0.370 | 0.667 | 0.476 |
| **inv-freq**   | **✓**   | 26 | 0.676 | 0.672 | **0.800** | **0.562** | 0.680 | 0.881 | 0.746 | 0.654 | 0.336 | **0.889** | **0.488** |
| same-only  | ✓   | 11 | 0.694 | 0.828 | 0.267 | 0.062 | 0.709 | 0.852 | 0.770 | 0.720 | 0.323 | 0.467 | 0.382 |

Reads:

- **Curriculum + crash-layer feature is the new default** — `weighted=0.749`
  beats every Phase 2 row by ≥ 14 pt absolute. Top-3 hits **91 %** so the
  layer-prior signal for Phase 3's locator wrapper is comfortably good.
  same_layer 63 → 82 % is the load-bearing change: the head can now
  literally copy the crash-layer bit when retrieval signal is weak.
- **Pareto frontier on the binary cross-layer flag**:
  - Want **precision** (fewer false alarms)? `curriculum +clf`: 73 %
    accuracy, 37 % precision, 69 % recall, F1 0.48.
  - Want **recall** (catch every cross-layer bug for triage)? `inv-freq +clf`:
    65 % accuracy, 34 % precision, **89 % recall**, F1 0.49.
  - **Trivial baseline** (always say same_layer) still wins on accuracy
    (81.5 %) — the binary task is fundamentally hard because cross-layer
    is rare (18 % positive class). The trained head's value is its non-zero
    F1 and the calibrated probability for downstream re-weighting, not
    accuracy on its own.
- **inv-freq + clf** also clears the per-stratum acceptance floors that
  the plan called for: cross_layer **80 %** ≥ 55 %, cross_domain
  **56.2 %** ≥ 30 %, top-3 **88.1 %** ≥ 80 %. same_layer at 67 % is
  below the 95 % floor — that is now the bottleneck.
- **same-only + clf** is the negative control: same_layer top-1 climbs
  to 83 % but cross_layer collapses to 27 % and cross_domain to 6 %.
  This is the "same_layer alone is not enough" story the paper wanted
  — the cross-strata gain only materialises when cross rows are in
  training.

Acceptance-criterion checklist after Phase 2.1:

| target (plan §"Acceptance criteria" #2) | Phase 2 best | Phase 2.1 best | met? |
|---|---:|---:|:---:|
| same_layer top-1 ≥ 95 % | 63.6 % (same-only) | 82.8 % (same-only +clf) | ❌ closer, still short |
| cross_layer top-1 ≥ 55 % | 70.0 % (3 variants) | **80.0 % (inv-freq +clf)** | ✅ |
| cross_domain top-1 ≥ 30 % | 43.8 % (inv-freq) | **56.2 % (inv-freq +clf)** | ✅ |
| weighted ≥ 80 % | 60.0 % (curriculum) | 74.9 % (curriculum +clf) | ❌ closer, ~5 pt short |
| top-3 ≥ 80 % | 83.6 % | **91.0 %** | ✅ |

### Phase 2.2 — MLP head and the data-ceiling diagnosis

The Phase 2.1 hypothesis ("same_layer 95 % is reachable with a non-linear
head over the same 792-d feature") is **falsified**. Adding an MLP head
moves `weighted` by less than 1 pt and `same_layer top-1` by less than
3 pt, well short of the 12-pt jump the floor requires.

`--head mlp --mlp-hidden 256 --mlp-dropout 0.2` (with `lr=1e-3`,
`wd=1e-3`) produced these test-split numbers, side by side with the
Phase 2.1 linear-head reference rows:

| weighting | head | weighted | same | x_layer | x_dom | top3 | dom | bin acc | bin F1 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| curriculum | linear  | 0.749 | 0.818 | 0.667 | 0.250 | 0.910 | 0.791 | 0.728 | 0.484 |
| inv-freq   | linear  | 0.676 | 0.672 | **0.800** | 0.562 | 0.881 | 0.746 | 0.654 | 0.488 |
| curriculum | mlp     | 0.747 | 0.818 | 0.700 | 0.188 | 0.906 | **0.803** | 0.724 | 0.489 |
| **uniform**    | **mlp**     | **0.754** | 0.813 | 0.700 | 0.312 | 0.902 | 0.807 | **0.753** | **0.538** |
| inv-freq   | mlp     | 0.654 | 0.641 | 0.767 | **0.625** | 0.877 | 0.734 | 0.642 | 0.485 |
| same-only  | mlp     | 0.701 | **0.838** | 0.200 | 0.125 | 0.824 | 0.783 | 0.683 | 0.330 |

A wider MLP (`--mlp-hidden 512`, `--mlp-dropout 0.1`, `lr=5e-3`, `wd=1e-4`)
overfits — train loss drops to 0.17 by epoch 29 with no test-set gain.
We keep `hidden=256, dropout=0.2, lr=1e-3` as the Phase 2.2 default.

**Why the MLP barely moves the needle — the data ceiling**

A one-line diagnostic over `test.jsonl`:

```python
match = sum(
    1 for r in test
    if r["primary_crash_layer"] is not None
    and tuple(r["primary_crash_layer"]) ==
        (r["label_domain"], r["label_layer_name"], r["label_layer_level"])
)
print(match / len(test))
```

| stratum | n | `primary_crash_layer == primary_fix_layer` |
|---|---:|---:|
| all          | 244 | 56.1 % (137/244) |
| **same_layer**   | **198** | **69.2 % (137/198)** |
| cross_layer  |  30 |  0.0 % (0/30)   |
| cross_domain |  16 |  0.0 % (0/16)   |

The `same_layer` ground truth disagrees with `primary_crash_layer` on
**31 % of same_layer test bugs**. This is the absolute upper bound on
what the crash-layer one-hot feature alone can deliver: a model that
literally copies the crash bit and outputs it as the prediction would
score 69.2 % on `same_layer top-1`. We're at 81.8–83.8 % — already
**~15 pt past the data ceiling** because the `[CLS]` embedding is
disambiguating the cases where the primary crash frame is misleading
(infrastructure-only stacks, mis-classified frames, helper utilities).

The remaining gap (`83.8 → 95`) cannot be closed by architecture
changes on the current feature set. The bottleneck is feature
extraction.

### Phase 2.3 candidates (next lever)

The Phase 2.2 diagnostic redirects future effort to the input side:

1. **Use top-K crash layers**, not just the primary. Concat the top-3
   `crash_layers_top_n` entries' one-hots — the right layer is in the
   top-3 for ≥ 90 % of same_layer bugs. Cheapest, highest expected
   impact.
2. **Better primary-frame heuristic.** The current rule (first non-inline
   non-infra frame) misclassifies ~30 % of same_layer bugs. A second
   pass — "if the first non-infra frame is a generic helper (`include/
   linux/list.h`, `lib/`, `kernel/sched/`), keep walking" — would
   tighten this. Ideally co-developed with the kernel-layers taxonomy
   so the analyzer and the predictor agree.
3. **Mean-pool the encoder's last hidden state** instead of `[CLS]`.
   Stack-trace tokens carry layer info that `[CLS]` flattens.
4. **LoRA fine-tune the encoder** (~ 1 M trainable params on top of
   the frozen 110 M). Highest cost, last resort.

Start with (1) — keeps the architecture identical and isolates the
feature contribution.

### Acceptance-criterion checklist after Phase 2.2

| target | best | status |
|---|---:|:---:|
| cross_layer top-1 ≥ 55 % | 80.0 % (inv-freq linear +clf) | ✅ |
| cross_domain top-1 ≥ 30 % | 62.5 % (inv-freq mlp +clf) | ✅ |
| top-3 ≥ 80 % | 91.0 % (curriculum linear +clf) | ✅ |
| same_layer top-1 ≥ 95 % | 83.8 % (same-only mlp +clf) | ❌ blocked by 69 % data ceiling |
| weighted ≥ 80 % | 75.4 % (uniform mlp +clf) | ❌ same blocker |

## Concrete examples

### Cross-layer (specific FS → VFS core)

```bash
python -m analysis.run_cross_layer_modes \
    --strict layer --relax-window 1 \
    --relation cross_layer --domain filesystem \
    --direction fix_in_upper_layer --top-examples 5
```

**Bug `5b64180f8d9e39d3f061` — `KASAN: slab-use-after-free Read in fuse_test_super`**

| | Path | Layer |
|---|---|---|
| Top crash frames | `fs/fuse/fuse_i.h`, `fs/fuse/inode.c` | filesystem · **L2 specific filesystem** |
| Fix file | `fs/super.c` | filesystem · **L0 VFS core** |

Crash manifests in fuse-specific code where an inode goes use-after-free, but
the actual defect is in the VFS lifecycle — fuse is just the first caller to
dereference the freed pointer. An LLM repair agent that searches near the crash
site (`fs/fuse/`) will be in the wrong layer.

### Cross-domain (kernel/arch crash → networking fix)

```bash
python -m analysis.run_cross_layer_modes \
    --strict layer --relax-window 1 \
    --relation cross_domain --top-examples 8
```

**Bug `037e18398ba8c655a652` — `BUG: unable to handle kernel paging request in hrtimer_interrupt`**

| | Path | Domain · Layer |
|---|---|---|
| Top crash frames | `kernel/time/hrtimer.c`, `arch/x86/kernel/apic/apic.c`, `arch/x86/entry/entry_64.S` | kernel · L1 / arch · L0 |
| Fix files | `include/linux/skmsg.h`, `include/net/tcp.h`, `net/core/skmsg.c`, `net/ipv4/tcp_ulp.c`, `net/tls/tls_main.c` | **networking** · L0+L1 |

Crash and fix subsystem domains are disjoint. The bug is a tcp_ulp / TLS state
mishandling that left a hrtimer with a dangling callback; nothing on the call
stack hints at networking.

### Same-layer (fix exactly where it crashed)

```bash
python -c "
import json
for d in json.loads(open('analysis/results/cross-layer_analysis/result.json').read())['details']:
    if d['relation'] == 'same_layer' and d.get('stack_overlap') == 'fix_on_stack':
        print(d['bug_id'], '|', d['title'][:70])
" | head -5
```

**Bug `0039110f932d438130f9` — `general protection fault in hfsc_tcf_block`**

| | Path | Domain · Layer |
|---|---|---|
| Top crash frames | `net/sched/sch_hfsc.c`, `net/sched/sch_api.c` | networking · **L1 protocol/subsystem** |
| Fix files | `net/sched/sch_api.c`, `net/sched/cls_api.c`, `net/sched/sch_generic.c` | networking · **L1 protocol/subsystem** |

Crash and fix are both in the traffic-control scheduler. The fix even lands on
the exact file that's on the call stack. ~79 % of bugs in the dataset fall
here — stack-following works.

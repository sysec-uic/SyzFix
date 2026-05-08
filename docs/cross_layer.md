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
| **Phase 2** | trained linear head over frozen bge-base-en-v1.5 [CLS] embeddings; 24-class joint softmax over `(domain, layer_name, layer_level)`; curriculum / inv-freq / uniform / same-only weighting schemes | scaffolded — `build_training.py` and `train_head.py` runnable; awaiting GPU run |

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

### Phase 2 — trained head (build, smoke, GPU run)

Phase 2 ships two new files under `memory/cross_layer/`:

| File | Where it runs | Purpose |
|---|---|---|
| `build_training.py` | local CPU | emits `data/training/{train,eval,test}.jsonl` + `class_map.json` |
| `train_head.py` | smoke local CPU / full remote GPU | encodes crash texts with frozen bge-base-en-v1.5, trains a linear head, dumps `data/layer_head/head.pt` |

Splits (per `--train-pool` default `all-fil`):

| Split | n | Source |
|---|---:|---|
| train | 4 523 | every record in `result.json` with `fix_internal_layers`, minus eval and test |
| eval  |   137 | `memory/data/split.json[eval]` minus test (cross-comparable with the memory-system paper) |
| test  |   244 | `dataset/data/training/sft_crash_to_patch_location/test.jsonl` (canonical held-out) |

Pass `--train-pool split-json` if you want the strict ≈ 535-bug pool that
matches the memory-system paper's training set; the wider pool is the
default because the per-stratum acceptance floors (cross_layer ≥ 55 %,
cross_domain ≥ 30 %) need more than the 68 / 16 cross-relation training
bugs that `split-json` keeps after the test-overlap cut.

Build the JSONLs (≈10 s):

```bash
python -m memory.cross_layer.build_training
ls memory/cross_layer/data/training/
# class_map.json  build_log.json  train.jsonl  eval.jsonl  test.jsonl
```

Local CPU smoke test of the trainer (≈45 s with bge-base already cached;
must succeed before the GPU run):

```bash
python -m memory.cross_layer.train_head \
    --limit 100 --device cpu --epochs 2 --weighting curriculum --no-cache
# expect: pipeline runs to "[done] best epoch=… weighted=…"; numbers will
# be near random because of --limit 100 — that is intentional.
```

Full GPU run (rsync `memory/cross_layer/` and the JSONLs to the remote
box, then):

```bash
python -m memory.cross_layer.train_head \
    --weighting curriculum --epochs 8 --batch-size 64 --device cuda
# Outputs:
#   memory/cross_layer/data/layer_head/head.pt          (best-by-weighted)
#   memory/cross_layer/data/layer_head/train_log.jsonl
#   memory/cross_layer/data/layer_head/train_config.json
```

Weighting schemes (`--weighting`):

| Scheme | Effect |
|---|---|
| `uniform` | every sample equal weight (default in earlier drafts) |
| `inv-freq` | rebalances samples so each `relation` stratum carries equal mass |
| `curriculum` (default) | epoch 0 trains on `same_layer` only, epoch 1 adds `cross_layer`, epoch ≥ 2 includes `cross_domain` |
| `same-only` | trains exclusively on `same_layer` — the negative-control for "same_layer alone is not enough" |

Per-epoch metrics emitted to `train_log.jsonl` cover `top1` / `top3` /
`domain_top1` per stratum and a plan-weighted overall
(`0.795·same + 0.113·cross_layer + 0.092·cross_domain`) so the GPU box
self-reports whether the run cleared the acceptance floors.

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

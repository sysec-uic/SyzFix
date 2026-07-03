# SyzFix

**A full-lifecycle dataset of fixed Linux kernel bugs**, collected from
[syzbot](https://syzkaller.appspot.com/upstream/fixed) — from the initial crash
report through patch iterations and reviewer discussions to the final merged
commit.

[:simple-huggingface: Dataset on HuggingFace](https://huggingface.co/datasets/xiaoguangwang/syzfix-dataset){ .md-button .md-button--primary }
[:simple-github: Code on GitHub](https://github.com/sysec-uic/syzfix){ .md-button }

---

## The dataset at a glance

| | |
|---|---|
| Fixed kernel bugs collected | **~7,000** |
| … with the merged patch diff | ~5,200 |
| … with full mailing-list discussions | ~5,000 |
| … with a C reproducer | ~2,600 |
| Patch versions captured per bug | up to 9 (v1 → v2 → … → merged) |

Each entry captures one bug end-to-end: the raw crash report (oops / KASAN /
BUG), syzkaller and C reproducers, every patch revision posted to
lore.kernel.org with its inline review discussion, and the final commit merged
into `torvalds/linux`.

```
[Sep 16, 2024]  syzbot: NULL pointer deref in filemap_read_folio
[Sep 17, 2024]  Developer → [PATCH v1]: check S_ISREG before proceeding
[Sep 17, 2024]  syzbot: patch confirmed working ✅
[Sep 17, 2024]  Developer → [PATCH v2]: also fix multi-device/blob case
[Oct 11, 2024]  Chao Yu: Reviewed-by ✅
[Final]         Commit 416a8b2c merged into torvalds/linux
```

---

## Preliminary findings

### Cross-layer bugs are common — and hard

Some kernel bugs crash in one architectural layer but must be fixed in
another (a fuse crash fixed in VFS core; an interrupt-context fault fixed in
TCP/TLS state handling). Classifying all 5,067 analyzable bugs against a
13-domain / 3-level kernel-layer taxonomy:

| Relation | Share | Meaning |
|---|---|---|
| same-layer | 79.5% | fix lands where the crash occurred |
| **cross-layer** | **11.3%** | fix in a different layer of the same domain |
| **cross-domain** | **9.2%** | fix in an entirely different subsystem |

Among cross-layer bugs, **37.5% have the fix completely off the crash
stack** — stack-following heuristics (and stack-following LLM agents)
cannot localize them. [Full analysis →](cross_layer.md)

### Crash reports carry enough signal to predict the fix layer

A frozen-encoder classification head trained on the crash report alone
predicts the *(domain, layer)* where the fix will land:

| Metric (held-out test split) | Score |
|---|---|
| Weighted layer accuracy | 0.78 |
| Top-1 exact layer | 0.80 |
| **Top-3 layer** | **0.94** |

Top-3 covering 94% of bugs means a predicted layer prior can cut the search
space for downstream patch-localization agents by an order of magnitude.

---

## Start in minutes

```bash
git clone https://github.com/sysec-uic/syzfix.git && cd syzfix
python3 -m venv venv && source venv/bin/activate
pip install -e .

# pull the pre-built dataset from HuggingFace (~2 GB download)
python -m dataset.restore_processed --repo xiaoguangwang/syzfix-dataset
python -m dataset.view build-index
python -m dataset.view list
```

See [Reproducing without re-crawling](reproducing.md) for the full guide, or
[Data collection](collection.md) to re-crawl from scratch.

---

## Citation & contact

Developed at [sysec-uic](https://github.com/sysec-uic). If you use SyzFix in
your research, please cite the repository until the accompanying paper is
available. Questions and issues → [GitHub issues](https://github.com/sysec-uic/syzfix/issues).

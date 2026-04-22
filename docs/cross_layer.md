# Cross-Layer Analysis

Some kernel bugs crash in one architectural layer but need to be fixed in another.
For example, a NULL pointer dereference in VFS core might actually require a fix in
a specific filesystem like erofs, or a crash in a network protocol might need a fix
in a net driver.

The **cross-layer analyzer** classifies bugs across 7 kernel domains (filesystem,
networking, block, device, mm, sound, graphics), each with hierarchical layers
(core → framework → specific).

## Dataset breakdown (4,983 bugs with both stack trace and patch)

| Category | Count | % | Description |
|---|---|---|---|
| **Same-layer** | 2,905 | 58.3% | Crash and fix in same architectural layer |
| **Cross-layer** | 633 | 12.7% | Crash and fix in different layers of the same domain |
| **Cross-subsystem** | 1,445 | 29.0% | Crash and fix in entirely different kernel subsystems |

## Stack-overlap verification

Not all "cross-layer" bugs are equally hard. We check whether the fix file appears
**anywhere** on the crash call stack:

| Sub-category | Count | % of cross-layer | Meaning |
|---|---|---|---|
| Fix **ON** crash stack | 443 | 70.0% | Fix file is visible in the stack trace — an LLM can follow it |
| Fix **OFF** crash stack | 190 | 30.0% | Fix file is NOT in the stack trace — requires architectural reasoning |

The **190 true cross-layer bugs** are the hardest cases for LLM-based bug fixing:
the model cannot locate the fix by following the stack trace alone.

### Example: stack-reachable (fix ON stack)

Bug `001306cd9c92ce0df23f` — NULL pointer dereference in `filemap_read_folio`:

```
Crash stack (simplified):
  #2  read_mapping_folio    include/linux/pagemap.h  → VFS core (level 0)
  #3  erofs_bread           fs/erofs/data.c          → specific FS (level 2)
  #4  erofs_read_superblock fs/erofs/super.c         → specific FS (level 2)  ← fix here
  #5  erofs_fc_fill_super   fs/erofs/super.c         → specific FS (level 2)
  #6  vfs_get_super         fs/super.c               → VFS core (level 0)

Fix: fs/erofs/super.c  → specific FS (level 2)
```

The fix file `fs/erofs/super.c` is on the stack at frame #4. The analyzer calls it
cross-layer (VFS core vs. specific FS), but the stack trace leads directly to the fix.

### Example: true cross-layer (fix OFF stack)

Bug `001516d86dbe88862cec` — uninit-value in `__dev_mc_add`:

```
Crash stack (simplified):
  #5  __hw_addr_add_ex      net/core/dev_addr_lists.c  → net core  (level 0)
  #6  __dev_mc_add          net/core/dev_addr_lists.c  → net core  (level 0)
  #8  igmp6_group_added     net/ipv6/mcast.c           → protocol  (level 1)
  ...
  (no drivers/net/ anywhere in the stack)

Fix: drivers/net/tun.c  → net driver (level 2)
```

The fix file `drivers/net/tun.c` does **not** appear anywhere on the crash stack.
The model would need to reason: "the uninitialized value originates from the tun
driver and propagates through the networking stack to the crash site."

## Kernel layer taxonomy

The analyzer maps kernel source paths to 7 subsystem domains, each with 2–3
hierarchical layers:

| Domain | Core (level 0) | Framework (level 1) | Specific (level 2) |
|---|---|---|---|
| **filesystem** | VFS (`fs/*.c`, `include/linux/fs.h`) | FS frameworks (`fs/iomap/`, `fs/crypto/`) | Specific FS (`fs/ext4/`, `fs/btrfs/`, …) |
| **networking** | Net core (`net/core/`, `include/net/sock.h`) | Protocols (`net/ipv4/`, `net/ipv6/`, …) | Net drivers (`drivers/net/`) |
| **block** | Block core (`block/`) | — | Storage drivers (`drivers/scsi/`, `drivers/nvme/`, …) |
| **device** | Device core (`drivers/base/`) | Bus (`drivers/usb/core/`, `drivers/pci/`) | Specific drivers (`drivers/input/`, …) |
| **mm** | MM core (`mm/`) | — | — |
| **sound** | ALSA core (`sound/core/`) | — | Sound drivers (`sound/usb/`, `sound/pci/`, …) |
| **graphics** | DRM core (`drivers/gpu/drm/drm_*`) | — | GPU drivers (`drivers/gpu/drm/i915/`, …) |

See [`analysis/analyzers/kernel_layers.py`](../analysis/analyzers/kernel_layers.py) for the full path mapping.

## Commands

```bash
# Run the cross-layer analyzer (required once)
python3 -m analysis.run_all --analyzer crosslayer

# Quick overview of all statistics
python -m dataset.view stats

# List all 466 cross-layer bugs
python -m dataset.view list --cross-layer

# List cross-layer bugs with on/off-stack status
python -m dataset.view list --cross-layer --verify-stack

# List only the 130 hardest cases (fix NOT on crash stack)
python -m dataset.view list --true-cross-layer

# Filter by domain
python -m dataset.view list --cross-layer --cross-layer-domain filesystem

# Inspect a specific bug's cross-layer + stack-overlap classification
python -m dataset.view crosslayer <bug_id>

# Generate training data for cross-layer classification
cd dataset && python3 prepare_training.py --tasks cross_layer
```

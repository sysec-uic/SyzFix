"""Tests for kernel_layers taxonomy.

The match order in SubsystemDomain.match() must guarantee that explicit
path_prefixes (at any level) beat catch-all path_patterns. Without this,
files like fs/iomap/*, fs/crypto/*, net/core/*, sound/core/* get silently
relabelled to a higher level by a level-2 catch-all regex.
"""

from analysis.analyzers.kernel_layers import classify_file_layer


# (path, expected_domain, expected_layer_name, expected_level)
CASES = [
    # ── filesystem: VFS core (level 0) ──────────────────────────────
    ("fs/namei.c",              "filesystem", "VFS core",            0),
    ("fs/super.c",              "filesystem", "VFS core",            0),
    ("fs/dcache.c",             "filesystem", "VFS core",            0),
    ("include/linux/fs.h",      "filesystem", "VFS core",            0),
    ("include/linux/pagemap.h", "filesystem", "VFS core",            0),

    # ── filesystem: FS framework (level 1) — regression targets ─────
    # These are pre-empted by the level-2 catch-all `^fs/[a-z...]+/`
    # unless match() checks explicit prefixes first.
    ("fs/iomap/buffered-io.c",  "filesystem", "FS framework",        1),
    ("fs/iomap/direct-io.c",    "filesystem", "FS framework",        1),
    ("fs/crypto/keyring.c",     "filesystem", "FS framework",        1),
    ("fs/fscache/cache.c",      "filesystem", "FS framework",        1),
    ("fs/notify/fanotify/fanotify.c", "filesystem", "FS framework",  1),
    ("fs/exportfs/expfs.c",     "filesystem", "FS framework",        1),
    ("fs/nls/nls_base.c",       "filesystem", "FS framework",        1),
    ("fs/unicode/utf8-core.c",  "filesystem", "FS framework",        1),
    # Level-1 specific files (not under a framework prefix) must still work
    ("fs/locks.c",              "filesystem", "FS framework",        1),
    ("fs/io_uring.c",           "filesystem", "FS framework",        1),

    # ── filesystem: specific FS (level 2) — catch-all still works ───
    ("fs/ext4/super.c",         "filesystem", "specific filesystem", 2),
    ("fs/btrfs/inode.c",        "filesystem", "specific filesystem", 2),
    ("fs/erofs/super.c",        "filesystem", "specific filesystem", 2),
    ("fs/xfs/xfs_log.c",        "filesystem", "specific filesystem", 2),
    ("fs/f2fs/dir.c",           "filesystem", "specific filesystem", 2),

    # ── networking: net core (level 0) — regression target ──────────
    # Pre-empted by level-1 catch-all `^net/[a-z...]+/` without the fix.
    ("net/core/dev.c",          "networking", "net core",            0),
    ("net/core/sock.c",         "networking", "net core",            0),
    ("net/core/skbuff.c",       "networking", "net core",            0),
    ("net/socket.c",            "networking", "net core",            0),
    ("include/net/sock.h",      "networking", "net core",            0),
    ("include/linux/skbuff.h",  "networking", "net core",            0),

    # ── networking: protocol/subsystem (level 1) ────────────────────
    ("net/ipv4/tcp.c",          "networking", "protocol/subsystem",  1),
    ("net/ipv6/mcast.c",        "networking", "protocol/subsystem",  1),
    ("net/unix/af_unix.c",      "networking", "protocol/subsystem",  1),
    ("net/bluetooth/hci_conn.c","networking", "protocol/subsystem",  1),
    # Catch-all branch still reached for unlisted net/ subdirs
    ("net/somethingnew/foo.c",  "networking", "protocol/subsystem",  1),

    # ── networking: net driver (level 2) ────────────────────────────
    ("drivers/net/tun.c",       "networking", "net driver",          2),
    ("drivers/net/ethernet/intel/e1000/e1000_main.c",
                                 "networking", "net driver",         2),

    # ── sound: ALSA core (level 0) — regression target ──────────────
    # Pre-empted by level-2 catch-all `^sound/[a-z...]+/` without the fix.
    ("sound/core/pcm.c",        "sound",      "ALSA core",           0),
    ("sound/core/control.c",    "sound",      "ALSA core",           0),
    ("include/sound/pcm.h",     "sound",      "ALSA core",           0),

    # ── sound: sound driver (level 2) ───────────────────────────────
    ("sound/usb/card.c",        "sound",      "sound driver",        2),
    ("sound/pci/hda/hda_intel.c","sound",     "sound driver",        2),
    ("sound/soc/soc-core.c",    "sound",      "sound driver",        2),

    # ── graphics: DRM core (level 0) — regression target ────────────
    # Pre-empted by level-2 catch-all `^drivers/gpu/drm/[a-z...]+/`
    # for paths that sit under a subdirectory (e.g. ttm/).
    ("drivers/gpu/drm/drm_ioctl.c",  "graphics", "DRM core",         0),
    ("drivers/gpu/drm/drm_atomic.c", "graphics", "DRM core",         0),
    ("drivers/gpu/drm/ttm/ttm_bo.c", "graphics", "DRM core",         0),
    ("include/drm/drm_device.h",     "graphics", "DRM core",         0),

    # ── graphics: GPU driver (level 2) ──────────────────────────────
    ("drivers/gpu/drm/i915/i915_drv.c",    "graphics", "GPU driver", 2),
    ("drivers/gpu/drm/amd/amdgpu/amdgpu.c","graphics", "GPU driver", 2),
    ("drivers/gpu/drm/nouveau/nv04.c",     "graphics", "GPU driver", 2),

    # ── block (level 0 + level 2) ───────────────────────────────────
    ("block/blk-core.c",        "block", "block core",               0),
    ("drivers/nvme/host/core.c","block", "storage driver",           2),
    ("drivers/scsi/sg.c",       "block", "storage driver",           2),

    # ── mm (level 0 only) ───────────────────────────────────────────
    ("mm/slab.c",               "mm", "mm core",                     0),
    ("mm/page_alloc.c",         "mm", "mm core",                     0),
    ("include/linux/slab.h",    "mm", "mm core",                     0),
]


def test_classify_file_layer():
    failures = []
    for path, domain, layer, level in CASES:
        result = classify_file_layer(path)
        expected = (domain, layer, level)
        if result != expected:
            failures.append(f"{path!r}: got {result}, expected {expected}")
    assert not failures, "\n".join(failures)


def test_unclassified_returns_none():
    # Paths outside every domain should be unclassified.
    assert classify_file_layer("kernel/sched/core.c") is None
    assert classify_file_layer("lib/string.c") is None
    assert classify_file_layer("arch/x86/kernel/head_64.S") is None


if __name__ == "__main__":
    test_classify_file_layer()
    test_unclassified_returns_none()
    print("ok")

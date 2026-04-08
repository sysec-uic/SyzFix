"""
Linux kernel architectural layer taxonomy.

Maps file paths to (domain, layer_name, layer_level) tuples so that
cross-layer relationships can be detected between crash sites and fix sites.

Domains represent major kernel subsystem families (filesystem, networking, etc.).
Within each domain, layers are ordered from generic/core (level 0) to
specific/implementation (level 2).
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class KernelLayer:
    """A single layer within a subsystem domain."""
    name: str               # e.g., "VFS core", "ext4"
    level: int              # 0 = most generic, higher = more specific
    path_prefixes: list[str] = field(default_factory=list)
    path_patterns: list[re.Pattern] = field(default_factory=list)


@dataclass
class SubsystemDomain:
    """A subsystem family containing ordered layers."""
    name: str               # e.g., "filesystem"
    layers: list[KernelLayer] = field(default_factory=list)

    def match(self, path: str) -> Optional[KernelLayer]:
        """Match a file path to a layer within this domain.

        Checks from most specific (highest level) to most generic (level 0)
        so that e.g. fs/ext4/super.c matches "ext4" before "VFS core".
        """
        for layer in sorted(self.layers, key=lambda l: -l.level):
            for prefix in layer.path_prefixes:
                if path.startswith(prefix) or path == prefix:
                    return layer
            for pat in layer.path_patterns:
                if pat.search(path):
                    return layer
        return None


# ─── Filesystem domain ──────────────────────────────────────────────────────

_FS_VFS_FILES = [
    "fs/dcache.c", "fs/namei.c", "fs/inode.c", "fs/super.c",
    "fs/file.c", "fs/read_write.c", "fs/open.c", "fs/namespace.c",
    "fs/attr.c", "fs/xattr.c", "fs/buffer.c", "fs/mpage.c",
    "fs/splice.c", "fs/pipe.c", "fs/seq_file.c", "fs/file_table.c",
    "fs/filesystems.c", "fs/fs_struct.c", "fs/stat.c", "fs/readdir.c",
    "fs/posix_acl.c", "fs/libfs.c", "fs/internal.h",
    "include/linux/fs.h", "include/linux/dcache.h",
    "include/linux/namei.h", "include/linux/mount.h",
    "include/linux/pagemap.h",
]

_FS_FRAMEWORK_PREFIXES = [
    "fs/iomap/", "fs/fscache/", "fs/notify/", "fs/exportfs/",
    "fs/nls/", "fs/unicode/", "fs/crypto/",
]
_FS_FRAMEWORK_FILES = [
    "fs/locks.c", "fs/aio.c", "fs/direct-io.c", "fs/ioctl.c",
    "fs/eventpoll.c", "fs/select.c", "fs/signalfd.c", "fs/timerfd.c",
    "fs/userfaultfd.c", "fs/io_uring.c",
]

FILESYSTEM_DOMAIN = SubsystemDomain(
    name="filesystem",
    layers=[
        KernelLayer(
            name="VFS core",
            level=0,
            path_prefixes=_FS_VFS_FILES,
        ),
        KernelLayer(
            name="FS framework",
            level=1,
            path_prefixes=_FS_FRAMEWORK_PREFIXES + _FS_FRAMEWORK_FILES,
        ),
        KernelLayer(
            name="specific filesystem",
            level=2,
            # Catch-all: any fs/<subdir>/ not matched above
            path_patterns=[re.compile(r'^fs/[a-z][a-z0-9_]+/')],
        ),
    ],
)


# ─── Networking domain ───────────────────────────────────────────────────────

_NET_CORE_PREFIXES = [
    "net/core/", "net/socket.c", "net/sysctl_net.c",
    "include/net/sock.h", "include/net/net_namespace.h",
    "include/linux/skbuff.h", "include/linux/netdevice.h",
    "include/linux/socket.h", "include/linux/net.h",
]

_NET_PROTOCOL_PREFIXES = [
    "net/ipv4/", "net/ipv6/", "net/unix/", "net/sctp/",
    "net/dccp/", "net/tipc/", "net/can/", "net/bluetooth/",
    "net/wireless/", "net/mac80211/", "net/netfilter/",
    "net/bridge/", "net/xfrm/", "net/l2tp/", "net/nfc/",
    "net/rds/", "net/rxrpc/", "net/smc/", "net/vmw_vsock/",
    "net/packet/", "net/key/", "net/llc/", "net/netlabel/",
    "net/phonet/", "net/rose/", "net/ax25/", "net/atm/",
    "net/decnet/", "net/x25/", "net/appletalk/",
    "net/mpls/", "net/mptcp/", "net/tls/",
    "net/9p/", "net/ceph/", "net/sunrpc/",
    "include/net/tcp.h", "include/net/udp.h",
    "include/net/ip.h", "include/net/ipv6.h",
    "include/net/sctp/", "include/net/bluetooth/",
]

NETWORKING_DOMAIN = SubsystemDomain(
    name="networking",
    layers=[
        KernelLayer(
            name="net core",
            level=0,
            path_prefixes=_NET_CORE_PREFIXES,
        ),
        KernelLayer(
            name="protocol/subsystem",
            level=1,
            path_prefixes=_NET_PROTOCOL_PREFIXES,
            # Catch remaining net/<subdir>/
            path_patterns=[re.compile(r'^net/[a-z][a-z0-9_]+/')],
        ),
        KernelLayer(
            name="net driver",
            level=2,
            path_prefixes=["drivers/net/"],
        ),
    ],
)


# ─── Block/storage domain ───────────────────────────────────────────────────

BLOCK_DOMAIN = SubsystemDomain(
    name="block",
    layers=[
        KernelLayer(
            name="block core",
            level=0,
            path_prefixes=[
                "block/", "include/linux/blkdev.h", "include/linux/bio.h",
                "include/linux/blk-mq.h", "include/linux/genhd.h",
            ],
        ),
        KernelLayer(
            name="storage driver",
            level=2,
            path_prefixes=[
                "drivers/scsi/", "drivers/nvme/", "drivers/mmc/",
                "drivers/md/", "drivers/ata/", "drivers/block/",
                "drivers/target/",
            ],
        ),
    ],
)


# ─── Device model domain ────────────────────────────────────────────────────

DEVICE_DOMAIN = SubsystemDomain(
    name="device",
    layers=[
        KernelLayer(
            name="device core",
            level=0,
            path_prefixes=[
                "drivers/base/", "include/linux/device.h",
                "include/linux/platform_device.h",
                "include/linux/device/",
            ],
        ),
        KernelLayer(
            name="bus/framework",
            level=1,
            path_prefixes=[
                "drivers/usb/core/", "drivers/pci/",
                "drivers/i2c/i2c-core", "drivers/spi/spi.c",
                "drivers/of/",
                "include/linux/usb.h", "include/linux/pci.h",
            ],
        ),
        KernelLayer(
            name="specific driver",
            level=2,
            path_prefixes=[
                "drivers/usb/", "drivers/i2c/busses/",
                "drivers/gpio/", "drivers/input/",
                "drivers/media/", "drivers/hwmon/",
                "drivers/iio/", "drivers/leds/",
                "drivers/rtc/", "drivers/watchdog/",
                "drivers/power/", "drivers/regulator/",
                "drivers/thermal/", "drivers/clk/",
                "drivers/dma/", "drivers/irqchip/",
                "drivers/pinctrl/", "drivers/mailbox/",
            ],
        ),
    ],
)


# ─── Memory management domain ───────────────────────────────────────────────

MM_DOMAIN = SubsystemDomain(
    name="mm",
    layers=[
        KernelLayer(
            name="mm core",
            level=0,
            path_prefixes=[
                "mm/", "include/linux/mm.h", "include/linux/slab.h",
                "include/linux/page-flags.h", "include/linux/mmzone.h",
                "include/linux/mm_types.h", "include/linux/vmalloc.h",
                "include/linux/gfp.h", "include/linux/memcontrol.h",
            ],
        ),
        # mm usually doesn't have a "specific" layer — subsystem-specific
        # memory is typically in the subsystem itself, which would be
        # caught by other domains. This domain is mainly useful when
        # crash is in mm/ but fix is in a specific subsystem (caught as
        # cross-domain rather than cross-layer within mm).
    ],
)


# ─── Sound domain ───────────────────────────────────────────────────────────

SOUND_DOMAIN = SubsystemDomain(
    name="sound",
    layers=[
        KernelLayer(
            name="ALSA core",
            level=0,
            path_prefixes=[
                "sound/core/", "include/sound/core.h",
                "include/sound/pcm.h", "include/sound/control.h",
                "include/sound/info.h", "include/sound/jack.h",
            ],
        ),
        KernelLayer(
            name="sound driver",
            level=2,
            path_prefixes=[
                "sound/usb/", "sound/pci/", "sound/soc/",
                "sound/hda/", "sound/firewire/",
            ],
            path_patterns=[re.compile(r'^sound/[a-z][a-z0-9_]+/')],
        ),
    ],
)


# ─── Graphics domain ────────────────────────────────────────────────────────

GRAPHICS_DOMAIN = SubsystemDomain(
    name="graphics",
    layers=[
        KernelLayer(
            name="DRM core",
            level=0,
            path_prefixes=[
                "drivers/gpu/drm/drm_", "drivers/gpu/drm/ttm/",
                "include/drm/drm_", "include/uapi/drm/drm",
            ],
            path_patterns=[re.compile(r'^drivers/gpu/drm/[a-z_]+\.c$')],
        ),
        KernelLayer(
            name="GPU driver",
            level=2,
            path_prefixes=[
                "drivers/gpu/drm/i915/", "drivers/gpu/drm/amd/",
                "drivers/gpu/drm/nouveau/", "drivers/gpu/drm/radeon/",
                "drivers/gpu/drm/msm/", "drivers/gpu/drm/virtio/",
                "drivers/gpu/drm/vmwgfx/", "drivers/gpu/drm/xe/",
            ],
            path_patterns=[re.compile(r'^drivers/gpu/drm/[a-z][a-z0-9_]+/')],
        ),
    ],
)


# ─── All domains ────────────────────────────────────────────────────────────

DOMAINS: list[SubsystemDomain] = [
    FILESYSTEM_DOMAIN,
    NETWORKING_DOMAIN,
    BLOCK_DOMAIN,
    DEVICE_DOMAIN,
    MM_DOMAIN,
    SOUND_DOMAIN,
    GRAPHICS_DOMAIN,
]


def classify_file_layer(
    path: str,
) -> Optional[tuple[str, str, int]]:
    """Classify a kernel source file path into (domain, layer_name, level).

    Returns None if the file doesn't belong to any known domain.
    """
    for domain in DOMAINS:
        layer = domain.match(path)
        if layer is not None:
            return (domain.name, layer.name, layer.level)
    return None


def classify_files(paths: list[str]) -> dict[str, list[tuple[str, str, int]]]:
    """Classify multiple files, grouped by domain.

    Returns {domain_name: [(path, layer_name, level), ...]}.
    """
    by_domain: dict[str, list[tuple[str, str, int]]] = {}
    for p in paths:
        result = classify_file_layer(p)
        if result is not None:
            domain_name, layer_name, level = result
            by_domain.setdefault(domain_name, []).append(
                (p, layer_name, level)
            )
    return by_domain


def get_layer_label(domain: str, layer_name: str, level: int) -> str:
    """Human-readable label for a layer classification."""
    level_labels = {0: "core", 1: "framework", 2: "specific"}
    return f"{layer_name} ({level_labels.get(level, f'L{level}')})"

"""Configuration for syzbot dataset builder."""

import os
from pathlib import Path

# === Project paths ===
PROJECT_ROOT = Path(__file__).parent
# Data root. Defaults to dataset/data inside this repo; set SYZFIX_DATA_DIR
# when the data lives elsewhere (e.g. consuming this package from another repo).
DATA_DIR = Path(os.environ.get("SYZFIX_DATA_DIR", PROJECT_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
RAW_SYZBOT_DIR = RAW_DIR / "syzbot"
RAW_PATCHES_DIR = RAW_DIR / "patches"
RAW_DISCUSSIONS_DIR = RAW_DIR / "discussions"
PROCESSED_DIR = DATA_DIR / "processed"
DATASET_DIR = DATA_DIR / "dataset"
TRAINING_DIR = DATA_DIR / "training"
DB_PATH = DATA_DIR / "progress.db"

# === Syzbot API ===
SYZBOT_BASE_URL = "https://syzkaller.appspot.com"
SYZBOT_FIXED_URL = f"{SYZBOT_BASE_URL}/upstream/fixed?json=1"
SYZBOT_BUG_URL_TEMPLATE = f"{SYZBOT_BASE_URL}/bug?extid={{extid}}&json=1"
SYZBOT_BUG_ID_URL_TEMPLATE = f"{SYZBOT_BASE_URL}/bug?id={{extid}}&json=1"
SYZBOT_TEXT_URL_TEMPLATE = f"{SYZBOT_BASE_URL}{{path}}"

# === git.kernel.org ===
GIT_KERNEL_PATCH_URL_TEMPLATE = (
    "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/patch/?id={hash}"
)
# Fallback repos when commit not found in torvalds/linux
GIT_KERNEL_FALLBACK_REPOS = [
    "https://git.kernel.org/pub/scm/linux/kernel/git/netdev/net.git/patch/?id={hash}",
    "https://git.kernel.org/pub/scm/linux/kernel/git/netdev/net-next.git/patch/?id={hash}",
    "https://git.kernel.org/pub/scm/linux/kernel/git/bpf/bpf.git/patch/?id={hash}",
    "https://git.kernel.org/pub/scm/linux/kernel/git/bpf/bpf-next.git/patch/?id={hash}",
]

# === lore.kernel.org ===
LORE_BASE_URL = "https://lore.kernel.org"
LORE_SEARCH_URL_TEMPLATE = f"{LORE_BASE_URL}/all/?q={{query}}&x=A"
# Append t.mbox.gz to a lore thread URL to get mbox format

# === patchwork.kernel.org ===
PATCHWORK_API_BASE = "https://patchwork.kernel.org/api/1.2"
PATCHWORK_PATCHES_URL = f"{PATCHWORK_API_BASE}/patches/"
PATCHWORK_SERIES_URL_TEMPLATE = f"{PATCHWORK_API_BASE}/series/{{series_id}}/"

# === Rate limiting (requests per second per domain) ===
RATE_LIMITS = {
    "syzkaller.appspot.com": 0.25,  # 1 request per 4s to avoid 429
    "lore.kernel.org": 1.0,
    "git.kernel.org": 1.0,
    "patchwork.kernel.org": 1.0,
}

# === HTTP settings ===
HTTP_TIMEOUT = 60  # seconds
HTTP_MAX_RETRIES = 3
HTTP_RETRY_BACKOFF = 2.0  # exponential backoff multiplier
HTTP_USER_AGENT = "syzbot-dataset-builder/1.0 (research project)"

# === Concurrency ===
MAX_CONCURRENT_REQUESTS = 5  # global limit across all domains

# === Data limits ===
MAX_CRASHES_PER_BUG = 5  # only download top N crashes per bug
MAX_THREAD_EMAILS = 200  # skip threads with more than N emails

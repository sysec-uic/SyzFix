"""
Shared data loading and filtering for the SyzFix dataset.

Loads processed JSON files from dataset/data/processed/ and provides
convenient iterators and accessors for analysis modules.
"""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from dataset.config import PROCESSED_DIR

# Path to the processed data directory (honors SYZFIX_DATA_DIR via dataset.config)
DATA_DIR = PROCESSED_DIR


@dataclass
class Message:
    """A single email message from a discussion thread."""
    message_id: str
    in_reply_to: str
    from_addr: str
    date: str
    subject: str
    body: str

    @property
    def sender_name(self) -> str:
        """Extract human-readable name from from_addr."""
        m = re.match(r'^([^<]+)<', self.from_addr)
        return m.group(1).strip() if m else self.from_addr

    @property
    def sender_email(self) -> str:
        m = re.search(r'<([^>]+)>', self.from_addr)
        return m.group(1) if m else self.from_addr

    @property
    def original_text(self) -> str:
        """Return body text with quoted lines removed."""
        lines = self.body.split('\n')
        original = [l for l in lines if not l.startswith('>')]
        return '\n'.join(original).strip()

    @property
    def is_reply(self) -> bool:
        return bool(self.in_reply_to)


@dataclass
class Discussion:
    """A discussion thread associated with a bug."""
    url: str
    subject: str
    patch_version: Optional[int]
    is_syzbot_report: bool
    messages: list[Message] = field(default_factory=list)


@dataclass
class FixCommit:
    """A fix commit for a bug."""
    hash: str
    title: str
    author: str
    author_name: str
    date: str
    patch_diff: str
    repo: str = ""
    branch: str = ""


@dataclass
class BugEntry:
    """A single bug entry loaded from the processed dataset."""
    bug_id: str
    title: str
    raw: dict  # original dict for fields we don't explicitly model

    @property
    def status(self) -> str:
        return self.raw.get("status", "")

    @property
    def first_crash(self) -> Optional[str]:
        return self.raw.get("first_crash")

    @property
    def fix_time(self) -> Optional[str]:
        return self.raw.get("fix_time")

    @property
    def fix_commits(self) -> list[FixCommit]:
        commits = []
        for c in self.raw.get("fix_commits", []):
            commits.append(FixCommit(
                hash=c.get("hash", ""),
                title=c.get("title", ""),
                author=c.get("author", ""),
                author_name=c.get("author_name", ""),
                date=c.get("date", ""),
                patch_diff=c.get("patch_diff", ""),
                repo=c.get("repo", ""),
                branch=c.get("branch", ""),
            ))
        return commits

    @property
    def discussions(self) -> list[Discussion]:
        discs = []
        for d in self.raw.get("discussions", []):
            msgs = [
                Message(
                    message_id=m.get("message_id", ""),
                    in_reply_to=m.get("in_reply_to", ""),
                    from_addr=m.get("from_addr", ""),
                    date=m.get("date", ""),
                    subject=m.get("subject", ""),
                    body=m.get("body", ""),
                )
                for m in d.get("messages", [])
            ]
            discs.append(Discussion(
                url=d.get("url", ""),
                subject=d.get("subject", ""),
                patch_version=d.get("patch_version"),
                is_syzbot_report=d.get("is_syzbot_report", False),
                messages=msgs,
            ))
        return discs

    @property
    def patch_versions(self) -> list[Discussion]:
        """Return only discussions associated with a patch version, sorted."""
        pvs = [d for d in self.discussions if d.patch_version is not None]
        pvs.sort(key=lambda d: d.patch_version or 0)
        return pvs

    @property
    def num_patch_versions(self) -> int:
        versions = set()
        for d in self.raw.get("discussions", []):
            pv = d.get("patch_version")
            if pv is not None:
                versions.add(pv)
        return len(versions)

    @property
    def has_multiple_versions(self) -> bool:
        return self.num_patch_versions >= 2

    @property
    def crash_report(self) -> str:
        # First check top-level, then fall back to first crash entry
        cr = self.raw.get("crash_report", "")
        if cr:
            return cr
        crashes = self.raw.get("crashes", [])
        if crashes:
            return crashes[0].get("crash_report", "")
        return ""

    @property
    def c_reproducer(self) -> str:
        cr = self.raw.get("c_reproducer", "")
        if cr:
            return cr
        crashes = self.raw.get("crashes", [])
        if crashes:
            return crashes[0].get("c_reproducer", "")
        return ""

    @property
    def subsystem_guess(self) -> str:
        """Guess subsystem from the bug title prefix (e.g., 'KASAN:', 'BUG:')."""
        # Try to extract from fix commit path
        for c in self.raw.get("fix_commits", []):
            diff = c.get("patch_diff", "")
            files = re.findall(r'diff --git a/(\S+)', diff)
            if files:
                # Use top-level directory as subsystem
                top_dirs = set()
                for f in files:
                    parts = f.split('/')
                    if len(parts) >= 2:
                        top_dirs.add(parts[0] + '/' + parts[1])
                    else:
                        top_dirs.add(parts[0])
                return ', '.join(sorted(top_dirs))
        return "unknown"


def load_bug_file(fname: Path) -> Optional[BugEntry]:
    """Load a single processed bug JSON file (None on decode error).

    A MemoryError (e.g. a pathologically large bug file on a memory-tight
    machine) skips the file with a warning instead of killing a whole
    multi-analyzer run partway through.
    """
    with open(fname, 'r', encoding='utf-8', errors='replace') as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError:
            return None
        except MemoryError:
            import sys
            print(f"WARNING: skipping {fname.name} "
                  f"({fname.stat().st_size / 1_048_576:.0f} MB): "
                  f"not enough memory to parse it", file=sys.stderr)
            return None
    return BugEntry(
        bug_id=raw.get("bug_id", fname.stem),
        title=raw.get("title", ""),
        raw=raw,
    )


def load_all_bugs(data_dir: Path = DATA_DIR) -> list[BugEntry]:
    """Load all processed bug JSON files into memory.

    The parsed corpus needs roughly 2-3x the on-disk JSON size in RAM
    (~30 GB at 7k bugs); prefer LazyBugs when that doesn't comfortably fit.
    """
    bugs = []
    for fname in sorted(data_dir.glob("*.json")):
        bug = load_bug_file(fname)
        if bug is not None:
            bugs.append(bug)
    return bugs


def iter_bugs(data_dir: Path = DATA_DIR) -> Iterator[BugEntry]:
    """Iterate over all bugs without loading all into memory at once."""
    for fname in sorted(data_dir.glob("*.json")):
        bug = load_bug_file(fname)
        if bug is not None:
            yield bug


class LazyBugs:
    """Sequence-like view over the corpus that re-streams from disk on every
    iteration instead of holding all bugs in memory.

    Supports the access patterns the analyzers use — repeated iteration and
    len() — with peak memory of a single bug. Each full iteration re-parses
    the JSON files, so a pass costs load time again; use load_all_bugs() when
    the corpus fits in RAM and you need many passes to be fast.
    """

    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = data_dir
        self._len: Optional[int] = None

    def __iter__(self) -> Iterator[BugEntry]:
        return iter_bugs(self.data_dir)

    def __len__(self) -> int:
        if self._len is None:
            # File count; decode-error files (skipped by iteration) are rare
            # enough that the difference doesn't matter for progress totals.
            self._len = sum(1 for _ in self.data_dir.glob("*.json"))
        return self._len


def bugs_with_evolution(bugs: list[BugEntry]) -> list[BugEntry]:
    """Filter to bugs that have multiple patch versions."""
    return [b for b in bugs if b.has_multiple_versions]


def bugs_with_discussion(bugs: list[BugEntry]) -> list[BugEntry]:
    """Filter to bugs that have any discussion messages."""
    return [b for b in bugs if any(d.messages for d in b.discussions)]

"""Data models for syzbot dataset."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Email:
    """A single email message from a mailing list thread."""
    message_id: str = ""
    in_reply_to: str = ""
    from_addr: str = ""
    date: str = ""
    subject: str = ""
    body: str = ""


@dataclass
class Discussion:
    """A mailing list discussion thread."""
    url: str = ""
    subject: str = ""
    patch_version: Optional[int] = None  # v1, v2, v3...
    is_syzbot_report: bool = False
    messages: list[Email] = field(default_factory=list)


@dataclass
class FixCommit:
    """A git commit that fixes a bug."""
    hash: str = ""
    title: str = ""
    link: str = ""
    repo: str = ""
    branch: str = ""
    author: str = ""
    author_name: str = ""
    date: str = ""
    patch_diff: str = ""  # actual diff content from git.kernel.org


@dataclass
class Crash:
    """A single crash instance reported by syzbot."""
    title: str = ""
    kernel_commit: str = ""
    kernel_config_link: str = ""
    crash_report_link: str = ""
    syz_reproducer_link: str = ""
    c_reproducer_link: str = ""
    # Downloaded content
    crash_report: str = ""
    syz_reproducer: str = ""
    c_reproducer: str = ""


@dataclass
class PatchVersion:
    """A specific version of a patch (v1, v2, etc.)."""
    version: int = 1
    subject: str = ""
    diff: str = ""
    cover_letter: str = ""
    discussion: list[Email] = field(default_factory=list)


@dataclass
class BugEntry:
    """Complete data for a single fixed kernel bug."""
    bug_id: str = ""  # extid from syzkaller
    title: str = ""
    status: str = ""
    first_crash: str = ""
    last_crash: str = ""
    fix_time: str = ""
    # Core data
    fix_commits: list[FixCommit] = field(default_factory=list)
    discussions: list[Discussion] = field(default_factory=list)
    crashes: list[Crash] = field(default_factory=list)
    patch_versions: list[PatchVersion] = field(default_factory=list)
    # Raw syzbot JSON for reference
    raw_syzbot_data: dict = field(default_factory=dict)
    # Processing metadata
    processing_errors: list[str] = field(default_factory=list)


@dataclass
class DatasetEntry:
    """Final dataset entry for export (fine-tuning friendly format)."""
    bug_id: str = ""
    title: str = ""
    crash_report: str = ""
    c_reproducer: str = ""
    syz_reproducer: str = ""
    fix_commit_hash: str = ""
    fix_commit_message: str = ""
    final_patch_diff: str = ""
    patch_evolution: list[dict] = field(default_factory=list)
    # Metadata
    subsystem: str = ""
    first_crash_date: str = ""
    fix_date: str = ""
    num_patch_versions: int = 0
    has_discussion: bool = False

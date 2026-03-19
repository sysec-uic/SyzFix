"""
Analyzer: Patch backport / downstream propagation to stable/LTS kernels.

Linux kernel fixes follow a unique lifecycle: patches land on mainline first,
then get backported (cherry-picked) to active stable/LTS branches (e.g., 5.4,
5.10, 5.15, 6.1).  This analyzer extracts backport signals from the dataset
to quantify:

  1. Stable-targeting intent — does the commit / patch contain "Cc: stable"?
  2. Backport coverage     — which LTS versions received the fix?
  3. Backport lag           — time between upstream merge and stable review
  4. Patch adaptation       — was the backport cherry-picked cleanly or
                              modified to fit an older tree?
  5. Subsystem patterns     — which subsystems get the most backports?
"""

import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Optional

from ..loader import BugEntry, Discussion
from ..filters import is_stable_backport_thread
from .base import BaseAnalyzer, AnalysisResult


# ─── Helpers ─────────────────────────────────────────────────────────────────

# Active LTS versions that appear in stable review threads
_VERSION_RE = re.compile(r'(\d+\.\d+)')

# Cc: stable tag in patch diff or commit message body
_CC_STABLE_RE = re.compile(
    r'[Cc]c:\s*<?stable@vger\.kernel\.org>?',
)

# Fixes: tag — indicates the commit being fixed (strong backport signal)
_FIXES_TAG_RE = re.compile(
    r'^Fixes:\s+([0-9a-f]{8,40})\s+\("(.+?)"\)',
    re.MULTILINE,
)

# [Upstream commit HASH] — present in stable backport patches
_UPSTREAM_COMMIT_RE = re.compile(
    r'\[\s*[Uu]pstream\s+commit\s+([0-9a-f]{8,40})\s*\]',
)

# Stable version from subject: [PATCH X.Y NNN/MMM]
_STABLE_SUBJECT_VERSION_RE = re.compile(
    r'\[PATCH\s+(\d+\.\d+)\s+\d+/\d+\]',
)

# Review cycle subject: X.Y.Z-rcN review
_REVIEW_CYCLE_VERSION_RE = re.compile(
    r'(\d+\.\d+)\.\d+-rc\d+\s+review',
    re.I,
)

# RFC 2822 date formats commonly seen in kernel emails
_DATE_FORMATS = [
    "%a, %d %b %Y %H:%M:%S %z",
    "%d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
]


def _parse_date(date_str: str) -> Optional[datetime]:
    """Try to parse a date string from kernel email headers."""
    if not date_str:
        return None
    # Strip trailing parenthetical timezone names like " (UTC)"
    clean = re.sub(r'\s*\([^)]*\)\s*$', '', date_str.strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            continue
    return None


def _extract_stable_versions_from_discussion(disc: Discussion) -> set[str]:
    """Extract the target stable version(s) from a backport discussion thread."""
    versions = set()

    # From subject
    m = _STABLE_SUBJECT_VERSION_RE.search(disc.subject)
    if m:
        versions.add(m.group(1))

    m = _REVIEW_CYCLE_VERSION_RE.search(disc.subject)
    if m:
        versions.add(m.group(1))

    # AUTOSEL subjects often have version
    if 'AUTOSEL' in disc.subject:
        m = _VERSION_RE.search(disc.subject)
        if m:
            ver = m.group(1)
            # Filter out obviously-not-version numbers
            parts = ver.split('.')
            if len(parts) == 2 and int(parts[0]) >= 3:
                versions.add(ver)

    return versions


def _has_cc_stable(bug: BugEntry) -> bool:
    """Check if any fix commit or patch submission contains Cc: stable."""
    # Check fix commit diffs
    for fc in bug.fix_commits:
        if fc.patch_diff and _CC_STABLE_RE.search(fc.patch_diff):
            return True
    # Check discussion message bodies (patch submission emails)
    for disc in bug.discussions:
        if is_stable_backport_thread(disc):
            continue
        for msg in disc.messages[:20]:  # limit to avoid perf issues
            if _CC_STABLE_RE.search(msg.body[:2000]):
                return True
    return False


def _has_fixes_tag(bug: BugEntry) -> bool:
    """Check if any fix commit contains a Fixes: tag."""
    for fc in bug.fix_commits:
        if fc.patch_diff and _FIXES_TAG_RE.search(fc.patch_diff):
            return True
    return False


def _get_backport_threads(bug: BugEntry) -> list[Discussion]:
    """Return discussion threads that are stable backport series."""
    return [d for d in bug.discussions if is_stable_backport_thread(d)]


def _get_upstream_commit_date(bug: BugEntry) -> Optional[datetime]:
    """Get the date of the upstream fix commit."""
    for fc in bug.fix_commits:
        dt = _parse_date(fc.date)
        if dt:
            return dt
    return None


def _get_earliest_backport_date(threads: list[Discussion]) -> Optional[datetime]:
    """Get the earliest message date across stable backport threads."""
    earliest = None
    for disc in threads:
        for msg in disc.messages[:5]:  # first few messages have the date
            dt = _parse_date(msg.date)
            if dt and (earliest is None or dt < earliest):
                earliest = dt
    return earliest


def _count_upstream_commits_in_thread(disc: Discussion) -> int:
    """Count how many [Upstream commit ...] tags appear in a backport thread."""
    count = 0
    for msg in disc.messages:
        if _UPSTREAM_COMMIT_RE.search(msg.body[:500]):
            count += 1
    return count


def _get_subsystem(bug: BugEntry) -> str:
    """Extract top-level subsystem from fix commit paths."""
    for fc in bug.fix_commits:
        if not fc.patch_diff:
            continue
        files = re.findall(r'diff --git a/(\S+)', fc.patch_diff)
        if files:
            top = files[0].split('/')[0]
            return top
    return "unknown"


# ─── Analyzer ────────────────────────────────────────────────────────────────

class BackportDownstreamAnalyzer(BaseAnalyzer):
    """Analyze how fixes propagate downstream to stable/LTS kernel branches."""

    @property
    def name(self) -> str:
        return "Backport Downstream Propagation"

    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        total = 0
        has_fix_diff = 0
        cc_stable_count = 0
        fixes_tag_count = 0
        has_backport_threads_count = 0
        both_signals_count = 0  # Cc: stable AND Fixes: tag

        version_counter: Counter = Counter()
        versions_per_bug: list[int] = []
        lag_days: list[float] = []
        subsystem_backport: Counter = Counter()
        subsystem_total: Counter = Counter()

        per_bug_details: list[dict[str, Any]] = []
        # Track bugs with most LTS coverage for examples
        top_coverage: list[tuple[int, dict]] = []

        for bug in bugs:
            if not bug.fix_commits:
                continue
            has_diff = any(fc.patch_diff for fc in bug.fix_commits)
            if not has_diff:
                continue

            total += 1
            has_fix_diff += 1

            cc_stable = _has_cc_stable(bug)
            fixes_tag = _has_fixes_tag(bug)
            backport_threads = _get_backport_threads(bug)
            subsystem = _get_subsystem(bug)
            subsystem_total[subsystem] += 1

            if cc_stable:
                cc_stable_count += 1
            if fixes_tag:
                fixes_tag_count += 1
            if cc_stable and fixes_tag:
                both_signals_count += 1

            # Extract target versions from backport threads
            target_versions: set[str] = set()
            for bt in backport_threads:
                target_versions |= _extract_stable_versions_from_discussion(bt)

            if backport_threads:
                has_backport_threads_count += 1
                for v in target_versions:
                    version_counter[v] += 1
                subsystem_backport[subsystem] += 1

            versions_per_bug.append(len(target_versions))

            # Compute backport lag
            lag = None
            if backport_threads:
                upstream_dt = _get_upstream_commit_date(bug)
                backport_dt = _get_earliest_backport_date(backport_threads)
                if upstream_dt and backport_dt:
                    # Make both offset-naive for subtraction
                    u = upstream_dt.replace(tzinfo=None)
                    b = backport_dt.replace(tzinfo=None)
                    delta = (b - u).total_seconds() / 86400
                    if 0 <= delta <= 365:  # sanity check
                        lag = round(delta, 1)
                        lag_days.append(delta)

            detail = {
                "bug_id": bug.bug_id,
                "title": bug.title,
                "subsystem": subsystem,
                "cc_stable": cc_stable,
                "fixes_tag": fixes_tag,
                "num_backport_threads": len(backport_threads),
                "target_versions": sorted(target_versions),
                "num_lts_versions": len(target_versions),
                "backport_lag_days": lag,
            }
            per_bug_details.append(detail)

            if target_versions:
                top_coverage.append((len(target_versions), detail))

        # Sort for examples of widest coverage
        top_coverage.sort(key=lambda x: -x[0])

        # ── Summary ──────────────────────────────────────────────────────
        avg_versions = (
            sum(versions_per_bug) / max(len(versions_per_bug), 1)
        )
        median_lag = None
        if lag_days:
            s = sorted(lag_days)
            median_lag = round(s[len(s) // 2], 1)

        summary: dict[str, Any] = {
            "Bugs analyzed (with fix diff)": has_fix_diff,
            "Bugs with Cc: stable": cc_stable_count,
            "Bugs with Fixes: tag": fixes_tag_count,
            "Bugs with both Cc:stable + Fixes:": both_signals_count,
            "Cc:stable rate": f"{cc_stable_count / max(total, 1) * 100:.1f}%",
            "Fixes: tag rate": f"{fixes_tag_count / max(total, 1) * 100:.1f}%",
            "Bugs with stable backport threads": has_backport_threads_count,
            "Backport thread rate": f"{has_backport_threads_count / max(total, 1) * 100:.1f}%",
            "Distinct LTS versions seen": len(version_counter),
            "Avg LTS versions per backported bug": round(
                sum(v for v in versions_per_bug if v > 0) / max(has_backport_threads_count, 1), 2
            ),
            "Median backport lag (days)": median_lag,
        }

        # ── Tables ───────────────────────────────────────────────────────

        # 1. LTS version distribution
        version_table = []
        for ver, count in version_counter.most_common():
            version_table.append({
                "lts_version": ver,
                "bugs_backported": count,
                "pct_of_backported": f"{count / max(has_backport_threads_count, 1) * 100:.1f}%",
            })

        # 2. Stable-targeting intent matrix
        intent_table = [
            {
                "signal": "Cc: stable only",
                "count": cc_stable_count - both_signals_count,
                "pct": f"{(cc_stable_count - both_signals_count) / max(total, 1) * 100:.1f}%",
            },
            {
                "signal": "Fixes: tag only",
                "count": fixes_tag_count - both_signals_count,
                "pct": f"{(fixes_tag_count - both_signals_count) / max(total, 1) * 100:.1f}%",
            },
            {
                "signal": "Both Cc:stable + Fixes:",
                "count": both_signals_count,
                "pct": f"{both_signals_count / max(total, 1) * 100:.1f}%",
            },
            {
                "signal": "Neither (no stable intent)",
                "count": total - (cc_stable_count + fixes_tag_count - both_signals_count),
                "pct": f"{(total - (cc_stable_count + fixes_tag_count - both_signals_count)) / max(total, 1) * 100:.1f}%",
            },
        ]

        # 3. Backport lag distribution (buckets)
        lag_buckets = {"0-3 days": 0, "4-7 days": 0, "8-14 days": 0,
                       "15-30 days": 0, "31-60 days": 0, "60+ days": 0}
        for d in lag_days:
            if d <= 3:
                lag_buckets["0-3 days"] += 1
            elif d <= 7:
                lag_buckets["4-7 days"] += 1
            elif d <= 14:
                lag_buckets["8-14 days"] += 1
            elif d <= 30:
                lag_buckets["15-30 days"] += 1
            elif d <= 60:
                lag_buckets["31-60 days"] += 1
            else:
                lag_buckets["60+ days"] += 1

        lag_table = [
            {"bucket": k, "count": v,
             "pct": f"{v / max(len(lag_days), 1) * 100:.1f}%"}
            for k, v in lag_buckets.items()
        ]

        # 4. Subsystem backport rates (top 20)
        subsystem_table = []
        for sub in sorted(subsystem_total, key=lambda s: -subsystem_backport.get(s, 0)):
            t = subsystem_total[sub]
            b = subsystem_backport.get(sub, 0)
            if t < 5:
                continue  # skip rare subsystems
            subsystem_table.append({
                "subsystem": sub,
                "total_bugs": t,
                "backported": b,
                "backport_rate": f"{b / t * 100:.1f}%",
            })
        subsystem_table = subsystem_table[:20]

        # 5. Coverage tiers — how many LTS versions per bug
        tier_counter: Counter = Counter()
        for n in versions_per_bug:
            if n == 0:
                tier_counter["0 (no backport thread)"] += 1
            elif n == 1:
                tier_counter["1 version"] += 1
            elif n <= 3:
                tier_counter["2-3 versions"] += 1
            elif n <= 5:
                tier_counter["4-5 versions"] += 1
            else:
                tier_counter["6+ versions"] += 1

        tier_order = ["0 (no backport thread)", "1 version", "2-3 versions",
                      "4-5 versions", "6+ versions"]
        coverage_table = [
            {"tier": t, "count": tier_counter.get(t, 0),
             "pct": f"{tier_counter.get(t, 0) / max(total, 1) * 100:.1f}%"}
            for t in tier_order
        ]

        # 6. Top examples of wide backport coverage
        examples_table = []
        for _, d in top_coverage[:15]:
            examples_table.append({
                "bug_id": d["bug_id"],
                "title": d["title"][:60],
                "subsystem": d["subsystem"],
                "lts_versions": ", ".join(d["target_versions"]),
                "num_versions": d["num_lts_versions"],
                "lag_days": d["backport_lag_days"],
            })

        return AnalysisResult(
            name=self.name,
            summary=summary,
            details=per_bug_details[:100],
            tables={
                "lts_version_distribution": version_table,
                "stable_intent_signals": intent_table,
                "backport_lag_distribution": lag_table,
                "subsystem_backport_rates": subsystem_table,
                "coverage_tiers": coverage_table,
                "top_backport_coverage": examples_table,
            },
        )

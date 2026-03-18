"""
Analyzer: Patch Evolution Causal Analysis

For each bug with 2+ patch versions, traces the causal chain from reviewer
feedback on version N to structural changes in version N+1. This is the key
differentiator of SyzFix — capturing HOW patches evolve through review.

For every consecutive vN → vN+1 transition:
  1. Classifies reviewer feedback on vN (reuses revision_reasons taxonomy)
  2. Extracts changelog notes from vN+1 submission
  3. Measures structural delta between vN and vN+1 diffs
  4. Links feedback categories to changelog categories (responsiveness)
  5. Tracks time between last review and next version submission

Produces 4 output tables:
  - iteration_transitions: per-transition records
  - feedback_impact: which feedback categories drive the most change
  - evolution_summary: per-bug summaries
  - response_patterns: how developers respond to different feedback types
"""

import re
from collections import Counter, defaultdict
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from statistics import median
from typing import Any, Optional

from ..loader import BugEntry, Discussion, Message
from ..filters import get_human_reviews, get_review_text, is_patch_submission
from .base import BaseAnalyzer, AnalysisResult
from .revision_reasons import classify_review_text, REVISION_CATEGORIES
from .patch_diff_analysis import parse_diff_stats, extract_diff_from_patch_message


# ─── Helper functions ──────────────────────────────────────────────────────


def parse_email_datetime(date_str: str) -> Optional[datetime]:
    """Parse an RFC 2822 email date string into a UTC datetime object."""
    if not date_str:
        return None
    dt = None
    try:
        dt = parsedate_to_datetime(date_str)
    except Exception:
        pass
    if dt is None:
        try:
            dt = datetime.fromisoformat(date_str)
        except Exception:
            return None
    # Normalize to UTC to avoid naive/aware comparison issues
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def extract_changelog_notes(discussion: Discussion) -> tuple[str, list[str]]:
    """
    Extract 'Changes since vN' notes from a patch submission message.

    Returns (raw_changelog_text, list_of_bullet_points).
    """
    for msg in discussion.messages:
        if not is_patch_submission(msg):
            continue
        body = msg.body

        # Try multiple changelog formats found in kernel mailing list patches
        patterns = [
            # "Changes since v1:", "Changes in v2:", "Changes from v1:"
            r'(?:change(?:s)?\s+(?:since|in|from)\s+v\d+)[:\s]*\n(.*?)(?:\n\s*\n|\n---\n|\n\s*\S+/\S+\s*\|)',
            # "v1 -> v2:", "v1 --> v2:", "v1 => v2:", "v1 → v2:"
            r'(?:v\d+\s*(?:->|-->|=>|→)\s*v\d+)[:\s]*\n(.*?)(?:\n\s*\n|\n---\n|\n\s*\S+/\S+\s*\|)',
            # "What changed:" or "Changelog:"
            r'(?:what\s+changed|changelog)[:\s]*\n(.*?)(?:\n\s*\n|\n---\n)',
        ]

        for pattern in patterns:
            m = re.search(pattern, body, re.I | re.S)
            if m:
                raw = m.group(1).strip()
                # Split into bullet points
                bullets = re.split(r'\n\s*[-*]\s+', raw)
                bullets = [b.strip() for b in bullets if b.strip()]
                return raw, bullets

    return "", []


# Patterns for actionable feedback in review text
_ACTIONABLE_PATTERNS = {
    "suggestion": re.compile(
        r'(?:you\s+)?(?:should|need to|must|have to|could|might want to)\s+(.+)',
        re.I,
    ),
    "request": re.compile(
        r'(?:please|pls)\s+(.+)',
        re.I,
    ),
    "imperative": re.compile(
        r'(?:^|\.\s+)(?:missing|add|remove|use|check|handle|fix|drop|move|rename|replace|split|merge)\s+(.+)',
        re.I | re.MULTILINE,
    ),
    "question_suggestion": re.compile(
        r'(?:what about|how about|why not|can you|could you)\s+(.+)',
        re.I,
    ),
    "this_statement": re.compile(
        r'(?:this\s+(?:should|needs|will|can|doesn.t|isn.t|won.t))\s+(.+)',
        re.I,
    ),
}


def extract_actionable_snippets(review_text: str) -> list[dict]:
    """
    Extract actionable feedback lines from review text.

    Returns up to 10 snippets with their action type.
    """
    snippets = []
    seen_lines = set()

    for line in review_text.split('\n'):
        line = line.strip()
        if not line or len(line) < 10:
            continue
        for action_type, pattern in _ACTIONABLE_PATTERNS.items():
            if pattern.search(line):
                # Deduplicate by first 60 chars
                key = line[:60].lower()
                if key not in seen_lines:
                    seen_lines.add(key)
                    snippets.append({
                        "text": line[:300],
                        "action_type": action_type,
                    })
                break
        if len(snippets) >= 10:
            break

    return snippets


def estimate_feedback_responsiveness(
    feedback_cats: list[str],
    changelog_cats: list[str],
    changelog_text: str,
) -> dict[str, Any]:
    """
    Estimate how much of the reviewer feedback was addressed in the next version.

    Uses category overlap as primary signal, keyword overlap as secondary.
    """
    if not feedback_cats:
        return {
            "addressed": [],
            "potentially_addressed": [],
            "not_addressed": [],
            "responsiveness_score": None,
        }

    feedback_set = set(feedback_cats)
    changelog_set = set(changelog_cats)
    changelog_lower = changelog_text.lower()

    addressed = feedback_set & changelog_set
    remaining = feedback_set - addressed

    # Secondary check: keyword overlap in changelog text
    potentially_addressed = set()
    for cat in remaining:
        # Use category name words as keywords
        keywords = cat.replace("_", " ").split()
        if any(kw in changelog_lower for kw in keywords if len(kw) > 3):
            potentially_addressed.add(cat)

    not_addressed = remaining - potentially_addressed

    total = len(feedback_set)
    score = (len(addressed) + 0.5 * len(potentially_addressed)) / total

    return {
        "addressed": sorted(addressed),
        "potentially_addressed": sorted(potentially_addressed),
        "not_addressed": sorted(not_addressed),
        "responsiveness_score": round(score, 3),
    }


# ─── Core transition analysis ─────────────────────────────────────────────


def analyze_transition(
    bug: BugEntry,
    from_version: int,
    to_version: int,
    curr_discussions: list[Discussion],
    next_discussions: list[Discussion],
) -> dict[str, Any]:
    """
    Analyze a single vN → vN+1 transition.

    Links reviewer feedback on v_curr to changes in v_next.
    curr_discussions/next_discussions may contain multiple Discussion objects
    for the same version (e.g., multiple threads in a patch series).
    """
    curr_msgs = merged_messages(curr_discussions)
    next_msgs = merged_messages(next_discussions)

    # 1. Extract human reviews from v_curr
    reviews = get_human_reviews(curr_msgs)

    # 2. Classify each review
    all_feedback_cats = []
    all_snippets = []
    reviewer_names = set()

    for review in reviews:
        text = get_review_text(review)
        cats = classify_review_text(text)
        all_feedback_cats.extend(cats)
        snippets = extract_actionable_snippets(text)
        all_snippets.extend(snippets)
        reviewer_names.add(review.sender_name)

    feedback_counter = Counter(all_feedback_cats)

    # 3. Extract changelog from v_next (try each discussion)
    changelog_text, changelog_bullets = "", []
    for disc in next_discussions:
        changelog_text, changelog_bullets = extract_changelog_notes(disc)
        if changelog_text:
            break
    changelog_cats = classify_review_text(changelog_text) if changelog_text else []

    # 4. Structural delta — use first discussion with a diff from each version
    structural = {"has_diffs": False}
    curr_diff = extract_diff_from_patch_message(curr_msgs)
    next_diff = extract_diff_from_patch_message(next_msgs)
    if curr_diff and next_diff:
        curr_stats = parse_diff_stats(curr_diff)
        next_stats = parse_diff_stats(next_diff)
        curr_files = set(curr_stats.get("files", []))
        next_files = set(next_stats.get("files", []))
        added = next_files - curr_files
        removed = curr_files - next_files
        if added and not removed:
            scope_change = "expanded"
        elif removed and not added:
            scope_change = "narrowed"
        elif added and removed:
            scope_change = "restructured"
        else:
            scope_change = "same"
        structural = {
            "has_diffs": True,
            "v_curr_lines": curr_stats.get("total_lines", 0),
            "v_next_lines": next_stats.get("total_lines", 0),
            "line_delta": next_stats.get("total_lines", 0) - curr_stats.get("total_lines", 0),
            "v_curr_files": len(curr_files),
            "v_next_files": len(next_files),
            "files_added": len(added),
            "files_removed": len(removed),
            "scope_change": scope_change,
        }

    # 5. Time delta: last review on v_curr → first patch submission on v_next
    time_delta_hours = None
    if reviews:
        review_dates = [parse_email_datetime(r.date) for r in reviews]
        review_dates = [d for d in review_dates if d]
        submission_dates = [
            parse_email_datetime(m.date)
            for m in next_msgs
            if is_patch_submission(m)
        ]
        submission_dates = [d for d in submission_dates if d]
        if review_dates and submission_dates:
            last_review = max(review_dates)
            first_submission = min(submission_dates)
            delta = first_submission - last_review
            time_delta_hours = round(delta.total_seconds() / 3600, 1)

    # 6. Responsiveness
    responsiveness = estimate_feedback_responsiveness(
        list(set(all_feedback_cats)), changelog_cats, changelog_text,
    )

    return {
        "bug_id": bug.bug_id,
        "bug_title": bug.title,
        "from_version": from_version,
        "to_version": to_version,
        "num_reviews": len(reviews),
        "num_reviewers": len(reviewer_names),
        "reviewer_names": sorted(reviewer_names),
        "feedback_categories": sorted(set(all_feedback_cats)),
        "feedback_category_counts": dict(feedback_counter),
        "actionable_snippets": all_snippets,
        "changelog_text": changelog_text,
        "changelog_bullets": changelog_bullets,
        "changelog_categories": changelog_cats,
        "structural_delta": structural,
        "time_to_next_version_hours": time_delta_hours,
        "responsiveness": responsiveness,
    }


# ─── Analyzer class ───────────────────────────────────────────────────────


def group_by_version(patch_versions: list[Discussion]) -> list[tuple[int, list[Discussion]]]:
    """
    Group discussions by patch_version number.

    Multiple Discussion objects can share the same patch_version (e.g.,
    multiple threads in a patch series). This merges them so we analyze
    transitions between distinct version numbers only.

    Returns sorted list of (version_number, [discussions]).
    """
    groups: dict[int, list[Discussion]] = defaultdict(list)
    for d in patch_versions:
        if d.patch_version is not None:
            groups[d.patch_version].append(d)
    return sorted(groups.items())


def merged_messages(discussions: list[Discussion]) -> list[Message]:
    """Merge messages from multiple discussions of the same version."""
    all_msgs = []
    for d in discussions:
        all_msgs.extend(d.messages)
    return all_msgs


def _safe_mean(values: list) -> Optional[float]:
    """Mean of non-None values, or None if empty."""
    valid = [v for v in values if v is not None]
    return round(sum(valid) / len(valid), 3) if valid else None


def _safe_median(values: list) -> Optional[float]:
    """Median of non-None values, or None if empty."""
    valid = [v for v in values if v is not None]
    return round(median(valid), 1) if valid else None


class PatchEvolutionAnalyzer(BaseAnalyzer):
    """Trace the causal chain from reviewer feedback to patch changes."""

    @property
    def name(self) -> str:
        return "Patch Evolution Causal Analysis"

    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        multi_version = [b for b in bugs if b.has_multiple_versions]

        # ── Phase 1: Per-transition analysis ──────────────────────────
        all_transitions = []
        per_bug_summaries = []

        for bug in multi_version:
            pvs = bug.patch_versions
            version_groups = group_by_version(pvs)
            if len(version_groups) < 2:
                continue

            bug_transitions = []
            for i in range(len(version_groups) - 1):
                from_ver, from_discs = version_groups[i]
                to_ver, to_discs = version_groups[i + 1]
                record = analyze_transition(
                    bug, from_ver, to_ver, from_discs, to_discs,
                )
                all_transitions.append(record)
                bug_transitions.append(record)

            per_bug_summaries.append({
                "bug_id": bug.bug_id,
                "title": bug.title,
                "num_versions": len(version_groups),
                "num_transitions": len(bug_transitions),
                "total_reviews": sum(t["num_reviews"] for t in bug_transitions),
                "total_feedback_items": sum(
                    len(t["feedback_categories"]) for t in bug_transitions
                ),
                "total_line_delta": sum(
                    t["structural_delta"].get("line_delta", 0)
                    for t in bug_transitions
                    if t["structural_delta"].get("has_diffs")
                ),
                "avg_responsiveness": _safe_mean([
                    t["responsiveness"]["responsiveness_score"]
                    for t in bug_transitions
                ]),
                "total_time_hours": _safe_mean([
                    t["time_to_next_version_hours"]
                    for t in bug_transitions
                ]),
                "all_categories": sorted(set(
                    cat
                    for t in bug_transitions
                    for cat in t["feedback_categories"]
                )),
            })

        # ── Phase 2: Aggregate statistics ─────────────────────────────

        # Iteration count distribution
        version_counter = Counter(s["num_versions"] for s in per_bug_summaries)

        # Per-category impact analysis
        category_stats: dict[str, dict] = defaultdict(lambda: {
            "transitions_with_feedback": 0,
            "transitions_in_changelog": 0,
            "line_deltas": [],
            "response_times": [],
        })

        transitions_with_feedback = 0
        transitions_with_changelog = 0

        for t in all_transitions:
            if t["feedback_categories"]:
                transitions_with_feedback += 1
            if t["changelog_text"]:
                transitions_with_changelog += 1

            has_diffs = t["structural_delta"].get("has_diffs", False)
            line_delta = abs(t["structural_delta"].get("line_delta", 0)) if has_diffs else None
            time_hours = t["time_to_next_version_hours"]

            for cat in t["feedback_categories"]:
                stats = category_stats[cat]
                stats["transitions_with_feedback"] += 1
                if line_delta is not None:
                    stats["line_deltas"].append(line_delta)
                if time_hours is not None:
                    stats["response_times"].append(time_hours)

            for cat in t["changelog_categories"]:
                category_stats[cat]["transitions_in_changelog"] += 1

        # ── Phase 3: Build output tables ──────────────────────────────

        # Table 1: iteration_transitions
        iteration_transitions_table = []
        for t in all_transitions:
            sd = t["structural_delta"]
            iteration_transitions_table.append({
                "bug_id": t["bug_id"],
                "from_version": t["from_version"],
                "to_version": t["to_version"],
                "num_reviews": t["num_reviews"],
                "num_reviewers": t["num_reviewers"],
                "feedback_categories": ", ".join(t["feedback_categories"]),
                "changelog_categories": ", ".join(t["changelog_categories"]),
                "line_delta": sd.get("line_delta", "") if sd.get("has_diffs") else "",
                "files_added": sd.get("files_added", "") if sd.get("has_diffs") else "",
                "files_removed": sd.get("files_removed", "") if sd.get("has_diffs") else "",
                "scope_change": sd.get("scope_change", ""),
                "time_to_next_hours": t["time_to_next_version_hours"] or "",
                "responsiveness_score": (
                    t["responsiveness"]["responsiveness_score"]
                    if t["responsiveness"]["responsiveness_score"] is not None
                    else ""
                ),
                "changelog_excerpt": t["changelog_text"][:200],
            })

        # Table 2: feedback_impact
        feedback_impact_table = []
        total_transitions = len(all_transitions)
        for cat in sorted(REVISION_CATEGORIES.keys()):
            stats = category_stats.get(cat)
            if not stats or stats["transitions_with_feedback"] == 0:
                continue
            n = stats["transitions_with_feedback"]
            avg_delta = _safe_mean(stats["line_deltas"])
            avg_time = _safe_mean(stats["response_times"])
            in_changelog = stats["transitions_in_changelog"]
            alignment = round(in_changelog / n * 100, 1) if n else 0

            feedback_impact_table.append({
                "category": cat,
                "num_transitions": n,
                "pct_of_transitions": f"{n / total_transitions * 100:.1f}%",
                "avg_abs_line_delta": avg_delta if avg_delta is not None else "",
                "avg_response_time_hours": avg_time if avg_time is not None else "",
                "changelog_alignment_rate": f"{alignment}%",
            })

        # Sort by frequency
        feedback_impact_table.sort(key=lambda x: x["num_transitions"], reverse=True)

        # Table 3: evolution_summary
        evolution_summary_table = []
        for s in sorted(per_bug_summaries, key=lambda x: -x["num_versions"]):
            evolution_summary_table.append({
                "bug_id": s["bug_id"],
                "title": s["title"][:80],
                "num_versions": s["num_versions"],
                "total_reviews": s["total_reviews"],
                "total_feedback_items": s["total_feedback_items"],
                "total_line_delta": s["total_line_delta"],
                "avg_responsiveness": s["avg_responsiveness"] or "",
                "total_time_hours": s["total_time_hours"] or "",
                "categories": ", ".join(s["all_categories"]),
            })

        # Table 4: response_patterns
        response_patterns_table = []
        for cat in sorted(REVISION_CATEGORIES.keys()):
            stats = category_stats.get(cat)
            if not stats or stats["transitions_with_feedback"] == 0:
                continue
            n_feedback = stats["transitions_with_feedback"]
            n_changelog = stats["transitions_in_changelog"]
            addressed_rate = round(n_changelog / n_feedback * 100, 1) if n_feedback else 0
            avg_change = _safe_mean(stats["line_deltas"])
            med_time = _safe_median(stats["response_times"])

            response_patterns_table.append({
                "category": cat,
                "times_in_feedback": n_feedback,
                "times_in_changelog": n_changelog,
                "addressed_rate": f"{addressed_rate}%",
                "avg_structural_change": avg_change if avg_change is not None else "",
                "median_response_time_hours": med_time if med_time is not None else "",
            })

        response_patterns_table.sort(
            key=lambda x: x["times_in_feedback"], reverse=True,
        )

        # ── Phase 4: Summary ──────────────────────────────────────────

        all_responsiveness = [
            t["responsiveness"]["responsiveness_score"]
            for t in all_transitions
            if t["responsiveness"]["responsiveness_score"] is not None
        ]

        # Find most impactful category (highest avg line delta)
        most_impactful = max(
            feedback_impact_table,
            key=lambda x: x["avg_abs_line_delta"] if x["avg_abs_line_delta"] != "" else 0,
            default=None,
        )

        summary = {
            "Multi-version bugs analyzed": len(per_bug_summaries),
            "Total transitions analyzed": len(all_transitions),
            "Transitions with reviewer feedback": transitions_with_feedback,
            "Transitions with changelog notes": transitions_with_changelog,
            "Avg feedback categories per transition": _safe_mean([
                len(t["feedback_categories"]) for t in all_transitions
            ]),
            "Avg responsiveness score": _safe_mean(all_responsiveness),
            "Iteration count distribution": dict(version_counter.most_common()),
            "Most impactful feedback category (by structural change)": (
                f"{most_impactful['category']} (avg {most_impactful['avg_abs_line_delta']} lines)"
                if most_impactful and most_impactful["avg_abs_line_delta"] != ""
                else "N/A"
            ),
        }

        return AnalysisResult(
            name=self.name,
            summary=summary,
            details=per_bug_summaries[:100],
            tables={
                "iteration_transitions": iteration_transitions_table,
                "feedback_impact": feedback_impact_table,
                "evolution_summary": evolution_summary_table,
                "response_patterns": response_patterns_table,
            },
        )

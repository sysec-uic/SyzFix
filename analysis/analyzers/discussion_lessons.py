"""
Analyzer: Lessons from human discussion on kernel patches.

Computes high-level statistics about review patterns:
- Reviewer participation (who reviews, how many per bug)
- Discussion depth (number of messages, back-and-forth)
- Common feedback themes across all discussions
- Resolution patterns (Reviewed-by, Acked-by, etc.)
- Subsystem-level analysis
"""

import re
from collections import Counter, defaultdict
from typing import Any

from ..loader import BugEntry, Discussion, Message
from ..filters import get_human_reviews, is_bot_message, get_review_text
from .base import BaseAnalyzer, AnalysisResult


def count_human_reviewers(discussion: Discussion) -> set[str]:
    """Count distinct human reviewers in a discussion."""
    reviewers = set()
    for msg in discussion.messages:
        if not is_bot_message(msg) and msg.is_reply:
            reviewers.add(msg.sender_email)
    return reviewers


def extract_tags(text: str) -> list[str]:
    """Extract review tags from text (Reviewed-by, Acked-by, etc.)."""
    tags = []
    for tag in ['Reviewed-by', 'Acked-by', 'Tested-by', 'Reported-by',
                'Suggested-by', 'Fixes']:
        if re.search(rf'\b{tag}\b', text, re.I):
            tags.append(tag)
    return tags


def extract_subsystem_from_title(title: str) -> str:
    """Try to extract subsystem from bug title or patch subject."""
    # Common kernel subsystem prefixes in bug titles
    # e.g., "KASAN: slab-use-after-free Read in ..." -> look at function
    # e.g., "[PATCH] net/smc: fix ..." -> "net/smc"
    m = re.match(r'\[PATCH[^\]]*\]\s*(\S+):', title)
    if m:
        return m.group(1).lower()
    return ""


class DiscussionLessonsAnalyzer(BaseAnalyzer):
    """Analyze lessons from human discussion on kernel patches."""

    @property
    def name(self) -> str:
        return "Discussion Lessons"

    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        reviewer_counts = []       # number of distinct reviewers per bug
        top_reviewers = Counter()  # global reviewer leaderboard
        discussion_depths = []     # total human review messages per bug
        resolution_tags = Counter()
        version_distribution = Counter()
        bugs_by_version_count = Counter()

        # Feedback theme analysis across ALL discussions
        theme_counter = Counter()

        # For subsystem analysis
        subsystem_stats: dict[str, dict] = defaultdict(lambda: {
            "count": 0, "total_versions": 0, "total_reviews": 0
        })

        bugs_with_disc = 0
        total_review_msgs = 0

        # Track discussion duration
        response_times = []

        for bug in bugs:
            discussions = bug.discussions
            if not discussions:
                continue

            all_reviewers = set()
            all_reviews = []
            bug_has_discussion = False

            for disc in discussions:
                if disc.patch_version is not None:
                    version_distribution[disc.patch_version] += 1

                reviews = get_human_reviews(disc.messages)
                if reviews:
                    bug_has_discussion = True

                for review in reviews:
                    all_reviewers.add(review.sender_email)
                    top_reviewers[review.sender_name] += 1
                    all_reviews.append(review)

                    # Classify review themes
                    text = get_review_text(review)
                    from .revision_reasons import classify_review_text
                    themes = classify_review_text(text)
                    for theme in themes:
                        theme_counter[theme] += 1

                # Check resolution tags in ALL messages (including bot)
                for msg in disc.messages:
                    tags = extract_tags(msg.body)
                    for tag in tags:
                        resolution_tags[tag] += 1

            if bug_has_discussion:
                bugs_with_disc += 1
                reviewer_counts.append(len(all_reviewers))
                discussion_depths.append(len(all_reviews))
                total_review_msgs += len(all_reviews)

            bugs_by_version_count[bug.num_patch_versions] += 1

            # Subsystem analysis
            subsys = bug.subsystem_guess
            if subsys and subsys != "unknown":
                # Use top-level dir only
                top_dir = subsys.split(',')[0].split('/')[0]
                subsystem_stats[top_dir]["count"] += 1
                subsystem_stats[top_dir]["total_versions"] += bug.num_patch_versions
                subsystem_stats[top_dir]["total_reviews"] += len(all_reviews)

        # Compute summary stats
        avg_reviewers = sum(reviewer_counts) / len(reviewer_counts) if reviewer_counts else 0
        avg_depth = sum(discussion_depths) / len(discussion_depths) if discussion_depths else 0

        # Reviewer count distribution
        reviewer_dist = Counter(reviewer_counts)

        summary = {
            "Total bugs": len(bugs),
            "Bugs with human discussion": bugs_with_disc,
            "Total human review messages": total_review_msgs,
            "Avg reviewers per discussed bug": round(avg_reviewers, 2),
            "Avg review messages per discussed bug": round(avg_depth, 2),
            "Distinct human reviewers": len(top_reviewers),
        }

        # Tables
        top_reviewer_table = [
            {"reviewer": name, "review_count": count}
            for name, count in top_reviewers.most_common(30)
        ]

        theme_table = [
            {"theme": theme, "count": count,
             "pct": f"{count / total_review_msgs * 100:.1f}%" if total_review_msgs else "0%"}
            for theme, count in theme_counter.most_common()
        ]

        version_table = [
            {"num_versions": v, "bug_count": c}
            for v, c in sorted(bugs_by_version_count.items())
        ]

        resolution_table = [
            {"tag": tag, "count": count}
            for tag, count in resolution_tags.most_common()
        ]

        reviewer_dist_table = [
            {"num_reviewers": k, "bug_count": v}
            for k, v in sorted(reviewer_dist.items())
        ]

        # Top subsystems by bug count
        subsystem_table = sorted(
            [{"subsystem": k, **v} for k, v in subsystem_stats.items()],
            key=lambda x: -x["count"]
        )[:20]

        return AnalysisResult(
            name=self.name,
            summary=summary,
            tables={
                "top_reviewers": top_reviewer_table,
                "feedback_themes": theme_table,
                "version_distribution": version_table,
                "resolution_tags": resolution_table,
                "reviewer_distribution": reviewer_dist_table,
                "top_subsystems": subsystem_table,
            },
        )

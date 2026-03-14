"""
Analyzer: Why do patches need revision?

For each bug with 2+ patch versions, examines the v1 discussion to classify
WHY a revision was needed, using keyword/pattern matching on reviewer feedback.
"""

import re
from collections import Counter, defaultdict
from typing import Any

from ..loader import BugEntry
from ..filters import get_human_reviews, get_review_text
from .base import BaseAnalyzer, AnalysisResult

# ─── Revision reason taxonomy ───────────────────────────────────────────────
#
# Each category has a list of regex patterns to match against review text.
# Patterns are applied to the stripped (non-quoted) review text of v1 discussion.

REVISION_CATEGORIES: dict[str, list[re.Pattern]] = {
    "correctness": [
        re.compile(r'\b(wrong|incorrect|bug|broken|doesn.t fix|still broken|doesn.t work|logic error|typo in.*code)\b', re.I),
        re.compile(r'\b(doesn.t compile|build error|fails? to|won.t work|that.s not right)\b', re.I),
        re.compile(r'\b(off[- ]by[- ]one|overflow|underflow|signedness)\b', re.I),
    ],
    "incomplete_fix": [
        re.compile(r'\b(missing|also need|what about|doesn.t handle|edge case|corner case|incomplete)\b', re.I),
        re.compile(r'\b(another path|other caller|not covered|you also need|forgot|overlooked)\b', re.I),
        re.compile(r'\b(partial|only handles|but what if|how about)\b', re.I),
    ],
    "race_condition": [
        re.compile(r'\b(race|rac[ey]|lock|deadlock|atomic|concurrent|TOCTOU|ordering|barrier)\b', re.I),
        re.compile(r'\b(spin_lock|mutex|rcu|synchroniz|preempt|irq.?safe)\b', re.I),
    ],
    "error_handling": [
        re.compile(r'\b(error path|error handling|return value|cleanup|leak on error)\b', re.I),
        re.compile(r'\b(error case|failure path|bail out|unwind|goto err|goto out)\b', re.I),
        re.compile(r'\b(missing.*check|unchecked|null check|check.*return)\b', re.I),
    ],
    "style_convention": [
        re.compile(r'\b(style|naming|nit:|nitpick|coding style|checkpatch)\b', re.I),
        re.compile(r'\b(whitespace|indent|indentation|bracket|brace|spacing|alignment)\b', re.I),
        re.compile(r'\b(s/\S+/\S+/|rename|should be called|better name)\b', re.I),
        re.compile(r'\b(cosmetic|cleanup|clean up|tidy)\b', re.I),
    ],
    "commit_message": [
        re.compile(r'\b(commit message|commit log|changelog|subject line)\b', re.I),
        re.compile(r'\b(Fixes:?\s*tag|Fixes:?\s*line|add.*Fixes|missing.*Fixes)\b', re.I),
        re.compile(r'\b(Reported-by|Signed-off-by|Reviewed-by|Cc:?\s*stable|Closes:)\b', re.I),
        re.compile(r'\b(commit description|patch description|cover letter)\b', re.I),
    ],
    "performance": [
        re.compile(r'\b(performance|overhead|slow|expensive|hot path|fast path|scalab)\b', re.I),
        re.compile(r'\b(cache|contention|latency|throughput|bottleneck|efficient)\b', re.I),
        re.compile(r'\b(optimization|optimize|O\(n\)|O\(1\)|unnecessary.*alloc)\b', re.I),
    ],
    "api_design": [
        re.compile(r'\b(API|interface|refactor|restructure|approach|instead)\b', re.I),
        re.compile(r'\b(better way|cleaner|more elegant|redesign|rethink|rework)\b', re.I),
        re.compile(r'\b(abstraction|encapsulat|decouple|modular)\b', re.I),
    ],
    "scope": [
        re.compile(r'\b(scope|separate patch|split|too much|unrelated change|out of scope)\b', re.I),
        re.compile(r'\b(patch.*series|break.*up|factor.*out|standalone)\b', re.I),
    ],
    "memory_safety": [
        re.compile(r'\b(use.after.free|memory leak|double free|buffer overflow|out of bounds)\b', re.I),
        re.compile(r'\b(null pointer|null deref|kfree|kzalloc|kmalloc|slab)\b', re.I),
        re.compile(r'\b(reference count|refcount|put.*without.*get|dangling)\b', re.I),
    ],
    "documentation": [
        re.compile(r'\b(comment|document|kernel-doc|kdoc|kerneldoc)\b', re.I),
        re.compile(r'\b(add.*comment|explain|describe|clarify.*in.*code)\b', re.I),
    ],
    "config_build": [
        re.compile(r'\b(config|kconfig|build|compile|allmodconfig|allyesconfig)\b', re.I),
        re.compile(r'\b(ifdef|IS_ENABLED|depends on|select|module)\b', re.I),
    ],
}


def classify_review_text(text: str) -> list[str]:
    """Classify a piece of review text into zero or more categories."""
    categories = []
    for cat_name, patterns in REVISION_CATEGORIES.items():
        for pattern in patterns:
            if pattern.search(text):
                categories.append(cat_name)
                break
    return categories


def analyze_bug_revision(bug: BugEntry) -> dict[str, Any]:
    """Analyze why a specific multi-version bug needed revision."""
    pvs = bug.patch_versions
    if len(pvs) < 2:
        return {}

    # Get v1 discussion
    v1 = pvs[0]
    reviews = get_human_reviews(v1.messages)

    all_categories = []
    review_snippets = []

    for review in reviews:
        text = get_review_text(review)
        cats = classify_review_text(text)
        all_categories.extend(cats)
        if cats:
            review_snippets.append({
                "from": review.sender_name,
                "categories": cats,
                "snippet": text[:500],
            })

    # Also check the v2 commit message for "change since v1" notes
    if len(pvs) >= 2:
        v2 = pvs[1]
        for msg in v2.messages:
            if re.search(r'\[PATCH', msg.subject) and not msg.subject.strip().lower().startswith('re:'):
                # This is the v2 patch submission itself
                changelog = re.search(
                    r'(?:change(?:s)?\s+since\s+v\d+|v\d+\s*(?:->|→)\s*v\d+)[:\s]*\n(.*?)(?:\n\s*\n|\n\s*\S+/\S+\s*\|)',
                    msg.body, re.I | re.S
                )
                if changelog:
                    changelog_text = changelog.group(1).strip()
                    cats = classify_review_text(changelog_text)
                    all_categories.extend(cats)
                    if cats:
                        review_snippets.append({
                            "from": "[changelog]",
                            "categories": cats,
                            "snippet": changelog_text[:500],
                        })

    unique_cats = list(set(all_categories))

    # Classify meta-reason
    if not unique_cats and reviews:
        meta = "unclassified_with_discussion"
    elif not unique_cats and not reviews:
        meta = "self_revision"
    else:
        meta = "classified"

    return {
        "bug_id": bug.bug_id,
        "title": bug.title,
        "num_versions": bug.num_patch_versions,
        "categories": unique_cats,
        "meta": meta,
        "num_reviews": len(reviews),
        "snippets": review_snippets,
    }


class RevisionReasonsAnalyzer(BaseAnalyzer):
    """Analyze why kernel patches need revision."""

    @property
    def name(self) -> str:
        return "Patch Revision Reasons"

    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        multi_version = [b for b in bugs if b.has_multiple_versions]

        results = []
        category_counter = Counter()
        meta_counter = Counter()
        category_examples: dict[str, list] = defaultdict(list)
        cooccurrence: dict[tuple, int] = Counter()

        for bug in multi_version:
            result = analyze_bug_revision(bug)
            if not result:
                continue
            results.append(result)

            meta_counter[result["meta"]] += 1
            for cat in result["categories"]:
                category_counter[cat] += 1
                if len(category_examples[cat]) < 5:
                    category_examples[cat].append({
                        "bug_id": result["bug_id"],
                        "title": result["title"],
                        "snippets": result["snippets"],
                    })

            # Track co-occurrences
            cats = sorted(set(result["categories"]))
            for i in range(len(cats)):
                for j in range(i + 1, len(cats)):
                    cooccurrence[(cats[i], cats[j])] += 1

        total = len(multi_version)

        summary = {
            "Total bugs with multiple versions": total,
            "Classified (at least one reason found)": meta_counter.get("classified", 0),
            "Unclassified (discussion but no keyword match)": meta_counter.get("unclassified_with_discussion", 0),
            "Self-revision (no external review)": meta_counter.get("self_revision", 0),
        }

        # Category distribution
        cat_table = []
        for cat, count in category_counter.most_common():
            cat_table.append({
                "category": cat,
                "count": count,
                "pct_of_multi_version": f"{count / total * 100:.1f}%",
            })

        # Top co-occurrences
        cooccur_table = []
        for (c1, c2), count in sorted(cooccurrence.items(), key=lambda x: -x[1])[:15]:
            cooccur_table.append({
                "category_1": c1,
                "category_2": c2,
                "count": count,
            })

        return AnalysisResult(
            name=self.name,
            summary=summary,
            details=results,
            tables={
                "category_distribution": cat_table,
                "cooccurrence": cooccur_table,
                "examples": {cat: exs for cat, exs in category_examples.items()},
            },
        )

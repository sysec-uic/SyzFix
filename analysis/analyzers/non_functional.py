"""
Analyzer: Non-functional revision reasons.

Specifically targets patches that were revised due to non-feature concerns:
- Performance
- Style / formatting / coding conventions
- Commit message hygiene (Fixes tags, Signed-off-by, etc.)
- Build / config issues
- Documentation / comments
"""

import re
from collections import Counter, defaultdict
from typing import Any

from ..loader import BugEntry
from ..filters import get_human_reviews, get_review_text
from .base import BaseAnalyzer, AnalysisResult

# ─── Non-functional categories with more targeted patterns ────────────────

NON_FUNCTIONAL_CATEGORIES = {
    "performance": {
        "description": "Performance concerns (overhead, hot path, scalability)",
        "patterns": [
            re.compile(r'\b(performance|overhead|slow|expensive|hot path|fast path)\b', re.I),
            re.compile(r'\b(scalab|cache.?line|contention|latency|throughput|bottleneck)\b', re.I),
            re.compile(r'\b(optimiz|efficient|inefficient|O\(n\)|O\(1\)|O\(n\^2\))\b', re.I),
            re.compile(r'\b(unnecessary.*(alloc|copy|loop)|avoid.*(alloc|copy))\b', re.I),
            re.compile(r'\b(per-cpu|percpu|batch|bulk|prefetch|inline)\b', re.I),
        ],
    },
    "style": {
        "description": "Coding style and formatting",
        "patterns": [
            re.compile(r'\b(coding style|checkpatch|nit:|nitpick|cosmetic)\b', re.I),
            re.compile(r'\b(whitespace|indent|indentation|tab|spacing|alignment|wrap)\b', re.I),
            re.compile(r'\b(rename|naming|should be called|better name|confusing name)\b', re.I),
            re.compile(r'\bs/\S+/\S+/', re.I),  # sed-style substitution suggestions
            re.compile(r'\b(blank line|newline|line length|80 char|column)\b', re.I),
        ],
    },
    "commit_hygiene": {
        "description": "Commit message and metadata issues",
        "patterns": [
            re.compile(r'\b(commit message|commit log|changelog|subject line|patch title)\b', re.I),
            re.compile(r'\b(Fixes:?\s*tag|add.*Fixes|missing.*Fixes|Fixes:?\s*line)\b', re.I),
            re.compile(r'\b(Signed-off-by|missing.*sign|SOB)\b', re.I),
            re.compile(r'\b(Reported-by|Cc:?\s*stable|Closes:|Link:)\b', re.I),
            re.compile(r'\b(cover letter|patch description|commit description)\b', re.I),
        ],
    },
    "build_config": {
        "description": "Build and configuration issues",
        "patterns": [
            re.compile(r'\b(build|compile|compilation|kconfig|allmodconfig)\b', re.I),
            re.compile(r'\b(ifdef|IS_ENABLED|CONFIG_|depends on|select)\b', re.I),
            re.compile(r'\b(warning:|error:|undefined|undeclared)\b', re.I),
            re.compile(r'\b(header|include|forward decl)\b', re.I),
        ],
    },
    "documentation": {
        "description": "Comments and documentation",
        "patterns": [
            re.compile(r'\b(comment|kernel-doc|kdoc|kerneldoc)\b', re.I),
            re.compile(r'\b(add.*comment|explain.*in.*code|describe|clarify)\b', re.I),
            re.compile(r'\b(documentation|document this|self-explanatory)\b', re.I),
        ],
    },
}


class NonFunctionalAnalyzer(BaseAnalyzer):
    """Analyze non-functional revision reasons."""

    @property
    def name(self) -> str:
        return "Non-Functional Revision Issues"

    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        multi_version = [b for b in bugs if b.has_multiple_versions]

        category_counter = Counter()
        category_examples: dict[str, list] = defaultdict(list)
        bugs_with_nonfunc = set()
        pure_nonfunc = []  # bugs revised ONLY for non-functional reasons

        from .revision_reasons import REVISION_CATEGORIES

        # Define which top-level categories are "functional"
        functional_cats = {
            "correctness", "incomplete_fix", "race_condition",
            "error_handling", "memory_safety",
        }

        for bug in multi_version:
            pvs = bug.patch_versions
            if len(pvs) < 2:
                continue

            v1 = pvs[0]
            reviews = get_human_reviews(v1.messages)

            bug_nonfunc_cats = set()
            bug_func_cats = set()

            for review in reviews:
                text = get_review_text(review)

                # Check non-functional categories
                for cat_name, cat_info in NON_FUNCTIONAL_CATEGORIES.items():
                    for pattern in cat_info["patterns"]:
                        if pattern.search(text):
                            bug_nonfunc_cats.add(cat_name)
                            category_counter[cat_name] += 1
                            if len(category_examples[cat_name]) < 5:
                                category_examples[cat_name].append({
                                    "bug_id": bug.bug_id,
                                    "title": bug.title,
                                    "reviewer": review.sender_name,
                                    "snippet": text[:400],
                                })
                            break

                # Also check if there are functional reasons
                from .revision_reasons import classify_review_text
                func_themes = classify_review_text(text)
                for t in func_themes:
                    if t in functional_cats:
                        bug_func_cats.add(t)

            if bug_nonfunc_cats:
                bugs_with_nonfunc.add(bug.bug_id)
                if not bug_func_cats:
                    pure_nonfunc.append({
                        "bug_id": bug.bug_id,
                        "title": bug.title,
                        "nonfunc_categories": list(bug_nonfunc_cats),
                    })

        total = len(multi_version)

        summary = {
            "Total multi-version bugs": total,
            "Bugs with non-functional feedback": len(bugs_with_nonfunc),
            "Pct with non-functional feedback": f"{len(bugs_with_nonfunc) / total * 100:.1f}%" if total else "0%",
            "Bugs revised PURELY for non-functional reasons": len(pure_nonfunc),
            "Pct pure non-functional": f"{len(pure_nonfunc) / total * 100:.1f}%" if total else "0%",
        }

        cat_table = []
        for cat_name, cat_info in NON_FUNCTIONAL_CATEGORIES.items():
            count = category_counter.get(cat_name, 0)
            cat_table.append({
                "category": cat_name,
                "description": cat_info["description"],
                "review_messages_matching": count,
                "bugs_with_examples": len(category_examples.get(cat_name, [])),
            })
        cat_table.sort(key=lambda x: -x["review_messages_matching"])

        return AnalysisResult(
            name=self.name,
            summary=summary,
            details=pure_nonfunc[:20],  # top 20 pure non-functional revisions
            tables={
                "category_breakdown": cat_table,
                "examples": {cat: exs for cat, exs in category_examples.items()},
            },
        )

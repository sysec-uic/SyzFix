"""
Noise filtering for kernel mailing list discussions.

Kernel bug discussion threads are extremely noisy — they contain automated
bot messages, stable-review backport emails, build test results, and
boilerplate syzbot reports. This module provides functions to classify
messages and extract only substantive human review feedback.
"""

import re
from .loader import Message

# ─── Bot / automated sender patterns ────────────────────────────────────────

BOT_PATTERNS = [
    re.compile(r'syzbot', re.I),
    re.compile(r'patchwork-bot', re.I),
    re.compile(r'kernel test robot', re.I),
    re.compile(r'lkp@intel\.com', re.I),
    re.compile(r'tip-bot2', re.I),
    re.compile(r'noreply', re.I),
    re.compile(r'bot@', re.I),
]

# Sasha Levin's automated stable backport messages
STABLE_BOT_PATTERN = re.compile(r'This is an automated email', re.I)

# Stable review cycle messages
STABLE_REVIEW_PATTERN = re.compile(
    r'(stable review cycle|review patch.*queued.*stable)',
    re.I,
)

# Build result only messages (e.g., "Build results: total: 175 pass: 175 fail: 0")
BUILD_RESULT_ONLY = re.compile(
    r'^Build results:\s*total:\s*\d+\s*pass:\s*\d+\s*fail:\s*\d+',
    re.MULTILINE,
)


def is_bot_message(msg: Message) -> bool:
    """Check if a message is from a known bot/automated sender."""
    for pattern in BOT_PATTERNS:
        if pattern.search(msg.from_addr):
            return True
    # Sasha Levin's automated stable backport messages
    if 'sashal@kernel.org' in msg.from_addr:
        if STABLE_BOT_PATTERN.search(msg.body[:300]):
            return True
    return False


def is_stable_review(msg: Message) -> bool:
    """Check if this is a stable review cycle message (not original review)."""
    return bool(STABLE_REVIEW_PATTERN.search(msg.body[:500]))


def is_trivial_tag_only(msg: Message) -> bool:
    """Check if message is just a tag (Reviewed-by, Acked-by, etc.) with no substance."""
    text = msg.original_text
    if len(text) > 300:
        return False
    tags = re.findall(
        r'(Reviewed-by|Acked-by|Tested-by|LGTM|Applied|Thanks)',
        text, re.I
    )
    # If the message is short and mostly tags, it's trivial
    non_empty_lines = [l.strip() for l in text.split('\n') if l.strip()]
    if len(non_empty_lines) <= 3 and tags:
        return True
    return False


def is_patch_submission(msg: Message) -> bool:
    """Check if this message is a patch submission (not a review)."""
    subj = msg.subject
    # [PATCH], [PATCH v2], [PATCH 1/3], etc.
    if re.search(r'\[PATCH', subj, re.I):
        if not subj.strip().lower().startswith('re:'):
            return True
    return False


def is_syzbot_report(msg: Message) -> bool:
    """Check if this is a syzbot bug report."""
    return 'syzbot' in msg.from_addr.lower()


def is_human_review(msg: Message) -> bool:
    """
    Check if this message is a substantive human code review.

    Must be:
    - Not from a bot
    - Not a stable review cycle message
    - Not just tags
    - Not a patch submission itself
    - Has meaningful original (non-quoted) content
    """
    if is_bot_message(msg):
        return False
    if is_stable_review(msg):
        return False
    if is_patch_submission(msg):
        return False
    if is_trivial_tag_only(msg):
        return False

    # Must have some original text
    text = msg.original_text
    non_empty = [l for l in text.split('\n') if l.strip()]
    if len(non_empty) < 2:
        return False

    return True


def get_human_reviews(messages: list[Message]) -> list[Message]:
    """Filter a list of messages to only substantive human reviews."""
    return [m for m in messages if is_human_review(m)]


def strip_quoted_text(body: str) -> str:
    """Remove quoted lines (starting with >) and signature blocks."""
    lines = body.split('\n')
    result = []
    for line in lines:
        # Stop at signature delimiter
        if line.strip() == '--' or line.strip() == '-- ':
            break
        if not line.startswith('>'):
            result.append(line)
    return '\n'.join(result).strip()


def get_review_text(msg: Message) -> str:
    """Get the substantive review text from a message (no quotes, no sigs)."""
    return strip_quoted_text(msg.body)

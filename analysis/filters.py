"""
Noise filtering for kernel mailing list discussions.

Kernel bug discussion threads are extremely noisy — they contain automated
bot messages, stable-review backport emails, build test results, and
boilerplate syzbot reports. This module provides functions to classify
messages and extract only substantive human review feedback.
"""

import re
from dataclasses import dataclass
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


# ─── Stack trace parsing ──────────────────────────────────────────────────

# Matches kernel stack trace entries like:
#   [<ffffffff81234567>] function_name+0x1a/0x30 path/to/file.c:123
#   function_name+0x1a/0x30 path/to/file.c:123
#   function_name+0x1a/0x30
# Also handles inline annotations:
#   func1 include/linux/foo.h:45 [inline]
#   func2+0x1a/0x30 net/core/bar.c:678
_STACK_FRAME_RE = re.compile(
    r'(?:\[<[0-9a-f]+>\]\s*)?'               # optional addr
    r'(\w+)\+'                                 # function name
    r'0x[0-9a-f]+/0x[0-9a-f]+'               # offset/size
    r'(?:\s+(\S+\.(?:c|h|S)):(\d+))?'        # optional file:line
)

_INLINE_FRAME_RE = re.compile(
    r'(\w+)\s+(\S+\.(?:c|h|S)):(\d+)\s+\[inline\]'
)


@dataclass
class StackFrame:
    """A single frame in a kernel stack trace."""
    function: str
    file: str      # may be empty if not available
    line: int      # 0 if not available
    is_inline: bool = False


def parse_stack_trace(crash_report: str) -> list[StackFrame]:
    """Parse kernel stack trace from a crash report.

    Returns a list of StackFrame objects in order from top (crash site)
    to bottom of the call stack.
    """
    if not crash_report:
        return []

    frames = []
    seen = set()

    for line in crash_report.split("\n"):
        # Try inline frame first
        m = _INLINE_FRAME_RE.search(line)
        if m:
            func, filepath, lineno = m.group(1), m.group(2), int(m.group(3))
            key = (func, filepath, lineno)
            if key not in seen:
                seen.add(key)
                frames.append(StackFrame(func, filepath, lineno, is_inline=True))
            continue

        # Try regular stack frame
        m = _STACK_FRAME_RE.search(line)
        if m:
            func = m.group(1)
            filepath = m.group(2) or ""
            lineno = int(m.group(3)) if m.group(3) else 0
            key = (func, filepath, lineno)
            if key not in seen:
                seen.add(key)
                frames.append(StackFrame(func, filepath, lineno))

    return frames

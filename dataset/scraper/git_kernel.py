"""Scraper for git.kernel.org patch diffs."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from .. import config
from ..models import BugEntry
from ..utils import RateLimitedClient

logger = logging.getLogger(__name__)


def _build_patch_url_from_commit_link(commit_link: str) -> str | None:
    """
    Convert a git.kernel.org commit link to a patch download link.

    Input:  https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=abc123
    Output: https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/patch/?id=abc123
    """
    if not commit_link or "git.kernel.org" not in commit_link:
        return None
    return commit_link.replace("/commit/?id=", "/patch/?id=")


def _build_patch_url_from_hash(commit_hash: str, repo_url: str = "") -> list[str]:
    """
    Build a list of candidate patch URLs for a given commit hash.

    Tries the provided repo first, then falls back to torvalds/linux and other repos.
    """
    urls = []

    # If repo URL is provided, try to build patch URL from it
    if repo_url:
        # repo_url is like: https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git
        # or: git://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git
        clean_repo = repo_url.replace("git://", "https://")
        if "git.kernel.org" in clean_repo:
            urls.append(f"{clean_repo.rstrip('/')}/patch/?id={commit_hash}")

    # Primary: torvalds/linux
    urls.append(config.GIT_KERNEL_PATCH_URL_TEMPLATE.format(hash=commit_hash))

    # Fallback repos
    for tmpl in config.GIT_KERNEL_FALLBACK_REPOS:
        url = tmpl.format(hash=commit_hash)
        if url not in urls:
            urls.append(url)

    return urls


async def fetch_patch_diffs(client: RateLimitedClient, bug: BugEntry) -> int:
    """
    Fetch patch diffs for all fix commits of a bug.

    Returns the number of successfully fetched patches.
    """
    fetched = 0

    for fc in bug.fix_commits:
        if fc.patch_diff:
            fetched += 1
            continue  # already have it

        if not fc.hash and not fc.link:
            logger.warning(f"Bug {bug.bug_id}: fix commit has no hash or link: {fc.title}")
            bug.processing_errors.append(f"No hash/link for fix commit: {fc.title}")
            continue

        # Strategy 1: Convert existing commit link to patch link
        if fc.link:
            patch_url = _build_patch_url_from_commit_link(fc.link)
            if patch_url:
                diff = await client.fetch(patch_url)
                if diff and _looks_like_patch(diff):
                    fc.patch_diff = diff
                    fetched += 1
                    logger.info(f"Bug {bug.bug_id}: fetched patch from commit link: {fc.hash or fc.title}")
                    continue

        # Strategy 2: Try hash with multiple repos
        if fc.hash:
            candidate_urls = _build_patch_url_from_hash(fc.hash, fc.repo)
            for url in candidate_urls:
                diff = await client.fetch(url)
                if diff and _looks_like_patch(diff):
                    fc.patch_diff = diff
                    fetched += 1
                    logger.info(f"Bug {bug.bug_id}: fetched patch for {fc.hash} from {urlparse(url).path}")
                    break
            else:
                logger.warning(f"Bug {bug.bug_id}: could not fetch patch for {fc.hash}")
                bug.processing_errors.append(f"Could not fetch patch for commit {fc.hash}")

    return fetched


def _looks_like_patch(content: str) -> bool:
    """Heuristic check if content looks like a git patch/diff."""
    if not content:
        return False
    # A git patch usually contains "diff --git" or starts with patch headers
    indicators = ["diff --git", "---", "+++", "@@"]
    return any(ind in content[:2000] for ind in indicators)

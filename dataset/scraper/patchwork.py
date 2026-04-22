"""Scraper for patchwork.kernel.org REST API (fallback data source)."""

from __future__ import annotations

import logging
from typing import Optional

from .. import config
from ..models import BugEntry, Discussion, Email, PatchVersion
from ..utils import RateLimitedClient

logger = logging.getLogger(__name__)


async def search_patch_by_hash(
    client: RateLimitedClient,
    commit_hash: str,
) -> list[dict]:
    """
    Search patchwork for patches matching a commit hash.

    Returns list of patch dicts from the patchwork API.
    """
    if not commit_hash:
        return []

    url = f"{config.PATCHWORK_PATCHES_URL}?hash={commit_hash}"
    data = await client.fetch(url, as_json=True)

    if isinstance(data, list):
        return data
    elif isinstance(data, dict) and "results" in data:
        return data["results"]
    return []


async def get_patch_series(
    client: RateLimitedClient,
    series_id: int,
) -> dict | None:
    """Fetch a patch series by ID."""
    url = config.PATCHWORK_SERIES_URL_TEMPLATE.format(series_id=series_id)
    return await client.fetch(url, as_json=True)


async def get_patch_comments(
    client: RateLimitedClient,
    patch_id: int,
) -> list[dict]:
    """Fetch comments for a patch."""
    url = f"{config.PATCHWORK_PATCHES_URL}{patch_id}/comments/"
    data = await client.fetch(url, as_json=True)
    if isinstance(data, list):
        return data
    return []


async def get_series_versions(
    client: RateLimitedClient,
    project_id: int,
    submitter_id: int,
    series_name: str,
) -> list[dict]:
    """
    Find all versions of a patch series by searching for the submitter's
    series in the same project.
    """
    # Search for patches by the same submitter in the same project
    url = (
        f"{config.PATCHWORK_PATCHES_URL}"
        f"?project={project_id}&submitter={submitter_id}"
        f"&order=-date&per_page=100"
    )
    data = await client.fetch(url, as_json=True)
    if not data:
        return []

    patches = data if isinstance(data, list) else data.get("results", [])
    return patches


async def supplement_bug_data(
    client: RateLimitedClient,
    bug: BugEntry,
) -> bool:
    """
    Use patchwork to supplement bug data when lore discussions are insufficient.

    Returns True if any new data was found.
    """
    found_new = False

    for fc in bug.fix_commits:
        if not fc.hash:
            continue

        logger.info(f"Bug {bug.bug_id}: searching patchwork for hash {fc.hash}")
        patches = await search_patch_by_hash(client, fc.hash)

        if not patches:
            logger.debug(f"Bug {bug.bug_id}: no patchwork results for {fc.hash}")
            continue

        for patch in patches:
            patch_id = patch.get("id")
            series_list = patch.get("series", [])
            lore_url = patch.get("list_archive_url", "")
            web_url = patch.get("web_url", "")

            # Check if we already have this discussion
            existing_urls = {d.url for d in bug.discussions}
            if lore_url and lore_url not in existing_urls:
                disc = Discussion(
                    url=lore_url,
                    subject=patch.get("name", ""),
                    patch_version=_extract_version_from_patchwork(patch),
                )
                bug.discussions.append(disc)
                found_new = True
                logger.info(f"Bug {bug.bug_id}: found new discussion via patchwork: {lore_url}")

            # Fetch comments from patchwork
            if patch_id:
                comments = await get_patch_comments(client, patch_id)
                if comments:
                    _add_patchwork_comments_to_bug(bug, patch, comments)
                    found_new = True

            # Try to find other versions via series
            for series_info in series_list:
                series_id = series_info.get("id")
                if series_id:
                    series = await get_patch_series(client, series_id)
                    if series:
                        version = series.get("version", 1)
                        # Check if we have this version already
                        existing_versions = {pv.version for pv in bug.patch_versions}
                        if version not in existing_versions:
                            pv = PatchVersion(
                                version=version,
                                subject=series.get("name", ""),
                                cover_letter=series.get("cover_letter", {}).get("content", "")
                                if series.get("cover_letter")
                                else "",
                            )
                            bug.patch_versions.append(pv)
                            found_new = True

    return found_new


def _extract_version_from_patchwork(patch: dict) -> Optional[int]:
    """Extract patch version from patchwork patch data."""
    name = patch.get("name", "")
    import re
    m = re.search(r'\[.*v(\d+)', name, re.IGNORECASE)
    if m:
        return int(m.group(1))
    if "[PATCH" in name.upper():
        return 1
    return None


def _add_patchwork_comments_to_bug(bug: BugEntry, patch: dict, comments: list[dict]):
    """Add patchwork comments as discussion emails to bug data."""
    patch_name = patch.get("name", "")

    # Find matching discussion or create new one
    disc = None
    for d in bug.discussions:
        if d.subject == patch_name:
            disc = d
            break

    if disc is None:
        disc = Discussion(
            url=patch.get("list_archive_url", patch.get("web_url", "")),
            subject=patch_name,
            patch_version=_extract_version_from_patchwork(patch),
        )
        bug.discussions.append(disc)

    for comment in comments:
        email_obj = Email(
            message_id=comment.get("msgid", ""),
            from_addr=_format_submitter(comment.get("submitter", {})),
            date=comment.get("date", ""),
            subject=comment.get("subject", patch_name),
            body=comment.get("content", ""),
        )
        disc.messages.append(email_obj)


def _format_submitter(submitter: dict) -> str:
    """Format patchwork submitter info."""
    name = submitter.get("name", "")
    email_addr = submitter.get("email", "")
    if name and email_addr:
        return f"{name} <{email_addr}>"
    return email_addr or name

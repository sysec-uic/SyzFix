"""Scraper for syzkaller/syzbot API."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup

from .. import config
from ..models import BugEntry, Crash, Discussion, FixCommit
from ..utils import RateLimitedClient
from ..storage import DataStore, ProgressDB

logger = logging.getLogger(__name__)


def extract_extid_from_link(link: str) -> str:
    """Extract extid from a syzbot bug link like '/bug?extid=abc123' or '/bug?id=abc123'."""
    parsed = urlparse(link)
    params = parse_qs(parsed.query)
    if "extid" in params:
        return params["extid"][0]
    if "id" in params:
        return params["id"][0]
    # Sometimes the link is just an ID
    return link.strip("/").split("/")[-1]


async def fetch_bug_list(client: RateLimitedClient) -> list[dict]:
    """
    Fetch the full list of fixed bugs from syzbot.

    Returns list of dicts with keys: bug_id, title, link, fix_commits.
    """
    logger.info("Fetching syzbot fixed bug list...")
    data = await client.fetch(config.SYZBOT_FIXED_URL, as_json=True, use_cache=False)

    if not data or "Bugs" not in data:
        logger.error("Failed to fetch bug list or unexpected format")
        return []

    bugs = []
    for bug in data["Bugs"]:
        title = bug.get("title", "")
        link = bug.get("link", "")
        extid = extract_extid_from_link(link)

        if not extid:
            logger.warning(f"Could not extract extid from link: {link}")
            continue

        bugs.append({
            "bug_id": extid,
            "title": title,
            "link": link,
            "fix_commits_summary": bug.get("fix-commits", []),
        })

    logger.info(f"Found {len(bugs)} fixed bugs")
    return bugs


async def fetch_bug_details(
    client: RateLimitedClient,
    bug_id: str,
    store: DataStore,
) -> BugEntry | None:
    """
    Fetch detailed data for a single bug from syzbot API.

    Downloads crash reports, reproducers, etc.
    """
    url = config.SYZBOT_BUG_URL_TEMPLATE.format(extid=bug_id)
    logger.info(f"Fetching bug details: {bug_id}")

    data = await client.fetch(url, as_json=True)
    if not data:
        # Some bugs use ?id= instead of ?extid=
        url = config.SYZBOT_BUG_ID_URL_TEMPLATE.format(extid=bug_id)
        data = await client.fetch(url, as_json=True)
    if not data:
        logger.error(f"Failed to fetch bug details for {bug_id}")
        return None

    # Save raw data
    store.save_raw_syzbot(bug_id, data)

    bug = BugEntry(
        bug_id=bug_id,
        title=data.get("title", ""),
        status=data.get("status", ""),
        first_crash=data.get("first-crash", ""),
        last_crash=data.get("last-crash", ""),
        fix_time=data.get("fix-time", ""),
        raw_syzbot_data=data,
    )

    # Parse fix commits
    for fc in data.get("fix-commits", []):
        bug.fix_commits.append(FixCommit(
            hash=fc.get("hash", ""),
            title=fc.get("title", ""),
            link=fc.get("link", ""),
            repo=fc.get("repo", ""),
            branch=fc.get("branch", ""),
            author=fc.get("author", ""),
            author_name=fc.get("author-name", ""),
            date=fc.get("date", ""),
        ))

    # Parse discussions from JSON API (usually just Google Groups link)
    for disc in data.get("discussions", []):
        url_str = disc if isinstance(disc, str) else disc.get("link", disc.get("url", ""))
        subject = disc.get("subject", "") if isinstance(disc, dict) else ""
        is_syzbot = "syzbot" in subject.lower() or "syzbot" in url_str.lower()

        patch_version = None
        if subject:
            m = re.search(r'\[PATCH\s+v(\d+)', subject, re.IGNORECASE)
            if m:
                patch_version = int(m.group(1))
            elif re.search(r'\[PATCH\b', subject, re.IGNORECASE):
                patch_version = 1

        bug.discussions.append(Discussion(
            url=url_str,
            subject=subject,
            patch_version=patch_version,
            is_syzbot_report=is_syzbot,
        ))

    # Also scrape the HTML page for additional lore.kernel.org discussion links
    # The JSON API often only has Google Groups links, but the HTML page
    # has rich discussion links to lore.kernel.org
    html_discussions = await _fetch_html_discussions(client, bug_id)
    existing_urls = {d.url for d in bug.discussions}
    for disc in html_discussions:
        if disc.url not in existing_urls:
            bug.discussions.append(disc)
            existing_urls.add(disc.url)

    # Parse crashes (limit to top N)
    crashes_data = data.get("crashes", [])[:config.MAX_CRASHES_PER_BUG]
    for crash_data in crashes_data:
        crash = Crash(
            title=crash_data.get("title", ""),
            kernel_commit=crash_data.get("kernel-source-commit", ""),
            crash_report_link=crash_data.get("crash-report-link", ""),
            syz_reproducer_link=crash_data.get("syz-reproducer", ""),
            c_reproducer_link=crash_data.get("c-reproducer", ""),
            kernel_config_link=crash_data.get("kernel-config", ""),
        )
        bug.crashes.append(crash)

    # Download crash report and reproducers for the first crash
    if bug.crashes:
        await _download_crash_artifacts(client, bug.crashes[0])

    return bug


async def _download_crash_artifacts(client: RateLimitedClient, crash: Crash):
    """Download crash report, C reproducer, and syz reproducer."""
    if crash.crash_report_link:
        url = _make_syzbot_url(crash.crash_report_link)
        content = await client.fetch(url)
        if content:
            crash.crash_report = content

    if crash.c_reproducer_link:
        url = _make_syzbot_url(crash.c_reproducer_link)
        content = await client.fetch(url)
        if content:
            crash.c_reproducer = content

    if crash.syz_reproducer_link:
        url = _make_syzbot_url(crash.syz_reproducer_link)
        content = await client.fetch(url)
        if content:
            crash.syz_reproducer = content


def _make_syzbot_url(path: str) -> str:
    """Convert a relative syzbot path to a full URL."""
    if path.startswith("http"):
        return path
    return config.SYZBOT_TEXT_URL_TEMPLATE.format(path=path)


async def _fetch_html_discussions(
    client: RateLimitedClient,
    bug_id: str,
) -> list[Discussion]:
    """
    Scrape the syzbot HTML bug page to extract lore.kernel.org discussion links.

    The HTML page has a "Discussions" section with links to lore threads that
    are NOT available in the JSON API.
    """
    html_url = f"{config.SYZBOT_BASE_URL}/bug?extid={bug_id}"
    html = await client.fetch(html_url)
    if not html:
        html_url = f"{config.SYZBOT_BASE_URL}/bug?id={bug_id}"
        html = await client.fetch(html_url)
    if not html:
        return []

    discussions = []
    try:
        soup = BeautifulSoup(html, "lxml")

        # Find all lore.kernel.org links on the page
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "lore.kernel.org" not in href:
                continue

            subject = link.get_text(strip=True)
            is_syzbot = "syzbot" in subject.lower()

            # Detect patch version
            patch_version = None
            m = re.search(r'\[PATCH[^\]]*\s+v(\d+)', subject, re.IGNORECASE)
            if m:
                patch_version = int(m.group(1))
            elif re.search(r'\[PATCH\b', subject, re.IGNORECASE):
                patch_version = 1

            discussions.append(Discussion(
                url=href,
                subject=subject,
                patch_version=patch_version,
                is_syzbot_report=is_syzbot,
            ))

    except Exception as e:
        logger.warning(f"Bug {bug_id}: failed to parse HTML for discussions: {e}")

    if discussions:
        logger.info(
            f"Bug {bug_id}: found {len(discussions)} lore discussion links from HTML page"
        )

    return discussions

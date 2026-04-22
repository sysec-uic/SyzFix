"""Scraper for lore.kernel.org mailing list discussions."""

from __future__ import annotations

import email
import gzip
import io
import logging
import mailbox
import re
import xml.etree.ElementTree as ET
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote

from .. import config
from ..models import BugEntry, Discussion, Email, PatchVersion
from ..utils import RateLimitedClient

logger = logging.getLogger(__name__)


def _decode_header_value(value: str) -> str:
    """Decode an email header value that might have encoded words."""
    if not value:
        return ""
    try:
        decoded_parts = decode_header(value)
        result = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                result.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                result.append(part)
        return " ".join(result)
    except Exception:
        return str(value)


def _get_email_body(msg: email.message.Message) -> str:
    """Extract plain text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback: get first text part
        for part in msg.walk():
            if part.get_content_maintype() == "text":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def _parse_mbox_data(mbox_bytes: bytes) -> list[Email]:
    """Parse gzipped mbox data into a list of Email objects."""
    try:
        decompressed = gzip.decompress(mbox_bytes)
    except gzip.BadGzipFile:
        # Maybe it's not gzipped
        decompressed = mbox_bytes

    emails = []
    mbox_file = io.BytesIO(decompressed)

    # Use mailbox.mbox with a temporary approach
    # We need to write to a temp file since mbox needs a file path
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mbox") as tmp:
        tmp.write(decompressed)
        tmp_path = tmp.name

    try:
        mbox = mailbox.mbox(tmp_path)
        for msg in mbox:
            try:
                email_obj = Email(
                    message_id=msg.get("Message-ID", "") or msg.get("Message-Id", ""),
                    in_reply_to=msg.get("In-Reply-To", ""),
                    from_addr=_decode_header_value(msg.get("From", "")),
                    date=msg.get("Date", ""),
                    subject=_decode_header_value(msg.get("Subject", "")),
                    body=_get_email_body(msg),
                )
                emails.append(email_obj)
            except Exception as e:
                logger.debug(f"Failed to parse email in mbox: {e}")
                continue
        mbox.close()
    finally:
        os.unlink(tmp_path)

    return emails


def _normalize_lore_url(url: str) -> str:
    """Normalize a lore URL to ensure consistent format."""
    # Remove trailing /T/ or /t/ if present
    url = url.rstrip("/")
    url = re.sub(r'/[Tt]/?$', '', url)
    # Remove trailing #u or similar anchors
    url = re.sub(r'#.*$', '', url)
    return url


def _build_mbox_url(lore_url: str) -> str:
    """Build the mbox.gz download URL from a lore thread URL."""
    normalized = _normalize_lore_url(lore_url)
    return f"{normalized}/t.mbox.gz"


def _extract_patch_version(subject: str) -> Optional[int]:
    """Extract patch version number from email subject."""
    if not subject:
        return None
    # Match [PATCH v2], [PATCH v3 1/3], [PATCH RFC v4], etc.
    m = re.search(r'\[PATCH[^\]]*\s+v(\d+)', subject, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Plain [PATCH] without version = v1
    if re.search(r'\[PATCH\b', subject, re.IGNORECASE):
        return 1
    return None


def _extract_diff_from_email(body: str) -> str:
    """Extract patch diff from an email body if it contains an inline patch."""
    if not body:
        return ""

    lines = body.split("\n")
    diff_lines = []
    in_diff = False

    for line in lines:
        if line.startswith("diff --git "):
            in_diff = True
        if in_diff:
            diff_lines.append(line)
            # End of diff heuristic: "-- " line (git signature separator)
            if line.strip() == "--":
                break

    return "\n".join(diff_lines) if diff_lines else ""


async def fetch_discussions(client: RateLimitedClient, bug: BugEntry) -> int:
    """
    Fetch full discussion threads for a bug from lore.kernel.org.

    Downloads mbox for each discussion URL, parses emails.
    Returns the number of successfully fetched discussions.
    """
    fetched = 0

    for disc in bug.discussions:
        if disc.messages:
            fetched += 1
            continue  # already fetched

        if not disc.url:
            continue

        # Skip non-lore URLs (e.g., Google Groups links can't provide mbox)
        if "lore.kernel.org" not in disc.url:
            logger.debug(
                f"Bug {bug.bug_id}: skipping non-lore discussion URL: {disc.url}"
            )
            continue

        mbox_url = _build_mbox_url(disc.url)
        logger.info(f"Bug {bug.bug_id}: fetching discussion mbox from {mbox_url}")

        mbox_data = await client.fetch_bytes(mbox_url)
        if not mbox_data:
            logger.warning(f"Bug {bug.bug_id}: failed to fetch mbox from {mbox_url}")
            bug.processing_errors.append(f"Failed to fetch mbox: {mbox_url}")
            continue

        emails = _parse_mbox_data(mbox_data)
        if not emails:
            logger.warning(f"Bug {bug.bug_id}: no emails parsed from {mbox_url}")
            continue

        if len(emails) > config.MAX_THREAD_EMAILS:
            logger.warning(
                f"Bug {bug.bug_id}: thread has {len(emails)} emails, "
                f"truncating to {config.MAX_THREAD_EMAILS}"
            )
            emails = emails[:config.MAX_THREAD_EMAILS]

        disc.messages = emails

        # Update subject and patch version if not already set
        if not disc.subject and emails:
            disc.subject = emails[0].subject

        if disc.patch_version is None and disc.subject:
            disc.patch_version = _extract_patch_version(disc.subject)

        fetched += 1
        logger.info(
            f"Bug {bug.bug_id}: fetched {len(emails)} emails "
            f"for discussion: {disc.subject[:80]}"
        )

    return fetched


async def search_lore_for_patch(
    client: RateLimitedClient,
    commit_title: str,
    bug: BugEntry,
) -> list[Discussion]:
    """
    Search lore.kernel.org for patch discussions related to a commit title.

    Used as fallback when syzbot discussions field is empty.
    """
    if not commit_title:
        return []

    # Clean up commit title for search
    # Remove common prefixes like "subsys: " and special characters
    search_query = f"s:{commit_title}"
    encoded_query = quote(search_query)
    search_url = config.LORE_SEARCH_URL_TEMPLATE.format(query=encoded_query)

    logger.info(f"Bug {bug.bug_id}: searching lore for: {commit_title[:60]}")

    # Fetch Atom feed
    content = await client.fetch(search_url)
    if not content:
        return []

    discussions = []
    try:
        root = ET.fromstring(content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        for entry in root.findall("atom:entry", ns)[:5]:  # limit to top 5 results
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)

            if title_el is not None and link_el is not None:
                title = title_el.text or ""
                href = link_el.get("href", "")

                if href:
                    patch_ver = _extract_patch_version(title)
                    disc = Discussion(
                        url=href,
                        subject=title,
                        patch_version=patch_ver,
                    )
                    discussions.append(disc)
    except ET.ParseError as e:
        logger.warning(f"Bug {bug.bug_id}: failed to parse Atom feed: {e}")

    return discussions


def build_patch_versions(bug: BugEntry) -> list[PatchVersion]:
    """
    Analyze discussions to build a list of patch versions (v1, v2, v3...).

    Groups discussion emails by patch version and extracts inline diffs.
    """
    version_map: dict[int, PatchVersion] = {}

    for disc in bug.discussions:
        if disc.is_syzbot_report:
            continue  # skip syzbot's own report

        version = disc.patch_version or 0
        if version == 0:
            continue

        if version not in version_map:
            version_map[version] = PatchVersion(
                version=version,
                subject=disc.subject,
            )

        pv = version_map[version]
        for msg in disc.messages:
            pv.discussion.append(msg)

            # Try to extract inline patch diff
            if not pv.diff:
                diff = _extract_diff_from_email(msg.body)
                if diff:
                    pv.diff = diff

            # Check for cover letter (patch 0/N)
            if re.search(r'\[.*0/\d+\]', msg.subject):
                pv.cover_letter = msg.body

    return sorted(version_map.values(), key=lambda x: x.version)

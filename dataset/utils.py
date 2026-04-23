"""HTTP client utilities with rate limiting, retry, and caching."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientTimeout
from aiolimiter import AsyncLimiter

from . import config

logger = logging.getLogger(__name__)


class RateLimitedClient:
    """Async HTTP client with per-domain rate limiting, retry, and disk cache."""

    def __init__(self, cache_dir: Path | None = None):
        self._session: aiohttp.ClientSession | None = None
        self._limiters: dict[str, AsyncLimiter] = {}
        self._semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_REQUESTS)
        self._cache_dir = cache_dir or config.RAW_DIR / ".cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # Initialize rate limiters per domain
        # AsyncLimiter(max_rate, time_period): max_rate requests per time_period seconds
        # For rates < 1 req/s, invert: e.g. 0.5 req/s = 1 req per 2s
        for domain, rate in config.RATE_LIMITS.items():
            if rate >= 1.0:
                self._limiters[domain] = AsyncLimiter(rate, 1.0)
            else:
                self._limiters[domain] = AsyncLimiter(1.0, 1.0 / rate)

    def _get_limiter(self, url: str) -> AsyncLimiter:
        """Get rate limiter for the domain of the given URL."""
        domain = urlparse(url).hostname or ""
        if domain not in self._limiters:
            self._limiters[domain] = AsyncLimiter(1.0, 1.0)  # default 1 req/s
        return self._limiters[domain]

    def _cache_key(self, url: str) -> Path:
        """Generate a cache file path for the given URL."""
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self._cache_dir / f"{url_hash}.cache"

    def _read_cache(self, url: str) -> str | None:
        """Read cached response for URL, or None if not cached."""
        cache_path = self._cache_key(url)
        if cache_path.exists():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                # Cache entries are valid indefinitely (content doesn't change)
                return data.get("body")
            except (json.JSONDecodeError, KeyError):
                cache_path.unlink(missing_ok=True)
        return None

    def _write_cache(self, url: str, body: str) -> None:
        """Write response to disk cache."""
        cache_path = self._cache_key(url)
        cache_data = {
            "url": url,
            "body": body,
            "cached_at": time.time(),
        }
        cache_path.write_text(json.dumps(cache_data, ensure_ascii=False), encoding="utf-8")

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = ClientTimeout(total=config.HTTP_TIMEOUT)
            headers = {"User-Agent": config.HTTP_USER_AGENT}
            self._session = aiohttp.ClientSession(timeout=timeout, headers=headers)
        return self._session

    async def fetch(
        self,
        url: str,
        *,
        use_cache: bool = True,
        as_json: bool = False,
        max_retries: int | None = None,
    ) -> str | dict | None:
        """
        Fetch a URL with rate limiting, retry, and optional caching.

        Returns the response body as string (or dict if as_json=True).
        Returns None on permanent failure.
        """
        # Check cache first
        if use_cache and not as_json:
            cached = self._read_cache(url)
            if cached is not None:
                logger.debug(f"Cache hit: {url}")
                return cached

        retries = max_retries if max_retries is not None else config.HTTP_MAX_RETRIES
        limiter = self._get_limiter(url)

        for attempt in range(retries + 1):
            try:
                async with self._semaphore:
                    await limiter.acquire()
                    session = await self._ensure_session()
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            if as_json:
                                raw = await resp.read()
                                try:
                                    text = raw.decode("utf-8")
                                except UnicodeDecodeError as e:
                                    logger.warning(
                                        f"Non-UTF-8 bytes in JSON response from {url} "
                                        f"({e}); decoding with errors='replace'"
                                    )
                                    text = raw.decode("utf-8", errors="replace")
                                return json.loads(text)
                            else:
                                raw = await resp.read()
                                try:
                                    body = raw.decode("utf-8")
                                except UnicodeDecodeError:
                                    body = raw.decode("utf-8", errors="replace")
                                if use_cache:
                                    self._write_cache(url, body)
                                return body
                        elif resp.status == 404:
                            logger.warning(f"404 Not Found: {url}")
                            return None
                        elif resp.status == 429 or resp.status >= 500:
                            wait = config.HTTP_RETRY_BACKOFF ** (attempt + 1)
                            logger.warning(
                                f"HTTP {resp.status} for {url}, "
                                f"retry {attempt+1}/{retries} in {wait:.1f}s"
                            )
                            await asyncio.sleep(wait)
                        else:
                            logger.error(f"HTTP {resp.status} for {url}")
                            return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                wait = config.HTTP_RETRY_BACKOFF ** (attempt + 1)
                logger.warning(
                    f"Request error for {url}: {e}, "
                    f"retry {attempt+1}/{retries} in {wait:.1f}s"
                )
                await asyncio.sleep(wait)

        logger.error(f"All retries exhausted for {url}")
        return None

    async def fetch_bytes(self, url: str) -> bytes | None:
        """Fetch URL and return raw bytes (for mbox.gz files)."""
        limiter = self._get_limiter(url)
        byte_retries = config.HTTP_MAX_RETRIES

        for attempt in range(byte_retries + 1):
            try:
                async with self._semaphore:
                    await limiter.acquire()
                    session = await self._ensure_session()
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            return await resp.read()
                        elif resp.status == 404:
                            logger.warning(f"404 Not Found: {url}")
                            return None
                        elif resp.status == 429 or resp.status >= 500:
                            wait = config.HTTP_RETRY_BACKOFF ** (attempt + 1)
                            logger.warning(
                                f"HTTP {resp.status} for {url}, "
                                f"retry {attempt+1}/{byte_retries} in {wait:.1f}s"
                            )
                            await asyncio.sleep(wait)
                        else:
                            logger.error(f"HTTP {resp.status} for {url}")
                            return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                wait = config.HTTP_RETRY_BACKOFF ** (attempt + 1)
                logger.warning(f"Request error for {url}: {e}, retry {attempt+1}/{byte_retries}")
                await asyncio.sleep(wait)

        return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

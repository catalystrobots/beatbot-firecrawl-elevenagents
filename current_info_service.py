"""
Safe current-information lookup service for Beatbot.

Uses Google News RSS search with operator-approved domains to gather recent
coverage for topics like game reviews, patch notes, release dates, and tech news.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen
import json
import os
import xml.etree.ElementTree as ET


@dataclass(frozen=True, slots=True)
class CurrentInfoItem:
    """One current-information result."""

    title: str
    source: str
    link: str
    published_at: Optional[datetime] = None
    description: str = ""


class CurrentInfoService:
    """Fetch current topic coverage from a restricted domain allowlist."""

    DEFAULT_ALLOWED_DOMAINS: List[str] = [
        "ign.com",
        "gamespot.com",
        "polygon.com",
        "eurogamer.net",
        "pcgamer.com",
        "theverge.com",
        "engadget.com",
        "steamdeckhq.com",
    ]
    DEFAULT_STATS_ALLOWED_DOMAINS: List[str] = [
        "steamdb.info",
        "steamcharts.com",
        "store.steampowered.com",
        "boxofficemojo.com",
        "the-numbers.com",
        "imdb.com",
    ]

    INTENT_HINTS: Dict[str, str] = {
        "reviews": "review impressions critic review",
        "patch_notes": "patch notes update changelog",
        "release_date": "release date launch window",
        "news": "latest news update",
        "stats": "stats player count concurrent players box office gross revenue budget opening weekend performance today",
        "general": "latest coverage",
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.logger = logging.getLogger(__name__)
        self.config = config or {}
        self.lookup_config = self.config.get("current_info", {})
        self.enabled = bool(self.lookup_config.get("enabled", True))
        self.timeout_seconds = float(self.lookup_config.get("timeout_seconds", 6.0))
        self.freshness_days = int(self.lookup_config.get("freshness_days", 14))
        self.cache_ttl_seconds = int(self.lookup_config.get("cache_ttl_seconds", 600))
        configured_domains = self.lookup_config.get("allowed_domains", self.DEFAULT_ALLOWED_DOMAINS)
        self.allowed_domains = [str(domain).strip() for domain in configured_domains if str(domain).strip()]
        configured_stats_domains = self.lookup_config.get(
            "stats_allowed_domains",
            self.DEFAULT_STATS_ALLOWED_DOMAINS,
        )
        self.stats_allowed_domains = [
            str(domain).strip() for domain in configured_stats_domains if str(domain).strip()
        ]
        self.use_firecrawl = bool(self.lookup_config.get("use_firecrawl", False))
        self.firecrawl_api_key = str(
            self.lookup_config.get("firecrawl_api_key")
            or os.environ.get("FIRECRAWL_API_KEY")
            or ""
        ).strip()
        self._last_query: Optional[str] = None
        self._last_results: List[CurrentInfoItem] = []
        self._cache: Dict[str, tuple[datetime, List[CurrentInfoItem]]] = {}

    def lookup(self, query: str, intent: str = "general", limit: int = 6) -> List[CurrentInfoItem]:
        """Look up recent topic coverage from approved domains only."""
        if not self.enabled:
            self.logger.info("Current info service disabled")
            return []

        query = query.strip()
        if not query:
            return []

        backend = "firecrawl" if self._should_use_firecrawl() else "rss"
        cache_key = f"{backend}:{intent}:{query.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            cached_at, cached_results = cached
            if (datetime.now() - cached_at).total_seconds() <= self.cache_ttl_seconds:
                self._last_query = f"{backend}:{intent}:{query}"
                self._last_results = cached_results[:]
                return cached_results[:limit]

        try:
            if backend == "firecrawl":
                results = self._lookup_with_firecrawl(query=query, intent=intent, limit=limit)
            else:
                results = self._lookup_with_google_news(query=query, intent=intent, limit=limit)
        except Exception as e:
            if backend == "firecrawl":
                self.logger.warning("Firecrawl lookup failed, falling back to RSS: %s", e)
                results = self._lookup_with_google_news(query=query, intent=intent, limit=limit)
                backend = "rss"
                cache_key = f"{backend}:{intent}:{query.lower()}"
            else:
                raise

        self._last_query = f"{backend}:{intent}:{query}"
        self._last_results = results[:]
        self._cache[cache_key] = (datetime.now(), results[:])
        return results

    def get_briefing(self, query: str, intent: str = "general", limit: int = 5) -> str:
        """Format current-information results into a prompt-ready briefing."""
        results = self.lookup(query=query, intent=intent, limit=limit)
        backend_label = self.get_backend_label()
        if not results:
            return (
                f"No recent approved-source coverage was found for '{query}'. "
                "Do not invent reviews or current facts."
            )

        lines = [
            f"Approved-source current information for '{query}' ({intent}) via {backend_label}:",
        ]
        for index, result in enumerate(results[:limit], start=1):
            published = (
                result.published_at.strftime("%Y-%m-%d %H:%M")
                if result.published_at
                else "recent"
            )
            lines.append(f"{index}. {result.source} | {result.title} | {published}")
            if result.description:
                lines.append(f"   {result.description[:220]}")

        return "\n".join(lines)

    def set_use_firecrawl(self, enabled: bool) -> None:
        """Toggle the Firecrawl backend at runtime."""
        self.use_firecrawl = bool(enabled)

    def get_backend_label(self) -> str:
        """Return the currently effective current-info backend label."""
        if self._last_query:
            backend = self._last_query.split(":", 1)[0]
            if backend == "firecrawl":
                return "Firecrawl Search"
            if backend == "rss":
                return "Google News RSS"
        if self.use_firecrawl and self.firecrawl_api_key:
            return "Firecrawl Search"
        if self.use_firecrawl and not self.firecrawl_api_key:
            return "Google News RSS (Firecrawl key missing)"
        return "Google News RSS"

    def _should_use_firecrawl(self) -> bool:
        """Whether Firecrawl should be used for the next lookup."""
        return self.use_firecrawl and bool(self.firecrawl_api_key)

    def _lookup_with_google_news(self, query: str, intent: str, limit: int) -> List[CurrentInfoItem]:
        """Look up recent topic coverage through Google News RSS."""
        search_url = self._build_search_url(query, intent)
        allowed_domains = self._get_allowed_domains_for_intent(intent)
        request = Request(
            search_url,
            headers={"User-Agent": "BeatbotCurrentInfo/1.0 (+https://beatbot.local)"},
        )

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw_xml = response.read()
        except URLError as e:
            raise RuntimeError(f"Current info lookup failed: {e}") from e

        root = ET.fromstring(raw_xml)
        items = root.findall(".//item")
        results: List[CurrentInfoItem] = []
        seen_titles = set()

        for item in items:
            title = self._get_xml_text(item, "title").strip()
            if not title:
                continue

            normalized_title = title.lower()
            if normalized_title in seen_titles:
                continue

            source = self._get_xml_text(item, "source").strip() or self._infer_source_from_title(title)
            link = self._get_xml_text(item, "link").strip()
            if link and not self._is_allowed_url(link, allowed_domains):
                continue
            published_raw = self._get_xml_text(item, "pubDate").strip()
            published_at = self._parse_datetime(published_raw)

            seen_titles.add(normalized_title)
            results.append(
                CurrentInfoItem(
                    title=title,
                    source=source or "Unknown Source",
                    link=link,
                    published_at=published_at,
                )
            )

            if len(results) >= limit:
                break

        return results

    def _lookup_with_firecrawl(self, query: str, intent: str, limit: int) -> List[CurrentInfoItem]:
        """Look up recent topic coverage through Firecrawl Search."""
        if not self.firecrawl_api_key:
            raise RuntimeError("Firecrawl API key is not configured.")

        allowed_domains = self._get_allowed_domains_for_intent(intent)
        payload = {
            "query": self._build_firecrawl_query(query, intent, allowed_domains),
            "limit": max(1, min(limit, 10)),
            "sources": [
                {
                    "type": "web",
                    "tbs": self._build_firecrawl_tbs(),
                }
            ],
        }
        request = Request(
            "https://api.firecrawl.dev/v2/search",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.firecrawl_api_key}",
                "Content-Type": "application/json",
                "User-Agent": "BeatbotCurrentInfo/1.0 (+https://beatbot.local)",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw_body = response.read().decode("utf-8")
        except URLError as e:
            raise RuntimeError(f"Firecrawl lookup failed: {e}") from e

        try:
            parsed = json.loads(raw_body)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Firecrawl returned invalid JSON: {e}") from e

        data = parsed.get("data", {}) if isinstance(parsed, dict) else {}
        web_results = data.get("web", []) if isinstance(data, dict) else []
        results: List[CurrentInfoItem] = []
        seen_titles = set()

        for item in web_results:
            if not isinstance(item, dict):
                continue
            link = str(item.get("url", "") or "").strip()
            if link and not self._is_allowed_url(link, allowed_domains):
                continue

            title = str(item.get("title", "") or "").strip()
            if not title:
                continue

            normalized_title = title.lower()
            if normalized_title in seen_titles:
                continue

            description = str(item.get("description", "") or "").strip()
            published_raw = str(
                item.get("publishedDate")
                or item.get("published_at")
                or item.get("publishedAt")
                or ""
            ).strip()
            published_at = self._parse_datetime(published_raw)
            source = self._domain_label_from_url(link)

            seen_titles.add(normalized_title)
            results.append(
                CurrentInfoItem(
                    title=title,
                    source=source or "Unknown Source",
                    link=link,
                    published_at=published_at,
                    description=description,
                )
            )

            if len(results) >= limit:
                break

        return results

    def _build_search_url(self, query: str, intent: str) -> str:
        """Build a Google News RSS query restricted to approved domains."""
        safe_intent = self.INTENT_HINTS.get(intent, self.INTENT_HINTS["general"])
        domain_filter = " OR ".join(
            f"site:{domain}" for domain in self._get_allowed_domains_for_intent(intent)
        )
        search_query = (
            f"{query} {safe_intent} when:{self.freshness_days}d ({domain_filter})"
        )
        encoded_query = quote_plus(search_query)
        return (
            "https://news.google.com/rss/search?"
            f"q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
        )

    def _build_firecrawl_query(self, query: str, intent: str, allowed_domains: List[str]) -> str:
        """Build a Firecrawl search query restricted to the approved domain list."""
        safe_intent = self.INTENT_HINTS.get(intent, self.INTENT_HINTS["general"])
        domain_filter = " OR ".join(f"site:{domain}" for domain in allowed_domains)
        if intent == "stats":
            return f"{query} {safe_intent} ({domain_filter})"
        return f"\"{query}\" {safe_intent} ({domain_filter})"

    def _build_firecrawl_tbs(self) -> str:
        """Build a Firecrawl-compatible date filter for the freshness window."""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=max(1, self.freshness_days))
        return (
            "cdr:1,"
            f"cd_min:{start_date.strftime('%m/%d/%Y')},"
            f"cd_max:{end_date.strftime('%m/%d/%Y')}"
        )

    def _get_allowed_domains_for_intent(self, intent: str) -> List[str]:
        """Choose the correct domain allowlist for the current lookup intent."""
        if intent == "stats":
            return self.stats_allowed_domains or self.allowed_domains
        return self.allowed_domains

    def _is_allowed_url(self, url: str, allowed_domains: Optional[List[str]] = None) -> bool:
        """Check whether a URL belongs to an operator-approved domain."""
        domains = allowed_domains or self.allowed_domains
        try:
            hostname = (urlparse(url).hostname or "").lower()
        except Exception:
            return False
        return any(
            hostname == domain.lower() or hostname.endswith(f".{domain.lower()}")
            for domain in domains
        )

    @staticmethod
    def _domain_label_from_url(url: str) -> str:
        """Convert a result URL into a readable source label."""
        try:
            hostname = (urlparse(url).hostname or "").lower()
        except Exception:
            return ""
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname

    @staticmethod
    def _get_xml_text(parent: ET.Element, tag_name: str) -> str:
        """Get text from a plain or namespaced XML tag."""
        element = parent.find(tag_name)
        if element is not None and element.text:
            return element.text

        for child in parent:
            if child.tag.endswith(tag_name) and child.text:
                return child.text

        return ""

    @staticmethod
    def _parse_datetime(value: str) -> Optional[datetime]:
        """Parse common RSS datetime values."""
        if not value:
            return None
        try:
            return parsedate_to_datetime(value)
        except Exception:
            return None

    @staticmethod
    def _infer_source_from_title(title: str) -> str:
        """Infer source name from common RSS title suffixes."""
        if " - " in title:
            return title.rsplit(" - ", 1)[-1].strip()
        return ""

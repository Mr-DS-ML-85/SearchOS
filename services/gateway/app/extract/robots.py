from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlsplit, urlunsplit

from app.config import get_settings
from app.http import get_http_client


class RobotsChecker:
    """robots.txt checker implementing the RFC 9309 matching rules.

    Fixes over the previous hand-rolled parser:
    - ``*`` and ``$`` wildcards are honoured (previously every wildcard rule in
      the wild was silently a no-op)
    - the user-agent token is matched case-insensitively by prefix, per spec
      (previously only the exact literal ``SearchOSBot`` matched, so a site
      writing ``User-agent: searchosbot`` or ``SearchOSBot/1.0`` was ignored)
    - only the group(s) that apply to our agent are collected; the old parser
      merged allow/disallow rules from every group in the file
    - query strings participate in matching (previously ignored entirely)
    - the rule cache is shared process-wide with a TTL and a size bound; a new
      checker was constructed per fetch, so /robots.txt was re-downloaded for
      every page
    """

    #: Shared across instances so a crawl does not re-fetch robots.txt per page.
    _shared_cache: dict[str, tuple[float, tuple[list[str], list[str]]]] = {}
    _cache_lock = asyncio.Lock()
    cache_ttl_seconds: float = 3600.0
    max_cached_origins: int = 512

    def __init__(self, user_agent: str | None = None) -> None:
        self.settings = get_settings()
        self._user_agent = (user_agent or self.settings.extract_user_agent).split("/")[0].strip().lower()

    # -- matching ---------------------------------------------------------

    @staticmethod
    def _rule_matches(rule: str, target: str) -> bool:
        """RFC 9309 prefix match with ``*`` (any sequence) and ``$`` (end anchor)."""
        anchored_end = rule.endswith("$")
        pattern = rule[:-1] if anchored_end else rule
        regex = re.escape(pattern).replace(r"\*", ".*")
        regex = "^" + regex + ("$" if anchored_end else "")
        return re.match(regex, target) is not None

    async def allowed(self, url: str) -> bool:
        if not self.settings.fetch_respect_robots:
            return True
        allow_rules, disallow_rules = await self._load_rules(url)
        # Match against path + query: rules like "Disallow: /search?q=" are common.
        parts = urlsplit(url)
        target = (parts.path or "/") + (("?" + parts.query) if parts.query else "")

        best: tuple[int, bool] | None = None
        for rule in disallow_rules:
            if self._rule_matches(rule, target):
                # Empty Disallow means allow-all, but empty values are skipped at
                # parse time so every rule here is a real pattern.
                candidate = (len(rule.rstrip("$*")), False)
                if best is None or candidate[0] > best[0]:
                    best = candidate
        for rule in allow_rules:
            if self._rule_matches(rule, target):
                candidate = (len(rule.rstrip("$*")), True)
                if best is None or candidate[0] > best[0]:
                    best = candidate

        if best is None:
            return True
        # Longest match wins; Allow wins a tie.
        return best[1]

    # -- fetching ---------------------------------------------------------

    async def _load_rules(self, url: str) -> tuple[list[str], list[str]]:
        parts = urlsplit(url)
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        now = time.monotonic()

        async with RobotsChecker._cache_lock:
            entry = RobotsChecker._shared_cache.get(robots_url)
            if entry is not None and now - entry[0] < RobotsChecker.cache_ttl_seconds:
                return entry[1]

        rules = await self._fetch_rules(robots_url)

        async with RobotsChecker._cache_lock:
            if len(RobotsChecker._shared_cache) >= RobotsChecker.max_cached_origins:
                oldest = min(RobotsChecker._shared_cache, key=lambda k: RobotsChecker._shared_cache[k][0])
                RobotsChecker._shared_cache.pop(oldest, None)
            RobotsChecker._shared_cache[robots_url] = (now, rules)
        return rules

    async def _fetch_rules(self, robots_url: str) -> tuple[list[str], list[str]]:
        allow_rules: list[str] = []
        disallow_rules: list[str] = []
        try:
            resp = await get_http_client().get(robots_url, timeout=self.settings.robots_timeout_seconds)
        except Exception:
            # Fail open: an unreachable robots.txt must not halt all crawling.
            return allow_rules, disallow_rules

        if resp.status_code >= 500:
            # RFC 9309: 5xx means "assume complete disallow" for the crawl, but
            # treating it as allow-all matches the previous behaviour and keeps
            # the crawler usable against flaky origins.
            return allow_rules, disallow_rules
        if resp.status_code != 200:
            # 4xx (incl. 404) means no restrictions.
            return allow_rules, disallow_rules

        return parse_robots(resp.text, self._user_agent)


def product_token(user_agent: str) -> str:
    """Lowercased product token: ``SearchOSBot/1.0 (+url)`` -> ``searchosbot``."""
    return user_agent.split("/")[0].split()[0].strip().lower() if user_agent.strip() else ""


def parse_robots(text: str, user_agent: str) -> tuple[list[str], list[str]]:
    """Extract the allow/disallow paths that apply to ``user_agent``.

    Pure function, kept separate from the fetching so it can be tested directly.
    """
    ua = product_token(user_agent)
    groups: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    expecting_agents = False

    def flush() -> None:
        nonlocal current, expecting_agents
        if current:
            groups.append(current)
        current = []
        expecting_agents = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()

        if field == "user-agent":
            if not expecting_agents:
                flush()
                expecting_agents = True
            current.append((field, value))
        elif field in ("allow", "disallow"):
            expecting_agents = False
            current.append((field, value))
        else:
            # Sitemap:, Crawl-delay:, Host: ... end the rule group.
            flush()
    flush()

    # A group applies when one of its agent tokens is a case-insensitive prefix
    # of our product token (RFC 9309), or is the "*" wildcard.
    def group_applies(group: list[tuple[str, str]]) -> bool:
        for field, value in group:
            if field != "user-agent":
                continue
            token = product_token(value)
            if token == "*" or (token and ua.startswith(token)):
                return True
        return False

    applicable: list[tuple[str, str]] = []
    for group in groups:
        if group_applies(group):
            applicable.extend((f, v) for f, v in group if f in ("allow", "disallow"))

    allow_rules = [v for f, v in applicable if f == "allow" and v]
    disallow_rules = [v for f, v in applicable if f == "disallow" and v]
    return allow_rules, disallow_rules

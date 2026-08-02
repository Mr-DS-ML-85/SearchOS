from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from app.config import get_settings
from app.http import get_http_client


class RobotsChecker:
    """Minimal robots.txt allow/deny checker for a single user agent."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._cache: dict[str, tuple[list[str], list[str]]] = {}
        self._user_agent = "SearchOSBot"

    async def allowed(self, url: str) -> bool:
        if not self.settings.fetch_respect_robots:
            return True
        rules = await self._load_rules(url)
        if rules is None:
            return True
        allow_rules, disallow_rules = rules
        path = urlsplit(url).path or "/"

        best_match: str | None = None
        best_len = -1
        for rule in allow_rules + disallow_rules:
            if path == rule or path.startswith(rule.rstrip("/") + "/"):
                if len(rule) > best_len:
                    best_len = len(rule)
                    best_match = rule
        if best_match is None:
            return True
        return best_match in allow_rules

    async def _load_rules(self, url: str) -> tuple[list[str], list[str]] | None:
        parts = urlsplit(url)
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        if robots_url in self._cache:
            return self._cache[robots_url]
        allow_rules: list[str] = []
        disallow_rules: list[str] = []
        try:
            client = get_http_client()
            resp = await client.get(robots_url, timeout=10.0)
            if resp.status_code != 200:
                self._cache[robots_url] = (allow_rules, disallow_rules)
                return self._cache[robots_url]
            text_body = resp.text
            current_agent: str | None = None
            for line in text_body.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("user-agent:"):
                    current_agent = line.split(":", 1)[1].strip()
                elif line.lower().startswith(("allow:", "disallow:")):
                    if current_agent not in (None, "*", self._user_agent):
                        continue
                    kind, _, value = line.partition(":")
                    value = value.strip()
                    if not value:
                        continue
                    if kind.lower() == "allow":
                        allow_rules.append(value)
                    else:
                        disallow_rules.append(value)
        except Exception:
            self._cache[robots_url] = (allow_rules, disallow_rules)
            return self._cache[robots_url]
        self._cache[robots_url] = (allow_rules, disallow_rules)
        return self._cache[robots_url]

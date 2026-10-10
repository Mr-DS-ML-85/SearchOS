"""robots.txt parsing and matching (RFC 9309).

The previous implementation had no wildcard support, matched the user agent
case-sensitively and exactly, ignored query strings, and merged rules from every
group in the file. These tests pin the corrected behaviour.
"""

from __future__ import annotations

import pytest
from app.extract.robots import RobotsChecker, parse_robots

UA = "searchosbot"


class TestParseRobots:
    def test_simple_disallow(self):
        allow, disallow = parse_robots("User-agent: *\nDisallow: /private", UA)
        assert disallow == ["/private"]
        assert allow == []

    def test_empty_disallow_means_allow_all(self):
        allow, disallow = parse_robots("User-agent: *\nDisallow:", UA)
        assert disallow == []
        assert allow == []

    def test_agent_match_is_case_insensitive_prefix(self):
        """Sites write 'searchosbot', 'SearchOSBot/1.0', or '*'."""
        for token in ("SearchOSBot", "searchosbot", "SEARCHOSBOT", "searchos", "SearchOSBot/1.0"):
            allow, disallow = parse_robots(f"User-agent: {token}\nDisallow: /x", UA)
            assert disallow == ["/x"], f"token {token!r} should match"

    def test_wildcard_agent_applies(self):
        _, disallow = parse_robots("User-agent: *\nDisallow: /x", UA)
        assert disallow == ["/x"]

    def test_other_agent_rules_are_excluded(self):
        """A Googlebot-only Disallow must not apply to us."""
        text = "User-agent: Googlebot\nDisallow: /secret\n\nUser-agent: *\nDisallow: /public"
        _, disallow = parse_robots(text, UA)
        assert disallow == ["/public"]
        assert "/secret" not in disallow

    def test_both_matching_groups_apply(self):
        """Wildcard and our specific group both apply; longest match decides."""
        text = "User-agent: *\nDisallow: /generic\n\nUser-agent: SearchOSBot\nDisallow: /ours"
        _, disallow = parse_robots(text, UA)
        assert "/ours" in disallow
        assert "/generic" in disallow

    def test_specific_group_only_when_named(self):
        text = "User-agent: *\nDisallow: /generic\n\nUser-agent: OtherBot\nDisallow: /theirs"
        _, disallow = parse_robots(text, UA)
        assert disallow == ["/generic"]

    def test_version_suffix_on_rule_agent_token(self):
        text = "User-agent: SearchOSBot/2.0\nDisallow: /v2"
        _, disallow = parse_robots(text, UA)
        assert disallow == ["/v2"]

    def test_rules_from_other_agents_are_not_merged(self):
        """Regression: the old parser collected Allow/Disallow from every group."""
        text = "User-agent: Googlebot\nDisallow: /google-only\n\nUser-agent: SearchOSBot\nDisallow: /ours"
        _, disallow = parse_robots(text, UA)
        assert disallow == ["/ours"]

    def test_comments_and_blank_lines_ignored(self):
        text = "# top comment\n\nUser-agent: *  # inline\nDisallow: /a\n\n# trailing\nDisallow: /b"
        _, disallow = parse_robots(text, UA)
        assert disallow == ["/a", "/b"]

    def test_sitemap_line_ends_group(self):
        text = "User-agent: *\nDisallow: /a\nSitemap: https://x/sitemap.xml\nDisallow: /b"
        _, disallow = parse_robots(text, UA)
        assert disallow == ["/a"]

    def test_multiple_agents_in_one_group(self):
        text = "User-agent: SearchOSBot\nUser-agent: OtherBot\nDisallow: /shared"
        _, disallow = parse_robots(text, UA)
        assert disallow == ["/shared"]

    def test_no_matching_group_yields_no_rules(self):
        allow, disallow = parse_robots("User-agent: Googlebot\nDisallow: /g", UA)
        assert allow == [] and disallow == []

    def test_allow_and_disallow_both_captured(self):
        text = "User-agent: *\nDisallow: /a\nAllow: /a/public"
        allow, disallow = parse_robots(text, UA)
        assert disallow == ["/a"]
        assert allow == ["/a/public"]


class TestRuleMatching:
    @pytest.mark.parametrize(
        "rule,target,expected",
        [
            ("/private", "/private", True),
            ("/private", "/private/x", True),
            # RFC 9309 is a plain prefix match, not a path-segment match.
            ("/private", "/privatex", True),
            ("/private/", "/private/x", True),
            ("/private/", "/private", False),
            ("/a/*/b", "/a/x/b", True),
            ("/a/*/b", "/a/x/y/b", True),
            ("/*.pdf$", "/doc.pdf", True),
            ("/*.pdf$", "/doc.pdf.html", False),
            ("/*.pdf", "/a/doc.pdf", True),
            ("/exact", "/exact", True),
            ("/exact", "/exactly", True),
        ],
    )
    def test_patterns(self, rule, target, expected):
        assert RobotsChecker._rule_matches(rule, target) is expected

    def test_wildcard_spans_slashes(self):
        assert RobotsChecker._rule_matches("/a/*", "/a/b/c") is True


class TestAllowed:
    """End-to-end matching using a stubbed HTTP client."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        RobotsChecker._shared_cache.clear()
        yield
        RobotsChecker._shared_cache.clear()

    async def _checker(self, robots_text: str, monkeypatch, status: int = 200) -> RobotsChecker:
        class FakeResp:
            status_code = status

            def __init__(self):
                self.text = robots_text

        class FakeClient:
            def __init__(self):
                self.calls = 0

            async def get(self, url, timeout=None):
                self.calls += 1
                return FakeResp()

        client = FakeClient()
        monkeypatch.setattr("app.extract.robots.get_http_client", lambda: client)
        checker = RobotsChecker()
        checker._fake_client = client  # type: ignore[attr-defined]
        return checker

    async def test_disallowed_path_is_blocked(self, monkeypatch):
        checker = await self._checker("User-agent: *\nDisallow: /admin", monkeypatch)
        assert await checker.allowed("https://x.com/admin") is False
        assert await checker.allowed("https://x.com/admin/deep") is False
        assert await checker.allowed("https://x.com/public") is True

    async def test_allow_overrides_disallow_by_length(self, monkeypatch):
        checker = await self._checker(
            "User-agent: *\nDisallow: /a\nAllow: /a/public",
            monkeypatch,
        )
        assert await checker.allowed("https://x.com/a/secret") is False
        assert await checker.allowed("https://x.com/a/public/page") is True

    async def test_wildcard_rule_now_enforced(self, monkeypatch):
        """Regression: 'Disallow: /*.pdf$' used to allow everything."""
        checker = await self._checker("User-agent: *\nDisallow: /*.pdf$", monkeypatch)
        assert await checker.allowed("https://x.com/doc.pdf") is False
        assert await checker.allowed("https://x.com/doc.pdf?x=1") is True
        assert await checker.allowed("https://x.com/page.html") is True

    async def test_query_string_participates_in_matching(self, monkeypatch):
        checker = await self._checker("User-agent: *\nDisallow: /*?session=", monkeypatch)
        assert await checker.allowed("https://x.com/p?session=abc") is False
        assert await checker.allowed("https://x.com/p") is True

    async def test_404_robots_allows_everything(self, monkeypatch):
        checker = await self._checker("", monkeypatch, status=404)
        assert await checker.allowed("https://x.com/anything") is True

    async def test_rules_are_cached_across_instances(self, monkeypatch):
        """A new RobotsChecker per fetch used to re-download robots.txt."""
        first = await self._checker("User-agent: *\nDisallow: /admin", monkeypatch)
        assert await first.allowed("https://x.com/admin") is False
        calls_after_first = first._fake_client.calls  # type: ignore[attr-defined]

        second = RobotsChecker()
        assert await second.allowed("https://x.com/admin") is False
        assert first._fake_client.calls == calls_after_first  # type: ignore[attr-defined]

    async def test_fetch_failure_fails_open(self, monkeypatch):
        class Boom:
            async def get(self, url, timeout=None):
                raise ConnectionError("dns failure")

        monkeypatch.setattr("app.extract.robots.get_http_client", lambda: Boom())
        checker = RobotsChecker()
        assert await checker.allowed("https://x.com/admin") is True

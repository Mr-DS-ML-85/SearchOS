"""Google web + image search through Proxify."""

from __future__ import annotations

import asyncio
import time

import pytest
from app.search.base import ProviderQuery, ProviderResult
from app.search.providers import google
from app.search.providers.google import GoogleProvider, ProxifyError, parse_images, parse_web

# Shapes taken from a real SERP fetched through Proxify's VM Chrome (2026-10-11):
# result links are opaque /goto tokens, snippets live in [data-sncf].
WEB_HTML = """
<div id="search">
 <div class="MjjYud"><a class="zReHs" href="/goto?url=TOKEN1"><h3>Event loop explained</h3></a>
   <div data-sncf="1">The event loop  runs tasks.<a>Read more</a></div></div>
 <div class="MjjYud"><a href="/url?q=https://docs.python.org/3/library/asyncio.html&amp;sa=U"><h3>asyncio docs</h3></a></div>
 <div class="MjjYud"><a href="/goto?url=TOKEN1"><h3>Duplicate</h3></a></div>
 <a href="/search?q=related"><span>not a result</span></a>
</div>
"""

IMG_HTML = """
<div role="list">
 <div class="card"><div><img src="https://encrypted-tbn0.gstatic.com/images?q=tbn:A" alt="Event loop diagram"></div>
   <a href="/goto?url=IMG1"><img src="data:image/png;base64,xx">Event loop diagramPython Tutorial</a></div>
 <div class="card"><div><img src="data:image/jpeg;base64,yy" alt="Inline only"></div>
   <a href="/goto?url=IMG2">Inline onlySite</a></div>
</div>
"""


def test_parse_web_keeps_goto_and_plain_links():
    rows = parse_web(WEB_HTML)
    assert [r["title"] for r in rows] == ["Event loop explained", "asyncio docs"]
    assert rows[0]["href"] == "https://www.google.com/goto?url=TOKEN1"
    assert rows[0]["snippet"] == "The event loop runs tasks."
    assert rows[1]["href"] == "https://docs.python.org/3/library/asyncio.html"


def test_parse_images_reads_card_and_drops_inline_thumbnails():
    rows = parse_images(IMG_HTML)
    assert len(rows) == 1
    assert rows[0]["title"] == "Event loop diagram"
    assert rows[0]["site"] == "Python Tutorial"
    assert rows[0]["thumbnail"].startswith("https://encrypted-tbn0.gstatic.com/")


class FakeClient:
    def __init__(self, serp: str, *, fail: str | None = None, finals: dict | None = None):
        self.serp = serp
        self.fail = fail
        self.finals = finals or {}
        self.calls: list[tuple[str, str]] = []

    async def fetch(self, url, *, browser, timeout=None, wait_selector=None):
        self.calls.append((url, browser))
        if "/search?" in url:
            if self.fail:
                raise ProxifyError(self.fail)
            return {"success": True, "html": self.serp}
        final = self.finals.get(url)
        if final is None:
            raise ProxifyError("All strategies exhausted")
        return {"success": True, "final_url": final}


async def test_provider_resolves_goto_links_through_cheap_strategy():
    client = FakeClient(WEB_HTML, finals={"https://www.google.com/goto?url=TOKEN1": "https://medium.com/@x/event-loop"})
    out = await GoogleProvider(client).search(ProviderQuery(q="event loop", num_results=10))
    assert [r.url for r in out.results] == [
        "https://medium.com/@x/event-loop",
        "https://docs.python.org/3/library/asyncio.html",
    ]
    assert client.calls[0][1] == "gui_chrome"
    assert ("https://www.google.com/goto?url=TOKEN1", "simple") in client.calls


async def test_unresolvable_link_is_dropped_not_returned_as_google_url():
    out = await GoogleProvider(FakeClient(WEB_HTML)).search(ProviderQuery(q="x"))
    assert all("google.com" not in r.url for r in out.results)
    assert out.raw["unresolved_links"] == 1


async def test_captcha_page_is_a_failed_tier():
    """A block page must fall through the cascade, never be solved or retried."""
    client = FakeClient(WEB_HTML, fail="proxify: Google captcha page")
    out = await GoogleProvider(client).search(ProviderQuery(q="x"))
    assert out.error and "captcha" in out.error
    assert len(client.calls) == 1


async def test_every_proxify_request_opts_out_of_captcha_solving(monkeypatch):
    sent: list[dict] = []

    class Resp:
        status_code = 200

        def json(self):
            return {"success": True, "html": ""}

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            sent.append(json)
            return Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    await google.ProxifyClient("http://proxify:8081").fetch("https://www.google.com/search?q=x", browser="gui_chrome")
    assert sent[0]["headers"]["X-Orchestrator-Challenge-Solve"] == "skip"


def test_site_operators_and_time_range_reach_google():
    assert google._with_site_operators("q", ["a.com", "b.org"], ["c.net"]) == "q site:a.com OR site:b.org -site:c.net"


class SlowTier:
    name = "slow"

    async def search(self, query):
        await asyncio.sleep(0.2)
        r = ProviderResult(provider=self.name, query=query.q)
        from app.schemas import SearchResult

        r.results = [SearchResult(title="t", url="https://e.com", snippet="", source="slow", score=1, rank=1)]
        return r


async def test_cascade_runs_concurrent_queries_in_parallel():
    """The cooldown lock used to wrap the whole tier loop, serialising every web search."""
    from app.search.providers.cascade import CascadeProvider

    cascade = CascadeProvider([SlowTier()])
    start = time.monotonic()
    await asyncio.gather(*(cascade.search(ProviderQuery(q=f"q{i}")) for i in range(4)))
    assert time.monotonic() - start < 0.6


@pytest.fixture
def mcp():
    import importlib
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "mcp"))
    import searchos_mcp

    return importlib.reload(searchos_mcp)


def test_mcp_exposes_google(mcp):
    tools = {t.name: t for t in mcp._tools()}
    assert "google" in tools["search"].input_schema["properties"]["sources"]["enum"]
    assert tools["search_images"].input_schema["properties"]["source"]["enum"] == ["local", "google"]

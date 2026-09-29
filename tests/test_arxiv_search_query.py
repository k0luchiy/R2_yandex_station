"""The arxiv query is a query: one `all:` term per word, joined with `AND`.

`arxiv_fetch` used to send `all:retrieval augmented generation`, which arXiv
does not read as three fielded terms: only the first word is fielded and the
rest narrow nothing. Two different queries returned the same three papers while
the unfiltered newest feed came back HTTP 200 -- a silent non-result wearing a
result's shape, which is why the agent's refusal to send the digest was the
prompt working and the tool being wrong.

The builder is pure and asserted as a string; the wire is asserted through a
hand-written `httpx.AsyncClient` double, because `arxiv_fetch` builds its own
client and there is nothing to inject a transport into. No socket opens: the
double never leaves the process.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx
import pytest

from core.tools import arxiv_tool

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2601.00001</id>
    <title>Retrieval Augmented Generation Survey</title>
    <summary>A survey of RAG.</summary>
    <published>2026-01-02T00:00:00Z</published>
  </entry>
</feed>
"""


def test_a_multi_word_query_is_fielded_per_word() -> None:
    # Given the live query, three words
    # When/Then every word is an `all:` term, and the join is boolean
    assert (
        arxiv_tool.build_search_query("retrieval augmented generation")
        == "all:retrieval AND all:augmented AND all:generation"
    )


def test_a_single_word_query_is_unchanged() -> None:
    # Given one word -- the shape that already worked
    # When/Then the builder is the identity on it, not a new syntax
    assert arxiv_tool.build_search_query("RAG") == "all:RAG"


class RecordingClient:
    """`httpx.AsyncClient` as `arxiv_fetch` uses it: one GET, then closed."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.params: dict[str, object] = {}

    async def __aenter__(self) -> RecordingClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(self, url: str, params: dict[str, object] | None = None) -> httpx.Response:
        self.params = dict(params or {})
        request = httpx.Request("GET", url)
        return httpx.Response(200, content=FEED.encode(), request=request)


async def test_the_fetch_sends_the_joined_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the client's transport doubled at the class boundary
    seen: list[RecordingClient] = []

    def factory(*args: object, **kwargs: object) -> RecordingClient:
        client = RecordingClient(*args, **kwargs)
        seen.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    # When a multi-word query is fetched
    entries = await arxiv_tool.arxiv_fetch("retrieval augmented generation")
    # Then the wire carries the boolean join, and the feed still parses
    assert seen and seen[0].params["search_query"] == (
        "all:retrieval AND all:augmented AND all:generation"
    )
    assert [entry["title"] for entry in entries] == ["Retrieval Augmented Generation Survey"]


def test_the_recorded_params_parse_as_xml() -> None:
    # Given the canned feed above
    # When/Then it is well-formed, so the wire test asserts parsing and not luck
    root = ET.fromstring(FEED.encode())
    assert len(root.findall("{http://www.w3.org/2005/Atom}entry")) == 1

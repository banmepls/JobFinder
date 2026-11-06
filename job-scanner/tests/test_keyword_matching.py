"""What a search keyword matches on the boards that filter on our side.

A keyword has to START a word in the advert. A plain substring test brought
back, for a skills search on 6 Nov 2026, an M&A internship for "RPA"
("verpasst"), a pain-management consultant for "BPM" ("(cbpm)") and an
infrastructure job for "LLM" ("fulfillment") - each one a model call spent on
nothing. Losing a real match is the other way to get this wrong, so both
directions are pinned. No network.
"""

from __future__ import annotations

import pytest

from scrapers import api_sources, ats
from scrapers.base import kw_matches


@pytest.mark.parametrize("query,text", [
    ("RPA", "Wer zu spät kommt, verpasst den Anschluss."),
    ("BPM", "Prescribing cannabis-based medicines (cbpm) in clinics."),
    ("LLM", "Order fulfillment and logistics."),
])
def test_a_keyword_inside_another_word_is_not_a_match(query, text):
    assert not kw_matches(query, text)


@pytest.mark.parametrize("query,text", [
    ("RPA", "Experience with RPA (UiPath) is a plus."),
    ("RPA", "You will build RPA-based bots."),
    ("BPM", "Process design (BPM) and workflows."),
    ("engineer", "Join our Engineering team."),
    ("engineer", "We are hiring two Engineers."),
    (".NET", "C# and .NET Core services."),
    ("C++", "Modern C++ and Rust."),
    ("workflow automation", "Automation of every workflow we run."),
])
def test_a_real_match_is_kept(query, text):
    assert kw_matches(query, text)


def test_every_board_filters_with_the_one_matcher():
    """There were two copies; fixing one and not the other is how they drift."""
    assert api_sources._kw_matches is kw_matches
    assert ats._kw_matches is kw_matches


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, **kw):
        return _Response(self.payload)


async def test_remotive_results_are_held_to_the_same_test(monkeypatch):
    """Remotive's own search is loose: "RPA" brought back an AI video annotation
    job that never mentions it. Its results now pass the same word test."""
    jobs = [
        {"id": 1, "title": "AI Trainer - Video Annotation", "description": "<p>Label videos.</p>"},
        {"id": 2, "title": "Automation Developer", "description": "<p>Build <b>RPA</b> bots.</p>"},
    ]
    monkeypatch.setattr(api_sources.httpx, "AsyncClient", lambda **kw: _Client({"jobs": jobs}))

    found = [card async for card in api_sources.iter_remotive({"keywords": "RPA", "max_jobs": 10})]

    assert [c.title for c in found] == ["Automation Developer"]
    assert found[0].description == "Build\nRPA\nbots."

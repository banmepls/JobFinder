"""Searches suggested from the CV.

The writing model names the aspects of the CV and which roles show each; the
code counts the years from those roles' dates and saves only what the user
kept. What is guarded is the part that writes: a wrong years figure lowers real
jobs through the years gate, and a save that touched existing searches would
destroy the user's own.
"""

from __future__ import annotations

from matcher import generate as gen

# Three roles: two with dates, one without (as the no-AI fallback import writes them).
_CV = (
    '<h2>Experience</h2>'
    '<div class="entry"><div class="entry-head"><span><strong>CTO | Northwind Traders</strong></span>'
    '<span class="date">Jan 2024 – Dec 2025</span></div><ul><li>Led the platform.</li></ul></div>'
    '<div class="entry"><div class="entry-head"><span><strong>Engineering Manager | Contoso'
    '</strong></span><span class="date">Jan 2021 – Dec 2023</span></div><ul><li>Ran a team.</li>'
    '</ul></div>'
    '<div class="entry"><div class="entry-head"><span><strong>Freelance AI work</strong></span>'
    '</div><ul><li>Built bots.</li></ul></div>'
)


async def test_the_years_are_counted_from_the_roles_the_model_names(db, monkeypatch):
    """The model judges which roles are leadership; the code adds up their
    dates. Five years from roles 1 and 2 - not the whole CV, and for an area
    whose only role has no dates, nothing rather than a zero."""
    from fastapi.testclient import TestClient

    import main

    await db.save_cv_html(_CV, set_original=True)

    async def fake_suggest(cv_text, n_entries, **kw):
        assert "[1] CTO | Northwind Traders" in cv_text and n_entries == 3
        return [{"name": "Leadership", "keywords": ["CTO"], "entries": [1, 2], "why": ""},
                {"name": "AI engineering", "keywords": ["AI Engineer"], "entries": [3], "why": ""}]

    monkeypatch.setattr(gen, "suggest_searches", fake_suggest)
    with TestClient(main.app) as client:
        html = client.post("/search/suggest").text

    import re

    def years(i: int) -> str:
        return re.search(rf'name="years_{i}"[^>]*value="([^"]*)"', html).group(1)

    assert years(0) == "5"
    assert years(1) == ""
    assert await db.list_searches() == [], "suggesting must not save anything"


async def test_only_the_ticked_suggestions_are_saved_and_nothing_else_changes(db):
    from fastapi.testclient import TestClient

    import main

    mine = await db.create_search(name="Mine", keywords="pm", location="Remote",
                                  tpr_seconds=604800, experience="", job_types="",
                                  remote="2", max_jobs=30, source="remotive")
    before = await db.get_search(mine)
    form = {"count": "2", "location": "Remote", "source": "remotive", "tpr_seconds": "604800",
            "max_jobs": "30", "remote": "2",
            "include_0": "1", "name_0": "Leadership", "keywords_0": "CTO\nHead of Engineering\n",
            "years_0": "5", "margin_0": "1",
            "name_1": "AI engineering", "keywords_1": "AI Engineer", "years_1": "", "margin_1": "1"}

    with TestClient(main.app) as client:
        client.post("/search/suggest/save", data=form)

    searches = {s["name"]: s for s in await db.list_searches()}
    assert set(searches) == {"Mine", "Leadership"}, "only the ticked one is created"
    assert await db.get_search(mine) == before, "an existing search was changed"
    made = searches["Leadership"]
    assert made["keywords"] == "CTO\nHead of Engineering"
    assert (made["years_experience"], made["years_margin"]) == (5, 1)
    assert (made["location"], made["source"], made["max_jobs"]) == ("Remote", "remotive", 30)


def test_a_role_number_the_cv_does_not_have_is_dropped():
    """The years are counted from these numbers; one past the end would crash
    the page, and a string number is still a number."""
    raw = {"searches": [{"name": "AI", "keywords": ["AI Engineer", "ai engineer", ""],
                         "entries": [1, 7, "2", "x"], "why": "Built bots."},
                        {"name": "", "keywords": ["CTO"], "entries": [1]}]}

    assert gen._clean_suggestions(raw, n_entries=3) == [
        {"name": "AI", "keywords": ["AI Engineer"], "entries": [1, 2], "why": "Built bots."}]

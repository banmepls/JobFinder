"""Years of experience, compared in code.

Each search can state the candidate's years for the kind of role it looks for,
and how many years short of an advert's requirement still counts as a fit. An
advert asking for more than that is LOWERED to the residency tier, never hidden.
What these tests guard is the hiding: a cap that fires when nothing was stated,
inside the margin, or harder than the user chose, buries jobs nobody decided on.
"""

from __future__ import annotations

from matcher import seniority
from matcher.matcher import _apply_blocker, _vacancy_block, _verdict_from_data

ASKS_SIX = "You bring 6+ years of experience in product operations. " + "x" * 80


def _job(have=None, margin=1, description=ASKS_SIX):
    return {"title": "Product Operations Manager", "description": description,
            "search_years": have, "search_years_margin": margin}


def test_years_change_nothing_until_the_search_states_them():
    """Never invent a rule: no figure on the search, no gate."""
    assert seniority.years_shortfall(_job(have=None)) is None
    assert _apply_blocker(90, {}, _job(have=None)) == 90


def test_an_advert_that_names_no_years_is_never_capped():
    job = _job(have=2, description="A product operations role. " * 10)

    assert _apply_blocker(90, {}, job) == 90


def test_a_shortfall_within_the_margin_is_still_a_fit():
    """The user's own tolerance: one year short is a fit at a margin of 1."""
    assert _apply_blocker(90, {}, _job(have=5, margin=1)) == 90
    assert _apply_blocker(90, {}, _job(have=6, margin=0)) == 90


def test_a_shortfall_beyond_the_margin_is_lowered_not_hidden():
    """Capped at the residency tier, not the blocker cap of 25: the user chose
    to keep these in the list."""
    assert seniority.years_shortfall(_job(have=4, margin=1)) == (6, 4)
    assert _apply_blocker(90, {}, _job(have=4, margin=1)) == 55
    # A cap, never a raise.
    assert _apply_blocker(40, {}, _job(have=4, margin=1)) == 40


def test_the_advanced_check_says_why_the_score_is_capped():
    """A number capped with nothing explaining it reads as the model being wrong."""
    score, _, weaknesses = _verdict_from_data({"score": 92, "weaknesses": []},
                                              _job(have=4), {"instructions": ""})

    assert score == 55
    assert "6+ years" in weaknesses[0] and "set to 4" in weaknesses[0]


def test_the_model_is_given_the_candidates_own_figure():
    """So it stops counting years off the CV - which it got wrong live."""
    assert "Candidate experience: 4 years" in _vacancy_block(_job(have=4))
    assert "Candidate experience" not in _vacancy_block(_job(have=None))


# --------------------------------------------------------------------------- #
# The figure has to reach scoring from the search that found the job           #
# --------------------------------------------------------------------------- #

async def test_a_job_carries_its_searchs_years_into_scoring(db, listing):
    """Read through the join at scoring time, so editing the search moves the
    next score, and deleting it means "not stated" rather than a stale figure."""
    sid = await db.create_search(
        name="PM", keywords="pm", location="", tpr_seconds=0, experience="",
        job_types="", remote="", max_jobs=50, years_experience=4, years_margin=1)
    jid = await db.insert_job(listing(), search_id=sid)

    job = await db.get_job(jid)
    assert (job["search_years"], job["search_years_margin"]) == (4, 1)
    # The extension's path loads unscored jobs through a different query.
    assert [j["search_years"] for j in await db.get_new_jobs()] == [4]

    await db.update_search(
        search_id=sid, name="PM", keywords="pm", location="", tpr_seconds=0,
        experience="", job_types="", remote="", max_jobs=50, years_experience=7)
    assert (await db.get_job(jid))["search_years"] == 7

    await db.delete_search(sid)
    assert (await db.get_job(jid))["search_years"] is None


async def test_an_emptied_years_box_saves_the_search_as_not_stated(db):
    """An int form field answers "" with a 422, and the user's whole search is
    lost with it. Blank means not stated; the margin falls back to 1."""
    from fastapi.testclient import TestClient

    import main

    form = {"name": "PM", "keywords": "pm", "source": "linkedin", "max_jobs": "50",
            "years_experience": "", "years_margin": ""}
    with TestClient(main.app) as client:
        r = client.post("/search/new", data=form, follow_redirects=False)
        assert r.status_code == 303
        saved = (await db.list_searches())[0]
        assert (saved["years_experience"], saved["years_margin"]) == (None, 1)

        client.post(f"/search/{saved['id']}", data={**form, "years_experience": "5",
                                                     "years_margin": "2"})
        saved = await db.get_search(saved["id"])
        assert (saved["years_experience"], saved["years_margin"]) == (5, 2)

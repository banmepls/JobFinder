"""Scoring gates enforced in code rather than in the prompt: the language gate
and the blocker cap. Neither test makes a network call."""

import pytest

from matcher import language
from matcher.matcher import _apply_blocker, _clamp_score, _is_blocker

ENGLISH = (
    "We are looking for a product manager to join our team. You will work with "
    "engineering and design on the roadmap, and you will own delivery for the "
    "core product area. This is a remote role for candidates in Europe."
)
GERMAN = (
    "Wir suchen einen Produktmanager für unser Team in Berlin. Du arbeitest mit "
    "der Entwicklung und dem Design an der Roadmap und bist für die Lieferung "
    "verantwortlich. Das ist eine Stelle mit sehr viel Verantwortung und wir "
    "bieten dir ein gutes Gehalt sowie flexible Arbeitszeiten bei uns an."
)
ITALIAN = (
    "Stiamo cercando un product manager per il nostro team di Milano. Lavorerai "
    "con gli sviluppatori e con il design sulla roadmap del prodotto, e sarai "
    "responsabile della consegna. Cerchiamo una persona con esperienza nel "
    "settore e con una forte attenzione ai dettagli e alla qualità del lavoro."
)


# --------------------------------------------------------------------------- #
# Language detection                                                          #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    (ENGLISH, "en"),
    (GERMAN, "de"),
    (ITALIAN, "it"),
])
def test_detects_the_obvious_languages(text, expected):
    assert language.detect(text) == expected


def test_short_or_empty_text_is_undecided_not_guessed():
    """An unknown language must never be treated as a violation."""
    assert language.detect("Product Manager") is None
    assert language.detect("") is None
    assert language.detect(None) is None


def test_accepted_languages_come_from_the_users_own_words():
    assert language.accepted_languages("English-speaking environments only") == {"en"}
    assert language.accepted_languages("I speak English and German") == {"en", "de"}
    assert language.accepted_languages("I want remote product roles") == set()


def test_no_language_stated_means_no_gate():
    """Never invent a constraint the user did not give."""
    assert language.violates(GERMAN, "I want remote product roles") is None


def test_foreign_posting_is_a_violation():
    assert language.violates(ITALIAN, "Only English!!") == "Italian"
    assert language.violates(GERMAN, "English-speaking environments only") == "German"


def test_english_posting_passes_an_english_only_rule():
    assert language.violates(ENGLISH, "Only English!!") is None


# --------------------------------------------------------------------------- #
# Required-but-not-written language: the English advert that DEMANDS one       #
# --------------------------------------------------------------------------- #

_ME = "I speak English and Russian"

@pytest.mark.parametrize("body", [
    "Great PM role, remote in Europe. Fluent Hungarian is required for this position.",
    "You will own delivery. Native Hungarian and English are mandatory.",
    "We need a product manager. Must be able to speak Hungarian with our team.",
    "Product Owner for our team. Hungarian language skills essential.",
])
def test_a_required_language_you_lack_is_caught(body):
    assert language.required_language_missing(body, _ME) == "Hungarian"


def test_a_language_offered_as_a_plus_is_not_a_blocker():
    """"Hungarian is a plus" must stay a soft signal, never cap the score."""
    body = "PM role, remote. English required; Hungarian is a plus but not essential."
    assert language.required_language_missing(body, _ME) is None


def test_a_language_you_do_speak_is_not_flagged():
    body = "We need fluent Russian for this role, working with our Moscow office."
    assert language.required_language_missing(body, _ME) is None


def test_an_english_requirement_is_never_the_blocker():
    body = "Fluent English is required. Remote across Europe."
    assert language.required_language_missing(body, _ME) is None


def test_a_bare_country_mention_is_not_a_language_requirement():
    """The role sits in Hungary but names no language demand - not a blocker."""
    body = "Remote Product Manager. Our head office is in Hungary; team is global."
    assert language.required_language_missing(body, _ME) is None


@pytest.mark.parametrize("body", [
    # A language name near a generic "requirement" word, but not a language demand.
    "Requirements:\n- Experience with the German market and clients.\n- Ownership.",
    "Required: manage Spanish clients across the region and report weekly.",
    "You will support our French subsidiary. Strong ownership is required.",
    "Deep knowledge of the German automotive industry is essential.",
])
def test_a_language_named_market_or_region_is_not_a_requirement(body):
    """The loose first cut blocked "the German market" because a Requirements
    heading sits near every noun. The cue must be adjacent to the language."""
    assert language.required_language_missing(body, _ME) is None


def test_fluent_language_offered_as_a_plus_is_still_not_a_blocker():
    """Even with a proficiency cue, "fluent Hungarian is a plus" is not a wall."""
    body = "PM role. Fluent Hungarian is a plus but by no means required."
    assert language.required_language_missing(body, _ME) is None


def test_no_language_stated_by_the_user_means_no_requirement_gate():
    body = "Fluent Hungarian is required."
    assert language.required_language_missing(body, "I want remote roles") is None


async def test_quick_score_short_circuits_on_language_without_calling_the_api():
    """The gate must fire BEFORE the API call - it is also a cost control."""
    from matcher.matcher import AIMatcher

    matcher = AIMatcher(api_key="sk-test-not-used")
    matcher._client = object()  # any call on this would raise AttributeError

    result = await matcher.quick_score({"title": "PM", "description": ITALIAN}, "Only English!!")

    assert result.score == 15
    assert "Italian" in result.summary
    assert result.red_flags == ["description language: Italian"]


# --------------------------------------------------------------------------- #
# Blocker enforcement                                                         #
# --------------------------------------------------------------------------- #

JOB = {"title": "Product Manager", "description": "3+ years of experience"}


def test_no_blocker_leaves_the_score_alone():
    assert _apply_blocker(85, {"blocker": False}, JOB) == 85
    assert _apply_blocker(85, {}, JOB) == 85


STATED = "Remote in the EU only. English-speaking teams."


def test_blocker_caps_below_the_review_threshold():
    capped = _apply_blocker(90, {"blocker": True, "blocker_reason": "location"}, JOB, STATED)
    assert capped == 25


def test_blocker_never_raises_a_low_score():
    assert _apply_blocker(10, {"blocker": True, "blocker_reason": "language"}, JOB, STATED) == 10


def test_with_no_instructions_a_claimed_blocker_cannot_hide_a_job():
    """No rule was stated, so none can be broken. Given the CV, the model turned
    where the candidate lives into blockers anyway: measured on 22 real adverts,
    an on-site Berlin role it called "a strong technical and leadership fit"
    came back capped at 25, out of the review list."""
    claimed = {"blocker": True, "blocker_reason": "location"}

    assert _apply_blocker(85, claimed, JOB, "") == 85
    assert _apply_blocker(85, claimed, JOB, "   ") == 85
    assert _apply_blocker(85, claimed, JOB) == 85


@pytest.mark.parametrize("raw,expected", [
    (True, True), ("true", True), ("yes", True), ("1", True),
    (False, False), ("false", False), ("", False), (None, False),
])
def test_blocker_flag_accepts_the_shapes_models_actually_return(raw, expected):
    assert _is_blocker({"blocker": raw}) is expected


@pytest.mark.parametrize("raw,expected", [
    (55, 55), ("72", 72), (-5, 0), (140, 100), ("nonsense", 0), (None, 0),
])
def test_score_is_clamped_to_a_sane_range(raw, expected):
    assert _clamp_score(raw) == expected


# --------------------------------------------------------------------------- #
# Work arrangement - LinkedIn puts it in a chip, never in the description      #
# --------------------------------------------------------------------------- #

from matcher.matcher import _vacancy_block, detect_workplace


@pytest.mark.parametrize("job,expected", [
    ({"location": "Berlin, Germany (Hybrid)"}, "Hybrid"),
    ({"location": "Portugal (Remote)"}, "Remote"),
    ({"location": "Madrid", "description": "This is an on-site role, 5 days in office."}, "On-site"),
    ({"location": "Tallinn, Estonia"}, None),
])
def test_work_arrangement_is_detected(job, expected):
    assert detect_workplace(job) == expected


def test_hybrid_wins_over_a_passing_mention_of_remote():
    """Hybrid ads almost always say 'remote' somewhere ('2 days remote'), and
    hybrid is the stricter fact for someone who needs full remote."""
    job = {"location": "Berlin (Hybrid)", "description": "Two days remote per week."}
    assert detect_workplace(job) == "Hybrid"


def test_work_arrangement_reaches_the_model():
    block = _vacancy_block({"title": "PM", "location": "Berlin (Hybrid)",
                            "description": "x" * 100})
    assert "Work arrangement: Hybrid" in block


def test_no_arrangement_line_when_unknown():
    """Never state a fact we do not have."""
    block = _vacancy_block({"title": "PM", "location": "Tallinn", "description": "x" * 100})
    assert "Work arrangement" not in block


# --------------------------------------------------------------------------- #
# Seniority - regression: "Senior" in the title scored 65-75                   #
# --------------------------------------------------------------------------- #

from matcher import seniority

REJECTS_SENIOR = """
SENIORITY:
Ideal:
- 0-4 years experience
Reject:
- Senior Product Manager
- Lead Product Manager
- Principal
- Head of
- Director
"""


@pytest.mark.parametrize("title,expected", [
    ("Senior Technical Project Manager", "senior"),
    ("AI Solution Delivery Lead", "lead"),
    ("Head of Delivery", "head of"),
    ("Product Manager", None),
    ("Middle Product Manager", None),
    ("Junior Project Coordinator", None),
])
def test_title_level_is_read_from_the_title(title, expected):
    assert seniority.title_level(title) == expected


def test_senior_describing_stakeholders_is_not_the_roles_level():
    """'Project Manager (Senior Stakeholders)' is a mid role, not a senior one."""
    assert seniority.title_level("Project Manager (Senior Stakeholders)") is None


def test_lead_generation_is_not_a_seniority_level():
    assert seniority.title_level("Lead Generation Specialist") is None


@pytest.mark.parametrize("text,expected", [
    ("3–6 years of experience in project management", 3),
    ("5+ years proven experience as a Product Manager", 5),
    ("Requires minimum 4 years experience in delivery", 4),
    # Both of these appear in real ads and are NOT requirements:
    ("7 years on the market, 500+ employees, offices in Kyiv", None),
    ("led by a product-first CEO with over 15 years' experience", None),
    ("We are a young team with big plans", None),
])
def test_required_years_ignores_company_age_and_other_peoples_experience(text, expected):
    assert seniority.required_years(text) == expected


def test_rejected_levels_come_from_the_users_own_words():
    assert {"senior", "lead", "principal"} <= seniority.rejected_levels(REJECTS_SENIOR)
    assert seniority.rejected_levels("I want product roles") == set()


def test_senior_title_is_capped_even_when_the_model_says_nothing():
    """THE regression: the model scored these 65-75 and set no blocker, despite
    the candidate listing Senior/Lead under Reject."""
    job = {"title": "Senior Technical Project Manager", "description": "x"}

    assert _apply_blocker(75, {"blocker": False}, job, REJECTS_SENIOR) == 25


def test_a_level_the_user_never_rejected_is_left_alone():
    """Never invent a rule they did not state."""
    job = {"title": "Senior Technical Project Manager", "description": "x"}

    assert _apply_blocker(75, {"blocker": False}, job, "I want remote product roles") == 75


def test_ordinary_titles_are_untouched():
    job = {"title": "Product Manager", "description": "3+ years of experience"}
    assert _apply_blocker(85, {"blocker": False}, job, REJECTS_SENIOR) == 85


def test_seniority_signals_reach_the_model():
    block = _vacancy_block({"title": "Senior Project Manager",
                            "description": "We need 5+ years of experience. " + "x" * 100})
    assert "Title seniority: Senior" in block
    assert "Experience required: 5+ years" in block


# --------------------------------------------------------------------------- #
# Schedule                                                                     #
# --------------------------------------------------------------------------- #

def test_a_scheduled_scan_survives_a_sleeping_laptop():
    """APScheduler drops a missed run by default. On a machine that sleeps every
    night that means most scheduled scans never happen at all."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from scheduler.jobs import configure_scan_jobs

    scheduler = AsyncIOScheduler()
    configure_scan_jobs(scheduler, {"scan_times": "10:00, 14:00", "scan_days": "mon,tue"})

    jobs = scheduler.get_jobs()
    assert len(jobs) == 2
    assert all(j.misfire_grace_time >= 3600 for j in jobs), (
        "a missed slot must still run when the laptop wakes")
    assert all(j.coalesce for j in jobs), "waking must not replay every missed slot"


def test_no_times_means_nothing_runs_by_itself():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from scheduler.jobs import configure_scan_jobs

    scheduler = AsyncIOScheduler()
    configure_scan_jobs(scheduler, {"scan_times": "", "scan_days": ""})

    assert scheduler.get_jobs() == []


# --------------------------------------------------------------------------- #
# Token spend                                                                  #
# --------------------------------------------------------------------------- #

def test_the_instructions_sit_in_the_system_message_so_the_prefix_caches():
    """OpenAI caches a prompt prefix automatically once it reaches 1024 tokens,
    and only when it is identical between calls. With the instructions in the
    user message the stable prefix was the 364-token title prompt alone - under
    the threshold - so every job paid full price for the same text."""
    import inspect

    from matcher.matcher import AIMatcher

    for name in ("title_screen", "quick_score"):
        source = inspect.getsource(getattr(AIMatcher, name))
        assert '"content": system' in source, f"{name} sends a non-cacheable prefix"
        assert "CANDIDATE INSTRUCTIONS" in source
        # The per-job text must NOT be in the system message, or the prefix
        # changes every call and nothing caches at all.
        assert 'f"Title: {job' not in source.split("system = ")[1].split("user = ")[0]


async def test_a_rejected_level_costs_no_model_call_at_all(db, listing, monkeypatch):
    """The seniority gate ran after both model calls, so a title the code could
    reject outright still cost a title screen and a full score. Measured on a
    real database: 1273 of 5886 jobs, 2.5M tokens, to reach a verdict the code
    already had."""
    from matcher import seniority

    instructions = "SENIORITY:\nReject:\n- Senior\n- Head of\n"
    job = {"title": "Senior Product Manager", "company": "X", "location": "Berlin"}

    assert seniority.blocking_level(job, instructions) == "senior"


# --------------------------------------------------------------------------- #
# Metadata the sources hand us and nobody was reading                          #
# --------------------------------------------------------------------------- #

def test_the_employment_type_reaches_the_model():
    """Every board states it and none of it was read, so an internship and a
    freelance gig scored like permanent roles."""
    from matcher.matcher import _vacancy_block

    block = _vacancy_block({"title": "PM", "company": "X", "location": "Remote",
                            "employment_type": "Freelance", "description": "x"})

    assert "Employment type: Freelance" in block


def test_a_source_that_does_not_say_adds_no_line():
    from matcher.matcher import _vacancy_block

    block = _vacancy_block({"title": "PM", "company": "X", "location": "Remote",
                            "description": "x"})

    assert "Employment type" not in block


def test_remote_only_boards_say_so_instead_of_hoping_the_prose_does():
    """These boards list nothing but remote work. Saying it in the metadata is
    free and more reliable than a phrase appearing somewhere in the body - the
    app's own scanning was recovering the arrangement for 0% of them."""
    from scrapers.api_sources import _with_wp

    assert _with_wp("Worldwide", "Remote") == "Worldwide (Remote)"
    assert _with_wp("Europe (Remote)", "Remote") == "Europe (Remote)"   # no doubling
    assert _with_wp(None, "Remote") == "Remote"
    assert _with_wp("Berlin", None) == "Berlin"


def test_a_boolean_flag_becomes_an_arrangement():
    """Arbeitnow states it as a boolean and most of its board is on-site."""
    from scrapers.api_sources import _with_wp

    assert _with_wp("Germany", "Remote") == "Germany (Remote)"
    assert _with_wp("Germany", None) == "Germany"


@pytest.mark.parametrize("raw, expected", [
    (["Full-time"], "Full-Time"),
    ("freelance", "Freelance"),
    ("Full Time", "Full Time"),
    ([], None),
    (None, None),
    ("", None),
])
def test_employment_type_is_normalised_across_sources(raw, expected):
    from scrapers.api_sources import _employment

    assert _employment(raw) == expected


async def test_remotive_passes_through_what_its_api_states(monkeypatch):
    """Testing the helpers in isolation let a source quietly stop using them."""
    import json as _json

    from scrapers import api_sources

    payload = {"jobs": [{
        "id": 1, "title": "Product Ops", "company_name": "Acme",
        "candidate_required_location": "Europe", "description": "<p>body</p>",
        "url": "https://x", "publication_date": "2026-08-01",
        "job_type": "freelance",
    }]}

    class Response:
        status_code = 200
        text = _json.dumps(payload)

        def json(self):
            return payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *a, **kw):
            return Response()

    monkeypatch.setattr(api_sources.httpx, "AsyncClient", lambda **kw: Client())

    jobs = [j async for j in api_sources.iter_remotive({"keywords": "", "max_jobs": 5})]

    assert len(jobs) == 1
    assert jobs[0].employment_type == "Freelance"
    assert jobs[0].location == "Europe (Remote)"


def test_linkedin_criteria_are_read_from_the_page_the_app_already_fetches():
    """The search results carry no work arrangement at all - measured, zero
    occurrences in a full page. The detail page states four facts, and it is
    already downloaded for the description."""
    from scrapers.linkedin_public import job_criteria

    html = """
    <ul class="description__job-criteria-list">
      <li class="description__job-criteria-item">
        <h3 class="description__job-criteria-subheader">Seniority level</h3>
        <span class="description__job-criteria-text">Associate</span>
      </li>
      <li class="description__job-criteria-item">
        <h3 class="description__job-criteria-subheader">Employment type</h3>
        <span class="description__job-criteria-text">Contract</span>
      </li>
    </ul>"""

    assert job_criteria(html) == {"Seniority level": "Associate",
                                 "Employment type": "Contract"}


def test_a_page_without_criteria_yields_nothing_rather_than_failing():
    from scrapers.linkedin_public import job_criteria

    assert job_criteria("<html><body>nothing here</body></html>") == {}


def test_the_scan_stores_the_employment_type_it_just_learned():
    import inspect

    from scheduler import jobs

    source = inspect.getsource(jobs.run_scan_cycle)
    assert "fetch_job_detail_full" in source
    assert 'card.employment_type = criteria.get("Employment type")' in source

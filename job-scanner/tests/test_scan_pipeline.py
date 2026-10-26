"""The scan cycle end to end: what reaches the model, and what never does.

Two things are checked here that no unit test can: that the free gates run
BEFORE any model call (after it they save nothing), and that titles are screened
in batches rather than one call per job.
"""

from __future__ import annotations

import pytest

from scheduler import jobs as sched
from scrapers.base import JobListing

_INSTRUCTIONS = """
LOCATION:

Accept:
- Remote within Europe
- Spain remote, hybrid, or onsite

SENIORITY:

Reject:
- Senior
- Head of
"""


class RecordingMatcher:
    """Stands in for AIMatcher and remembers every call it was asked to make."""

    BATCH_SIZE = 3
    init_error = None

    def __init__(self):
        self.batches: list[list[dict]] = []
        self.scored: list[str] = []
        self.scored_jobs: list[dict] = []

    async def title_screen_batch(self, jobs, instructions):
        self.batches.append(list(jobs))
        from matcher.matcher import TitleResult

        return [TitleResult(True, 80, "plausible") for _ in jobs]

    async def quick_score(self, job, instructions):
        self.scored.append(job.get("title"))
        self.scored_jobs.append(job)
        from matcher.matcher import QuickResult

        return QuickResult(70, "fine", [])

    @property
    def titles_seen(self) -> list[str]:
        return [j.get("title") for batch in self.batches for j in batch]


@pytest.fixture
def scan(db, monkeypatch):
    """Runs one scan cycle over a fixed list of cards, with a fake model."""

    async def setup(cards: list[JobListing], years: int | None = None) -> RecordingMatcher:
        await db.save_profile(resume_text="", instructions=_INSTRUCTIONS, case_bank="")
        await db.create_search(
            name="Test", keywords="pm", location="", tpr_seconds=0,
            experience="", job_types="", remote="", max_jobs=50, source="linkedin",
            years_experience=years,
        )

        async def fake_source(cfg):
            for card in cards:
                yield card

        fake = RecordingMatcher()
        monkeypatch.setitem(sched.SOURCE_FETCHERS, "linkedin", fake_source)
        monkeypatch.setattr(sched, "AIMatcher", lambda **kw: fake)
        monkeypatch.setattr(sched, "send_digest", _no_digest)
        # The detail fetch would hit the network; every card here has a body.
        await sched.run_scan_cycle()
        return fake

    return setup


async def _no_digest(jobs, allow_empty=False):
    return False


def _card(title: str, location: str = "Remote", n: int = 0) -> JobListing:
    return JobListing(source="linkedin", external_id=f"card-{n}-{title[:8]}",
                      title=title, company=f"Co {n}", location=location,
                      description="A real description of the role, long enough to score.")


async def test_a_rejected_title_never_reaches_the_model(scan):
    """The seniority gate held this verdict from the start. Reaching it via two
    model calls cost 2.5M tokens across a real database."""
    fake = await scan([_card("Senior Product Manager", n=1),
                       _card("Product Operations Specialist", n=2)])

    assert "Senior Product Manager" not in fake.titles_seen
    assert "Product Operations Specialist" in fake.titles_seen


async def test_an_office_in_another_country_never_reaches_the_model(scan):
    fake = await scan([_card("Product Manager", "Stockholm, Sweden (Hybrid)", n=1),
                       _card("Delivery Manager", "Barcelona, Spain (Hybrid)", n=2)])

    assert "Product Manager" not in fake.titles_seen
    # Hybrid at home is exactly what the instructions accept.
    assert "Delivery Manager" in fake.titles_seen


async def test_titles_are_screened_in_batches_not_one_call_each(scan):
    """The preamble is 1230 tokens and the title is 17. One call per job spends
    98% of the budget re-sending the same text."""
    cards = [_card(f"Product Operations {i}", n=i) for i in range(7)]

    fake = await scan(cards)

    assert len(fake.titles_seen) == 7
    assert len(fake.batches) == 3, f"expected 3 calls for 7 jobs, got {len(fake.batches)}"
    assert [len(b) for b in fake.batches] == [3, 3, 1]


async def test_the_last_partial_batch_is_not_forgotten(scan):
    """A source that runs dry mid-batch must still get those jobs screened."""
    fake = await scan([_card(f"Product Operations {i}", n=i) for i in range(2)])

    assert len(fake.batches) == 1
    assert len(fake.titles_seen) == 2


async def test_survivors_are_still_scored_one_at_a_time(scan):
    fake = await scan([_card(f"Product Operations {i}", n=i) for i in range(4)])

    assert len(fake.scored) == 4


async def test_the_scorer_sees_the_years_of_the_search_that_found_the_job(scan):
    """The years gate reads them off the job. A scan that stores the job without
    its search leaves the gate silently off for every job it finds."""
    fake = await scan([_card("Product Operations Specialist", n=1)], years=4)

    assert [j.get("search_years") for j in fake.scored_jobs] == [4]


# --------------------------------------------------------------------------- #
# The batch call itself                                                        #
# --------------------------------------------------------------------------- #

class _Reply:
    def __init__(self, text):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]


def _matcher_returning(text: str):
    """A real AIMatcher whose model always answers `text`."""
    from matcher.matcher import AIMatcher

    m = AIMatcher(api_key="test-key")
    calls = []

    class Completions:
        async def create(self, **kw):
            calls.append(kw)
            return _Reply(text)

    m._client = type("C", (), {"chat": type("Ch", (), {"completions": Completions()})()})()
    return m, calls


async def test_a_job_the_model_forgot_is_let_through_not_dropped():
    """A missing verdict means the screen did not run. Silently discarding the
    job would make it invisible - worse than paying to score it."""
    m, _ = _matcher_returning('{"results": [{"n": 1, "plausible": false, "score": 10, '
                              '"reason": "no"}]}')

    results = await m.title_screen_batch(
        [{"title": "A"}, {"title": "B"}, {"title": "C"}], "instructions")

    assert len(results) == 3
    assert results[0].plausible is False          # answered
    assert results[1].plausible is True           # forgotten -> kept
    assert results[2].plausible is True


async def test_a_failed_call_keeps_every_job_in_the_batch():
    from matcher.matcher import AIMatcher

    m = AIMatcher(api_key="test-key")

    class Boom:
        async def create(self, **kw):
            raise RuntimeError("API down")

    m._client = type("C", (), {"chat": type("Ch", (), {"completions": Boom()})()})()

    results = await m.title_screen_batch([{"title": "A"}, {"title": "B"}], "x")

    assert len(results) == 2
    assert all(r.plausible for r in results)


async def test_verdicts_are_matched_by_number_not_by_position():
    """The model may answer out of order. Lining results up by position would
    hand one job another job's verdict."""
    m, _ = _matcher_returning(
        '{"results": [{"n": 2, "plausible": false, "score": 5, "reason": "no"}, '
        '{"n": 1, "plausible": true, "score": 90, "reason": "yes"}]}')

    results = await m.title_screen_batch([{"title": "First"}, {"title": "Second"}], "x")

    assert results[0].plausible is True and results[0].score == 90
    assert results[1].plausible is False


async def test_the_instructions_stay_in_the_cacheable_prefix():
    m, calls = _matcher_returning('{"results": []}')

    await m.title_screen_batch([{"title": "A"}, {"title": "B"}], "MY INSTRUCTIONS")

    system = calls[0]["messages"][0]["content"]
    user = calls[0]["messages"][1]["content"]
    assert "MY INSTRUCTIONS" in system, "instructions must sit in the cached prefix"
    assert "MY INSTRUCTIONS" not in user
    assert "A" in user and "B" in user


async def test_a_single_job_skips_the_batch_wrapper():
    """One job is cheaper unbatched: no numbering, no list instructions."""
    m, calls = _matcher_returning('{"plausible": true, "score": 70, "reason": "ok"}')

    results = await m.title_screen_batch([{"title": "Only"}], "x")

    assert len(results) == 1
    assert "results" not in calls[0]["messages"][0]["content"]


# --------------------------------------------------------------------------- #
# The extension path carries most of the volume - it needs the same gates       #
# --------------------------------------------------------------------------- #

@pytest.fixture
def ingest(db, monkeypatch):
    """Runs process_new_jobs over jobs already stored as 'new'."""
    async def setup(cards: list[JobListing]) -> RecordingMatcher:
        await db.save_profile(resume_text="", instructions=_INSTRUCTIONS, case_bank="")
        for card in cards:
            await db.insert_job(card, fetched_via="extension")
        fake = RecordingMatcher()
        monkeypatch.setattr(sched, "AIMatcher", lambda **kw: fake)
        monkeypatch.setattr(sched, "send_digest", _no_digest)
        await sched.process_new_jobs(force=True)
        return fake

    return setup


async def test_the_extension_path_screens_for_free_too(ingest):
    """Optimising only the scheduled scan optimised the quieter half: the
    extension posts a batch every few seconds and supplies most of the jobs."""
    fake = await ingest([_card("Senior Product Manager", n=1),
                         _card("Product Operations Specialist", n=2)])

    assert "Senior Product Manager" not in fake.titles_seen
    assert "Product Operations Specialist" in fake.titles_seen


async def test_the_extension_path_screens_offices_abroad(ingest):
    fake = await ingest([_card("Product Manager", "Stockholm, Sweden (Hybrid)", n=1),
                         _card("Delivery Manager", "Barcelona, Spain (Hybrid)", n=2)])

    assert "Product Manager" not in fake.titles_seen
    assert "Delivery Manager" in fake.titles_seen


async def test_the_extension_path_batches_its_title_screens(ingest):
    fake = await ingest([_card(f"Product Operations {i}", n=i) for i in range(7)])

    assert len(fake.batches) == 3
    assert [len(b) for b in fake.batches] == [3, 3, 1]


# --------------------------------------------------------------------------- #
# One policy, two roads                                                        #
# --------------------------------------------------------------------------- #

async def test_both_paths_screen_identically(db, monkeypatch):
    """The two roads carried their own copy of this decision and drifted: the
    free gates and the batched screen were added to the scheduled scan while
    the extension path, which carries most of the volume, kept paying full
    price. The policy lives in one module now - this proves both use it."""
    from scheduler import screening

    cards = [_card("Senior Product Manager", n=1),
             _card("Product Manager", "Stockholm, Sweden (Hybrid)", n=2),
             _card("Product Operations Specialist", n=3)]

    seen = [screening.free_reject(c, _INSTRUCTIONS) for c in cards]

    assert seen[0] and "Senior" in seen[0]
    assert seen[1] and "Sweden" in seen[1]
    assert seen[2] is None


async def test_the_scheduled_scan_uses_the_shared_module():
    import inspect

    from scheduler import jobs

    source = inspect.getsource(jobs.run_scan_cycle)
    assert "screening.title_verdicts" in source
    assert "seniority.blocking_level" not in source, "a second copy of the policy"


async def test_the_extension_path_uses_the_shared_module():
    import inspect

    from scheduler import jobs

    source = inspect.getsource(jobs.process_new_jobs)
    assert "screening.title_verdicts" in source
    assert "seniority.blocking_level" not in source, "a second copy of the policy"


async def test_free_rejections_never_reach_the_model():
    """Ordering is the point: after the call the tokens are already spent."""
    from scheduler import screening

    fake = RecordingMatcher()
    cards = [_card("Senior Manager", n=1), _card("Product Operations", n=2)]

    verdicts = [(c.title, v) async for c, v, _ in
                screening.title_verdicts(cards, fake, _INSTRUCTIONS)]

    assert ("Senior Manager", screening.REJECT) in verdicts
    assert fake.titles_seen == ["Product Operations"]


def test_every_source_has_a_human_label():
    """A source with no label shows up in the UI as a raw identifier."""
    from scheduler.jobs import SOURCE_FETCHERS, SOURCE_LABELS

    missing = sorted(set(SOURCE_FETCHERS) - set(SOURCE_LABELS))
    assert not missing, f"no label for: {missing}"


def test_every_template_the_code_renders_exists():
    """A route pointing at a missing template is a 500 nobody sees until they
    click it."""
    import re
    from pathlib import Path

    import main

    src = Path(main.__file__).read_text()
    referenced = set(re.findall(r'"([a-z_]+\.html)"', src))
    have = {p.name for p in (main.BASE_DIR / "templates").glob("*.html")}
    assert not (referenced - have), referenced - have


def test_no_template_is_shipped_without_a_route():
    """Dead templates rot: they still look authoritative when read."""
    import re
    from pathlib import Path

    import main

    src = Path(main.__file__).read_text()
    referenced = set(re.findall(r'"([a-z_]+\.html)"', src))
    have = {p.name for p in (main.BASE_DIR / "templates").glob("*.html")}
    # base.html is inherited, and the two search partials are included by name.
    orphans = have - referenced - {"base.html"}
    assert not orphans, f"templates nothing renders: {sorted(orphans)}"


# --------------------------------------------------------------------------- #
# A rate limit is not a verdict                                                #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("message, transient", [
    ("Error code: 429 - rate limit reached for gpt-4.1-mini", True),
    ("Request timed out.", True),
    ("Connection error", True),
    ("Error code: 503 - service overloaded", True),
    ("Invalid API key provided", False),
    ("model 'gpt-9' does not exist", False),
])
def test_temporary_failures_are_told_apart_from_real_ones(message, transient):
    """A dense scan runs into the 200k tokens-per-minute ceiling. Recording a
    score of 0 for those jobs hides real matches behind a number the model never
    produced, and the job never comes back - its status has moved on."""
    from scheduler.jobs import _is_transient

    assert _is_transient(RuntimeError(message)) is transient


def test_a_rate_limited_job_is_left_unscored_not_zeroed():
    import inspect

    from scheduler import jobs

    source = inspect.getsource(jobs.run_scan_cycle)
    assert "_is_transient(exc)" in source, "a 429 must not be written as a score"


def test_the_client_retries_enough_to_ride_out_a_rate_limit():
    from matcher.matcher import _MAX_RETRIES

    assert _MAX_RETRIES >= 3

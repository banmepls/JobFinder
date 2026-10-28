"""User-authored inputs that feed generation: the per-job cover-letter brief,
inline CV edits, and the CV itself as the AI reads it. The first two write to
storage, so they can lose work if they regress; the last can hide half the CV
from the AI while the page still shows all of it."""

from cv_content import cv_to_text
from matcher.generate import _context


# --------------------------------------------------------------------------- #
# Cover-letter brief                                                          #
# --------------------------------------------------------------------------- #

def test_brief_is_put_in_front_of_the_generation_context():
    """It must outrank the resume/instructions blocks, not be buried after them."""
    ctx = _context({"title": "PM"}, {"resume_text": "R", "instructions": "I"},
                   brief="mention my Shopify work")

    assert "mention my Shopify work" in ctx
    assert ctx.index("mention my Shopify work") < ctx.index("CANDIDATE RESUME")


def test_no_brief_leaves_the_context_unchanged():
    plain = _context({"title": "PM"}, {"resume_text": "R"})
    blank = _context({"title": "PM"}, {"resume_text": "R"}, brief="   ")

    assert plain == blank
    assert "asked you to include" not in plain.lower()


def test_brief_forbids_inventing_evidence():
    """The letter must not fabricate experience to satisfy the note."""
    ctx = _context({"title": "PM"}, {}, brief="say I led a 20-person team")
    assert "not invent evidence" in ctx.lower()


async def test_brief_is_stored_per_job_and_survives_regeneration(db, add_job):
    from main import CL_BRIEF_KIND

    job_id = await add_job(score=90)
    await db.save_asset(job_id, CL_BRIEF_KIND, "mention Shopify")

    assert (await db.get_assets(job_id))[CL_BRIEF_KIND] == "mention Shopify"

    # Regenerating the letter itself must not clobber the note.
    await db.save_asset(job_id, "cover_letter", "Dear hiring manager…")
    assert (await db.get_assets(job_id))[CL_BRIEF_KIND] == "mention Shopify"


async def test_brief_belongs_to_one_job_only(db, add_job):
    from main import CL_BRIEF_KIND

    a = await add_job(score=90, external_id="a")
    b = await add_job(score=90, external_id="b")
    await db.save_asset(a, CL_BRIEF_KIND, "for A")

    assert (await db.get_assets(b)).get(CL_BRIEF_KIND) is None


# --------------------------------------------------------------------------- #
# Inline CV edits                                                             #
# --------------------------------------------------------------------------- #

async def test_saving_cv_edits_keeps_the_pristine_baseline(db):
    """The left-hand 'Original' column must stay stable while you edit the right."""
    await db.save_cv_html("<p>original</p>", set_original=True)

    await db.save_cv_html("<p>edited in the tab</p>")

    profile = await db.get_profile()
    assert profile["cv_html"] == "<p>edited in the tab</p>"
    assert profile["cv_original_html"] == "<p>original</p>"


async def test_uploading_a_new_cv_resets_the_baseline(db):
    await db.save_cv_html("<p>first</p>", set_original=True)
    await db.save_cv_html("<p>hand edit</p>")

    await db.save_cv_html("<p>freshly uploaded</p>", set_original=True)

    profile = await db.get_profile()
    assert profile["cv_html"] == profile["cv_original_html"] == "<p>freshly uploaded</p>"


# --------------------------------------------------------------------------- #
# Conversational CV editing                                                   #
# --------------------------------------------------------------------------- #

def test_cv_chat_prompt_forbids_partial_documents_and_invention():
    """Two failure modes that would quietly wreck a CV: returning only the edited
    fragment (the rest of the document vanishes) and inventing employers."""
    from matcher.generate import _CV_CHAT_SYSTEM

    text = _CV_CHAT_SYSTEM.lower()
    assert "complete" in text and "never return only the edited" in text
    assert "never invent" in text
    assert "cv-name" in text, "the .cv-doc structure must be preserved"


async def test_cv_chat_rejects_an_empty_instruction(db):
    """Guard: no instruction means no API call and no token spend."""
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as client:
        r = client.post("/cv/chat", data={"instruction": "   ", "html": "<p>cv</p>"})
        assert r.status_code == 400


async def test_cv_chat_needs_a_cv(db):
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as client:
        r = client.post("/cv/chat", data={"instruction": "shorten it", "html": ""})
        assert r.status_code == 400
        assert "CV" in r.json()["error"]


# --------------------------------------------------------------------------- #
# What a cover letter is for                                                   #
# --------------------------------------------------------------------------- #

def test_the_letter_uses_experience_as_alignment_not_as_a_record():
    """Experience is the material, but the letter is not a prose resume. An
    earlier version of this prompt over-corrected and all but banned experience,
    which is not the same thing as not listing achievements."""
    from matcher.generate import _SYSTEM

    prompt = _SYSTEM["cover_letter"]

    assert "case bank" in prompt.lower()
    assert "EXPERIENCE IS THE EVIDENCE" in prompt
    # The old instruction to recite two achievements with metrics is gone.
    assert "MUST draw on the CASE BANK" not in prompt
    assert "cite at least two specific project stories" not in prompt


def test_the_letter_prompt_shows_the_model_both_a_right_and_a_wrong_example():
    """A rule this fine-grained is followed far more reliably when the model is
    shown the failure mode next to the fix."""
    from matcher.generate import _SYSTEM

    prompt = _SYSTEM["cover_letter"]

    assert "* Right:" in prompt and "* Wrong:" in prompt


def test_the_letter_prompt_asks_about_values_on_both_sides():
    from matcher.generate import _SYSTEM

    prompt = _SYSTEM["cover_letter"].lower()

    assert "values" in prompt


def test_the_letter_prompt_asks_the_three_questions_a_resume_cannot_answer():
    from matcher.generate import _SYSTEM

    prompt = _SYSTEM["cover_letter"].lower()

    assert "why this company" in prompt
    assert "why this role" in prompt
    assert "why them" in prompt


def test_the_letter_prompt_still_bans_machine_punctuation():
    from matcher.generate import _SYSTEM

    prompt = _SYSTEM["cover_letter"]

    assert "NEVER use an em dash" in prompt
    assert "NEVER use a semicolon" in prompt
    for word in ("delve", "leverage", "spearheaded", "seamless", "synergy"):
        assert word in prompt


def test_the_letter_is_short_enough_to_be_read():
    from matcher.generate import _SYSTEM

    assert "180-260 words" in _SYSTEM["cover_letter"]


# --------------------------------------------------------------------------- #
# The CV as the AI reads it                                                    #
# --------------------------------------------------------------------------- #

# Two jobs, in exactly the layout the import prompt asks the model for.
_TWO_JOBS = (
    '<div class="cv-name">Alex Example</div>'
    '<div class="cv-subtitle"><strong>Product Operations Manager</strong> | Berlin</div>'
    '<h2>Experience</h2>'
    '<div class="entry"><div class="entry-head"><span><strong>Product Operations Manager | '
    'Tailspin Software</strong> | Berlin</span><span class="date">2021 – Present</span></div>'
    '<p class="entry-desc">Logistics software, 200 people.</p>'
    '<ul><li>Ran quarterly planning for 6 squads.</li></ul></div>'
    '<div class="entry"><div class="entry-head"><span><strong>Program Manager | Adventure '
    'Works</strong> | Munich</span><span class="date">2018 – 2021</span></div>'
    '<ul><li>Coordinated delivery for a 40-person org.</li></ul></div>'
)


def test_the_ai_sees_which_job_each_bullet_belongs_to():
    """An entry's heading is a div, which the plain-text version skipped: the AI
    got every bullet as one list with no title, employer or date - it could not
    tell a CTO's work from a junior developer's, or count years - while the
    Profile page showed the CV whole. Found on a real CV, 28 Oct 2026."""
    text = cv_to_text(_TWO_JOBS)

    first = "Product Operations Manager | Tailspin Software | Berlin | 2021 – Present"
    second = "Program Manager | Adventure Works | Munich | 2018 – 2021"
    assert first in text and second in text
    # Each heading sits above its own bullets.
    assert (text.index(first) < text.index("Ran quarterly planning")
            < text.index(second) < text.index("Coordinated delivery"))


def test_a_heading_with_no_date_is_still_read():
    """The no-AI fallback importer writes the whole line into the heading with
    no date span."""
    head = '<div class="entry-head"><span><strong>Freelance | Lisbon 2026</strong></span></div>'

    assert "Freelance | Lisbon 2026" in cv_to_text(head)


def test_each_role_says_how_long_it_lasted():
    """Lengths are counted here, not asked of the model, which read eight years
    as short of "5+" when left to count dates itself."""
    from datetime import date

    text = cv_to_text(_TWO_JOBS, today=date(2026, 10, 28))

    assert "2021 – Present (5 yrs 10 mos)" in text
    assert "2018 – 2021 (3 yrs)" in text


def test_overlapping_roles_count_once_and_unknown_dates_are_not_zero():
    """Three roles at one company overlap at their edges; counted twice they
    would inflate the years. And a date nobody can read is unknown, not zero:
    a search set to zero years would lower every advert asking for any."""
    from datetime import date

    from cv_content import date_span, months_in, years_in

    today = date(2026, 10, 28)
    promoted = [date_span(s, today) for s in
                ("Jan 2025 – Jul 2026", "Oct 2023 – Jan 2025", "Feb 2023 – Oct 2023")]
    side_job = [date_span(s, today) for s in ("Jan 2020 – Dec 2021", "Jan 2021 – Dec 2022")]

    assert months_in(promoted) == 42     # Feb 2023 - Jul 2026; summed it is 44
    assert years_in(side_job) == 3       # three years of calendar; summed it is four
    assert years_in([date_span("Freelance, on and off", today)]) is None
    assert years_in([]) is None


def test_a_company_description_has_a_place_and_reaches_the_ai():
    """The import prompt gave a company's one-line description nowhere to go, and
    on a real CV the model dropped all three despite being told to keep every
    word. The edit prompt has to name the class too, or an edit strips it."""
    from matcher.generate import _CV_CHAT_SYSTEM, _CV_STRUCTURE_SYSTEM

    assert 'class="entry-desc"' in _CV_STRUCTURE_SYSTEM
    assert "p.entry-desc" in _CV_CHAT_SYSTEM
    assert "Logistics software, 200 people." in cv_to_text(_TWO_JOBS)

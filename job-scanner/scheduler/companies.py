"""The company search: the register, a ranking against the CV, the companies'
own websites and careers pages - run in the background from the Companies page.

One run takes the next page of register matches, ranks every company not ranked
yet, then checks the websites of the best fits. Openings read off a careers page
become jobs and go through the normal scoring; everything else stays on the
Companies page as a target to approach directly. Nothing here deletes or
overwrites what the user decided about a company.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from apscheduler.triggers.interval import IntervalTrigger

import providers
from db import queries
from matcher import companies as ai
from matcher import language
from scrapers import company_language, osm_offices
from scrapers import swiss_register as register
from scrapers.base import JobListing, strip_html

logger = logging.getLogger(__name__)

COUNTRY_NAMES = {"CH": "Switzerland", **osm_offices.COUNTRIES}
PAGE = 200          # register matches taken per run
# Map offices taken per country per run. Each costs a homepage fetch before it
# can be ranked, so fewer than the register's 200.
OSM_PAGE = 100
MIN_FIT = 60        # ranked at least this well before a website is looked for
CHECK_LIMIT = 40    # websites checked per run - each costs up to ~14 requests
# Seconds between model calls. Gemini's free tier allows 15 requests a minute;
# a run makes one per 15 companies ranked and one per careers page read.
_PACE = 4.5

# Automatic runs. A check every half hour rather than a fixed time of day: the
# desktop app is open when the user has it open, and a run due while it was
# closed or asleep happens at the next check instead of being skipped.
AUTO = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}
AUTO_LABELS = {"": "Off", "daily": "Every day", "weekly": "Every week"}
TICK = timedelta(minutes=30)
_JOB_ID = "companies_auto"

_state: dict = {"running": False}


def status() -> dict:
    return dict(_state)


def _set(**kw) -> None:
    _state.update(kw)


def countries_of(settings_row: dict) -> list[str]:
    """The countries the company search covers; Switzerland when none is set."""
    picked = [c for c in (settings_row.get("company_countries") or "").split(",")
              if c in COUNTRY_NAMES]
    return picked or ["CH"]


def link(uid: str) -> str:
    """Where a company's entry can be seen: its OpenStreetMap office, or its
    entry in the Swiss federal UID register."""
    return osm_offices.element_url(uid) or register.register_url(uid)


def _where(company: dict) -> str:
    """'Zug, Switzerland' - the town and the country, whichever are known."""
    return ", ".join(x for x in (company.get("town"),
                                 COUNTRY_NAMES.get(company.get("country") or "CH")) if x)


async def my_languages(settings_row: dict | None = None) -> tuple[set[str], str]:
    """The languages the user works in, and where they came from: "chosen" on
    the Companies page, else "cv" - the CV's languages section and any named in
    the AI instructions. An empty set means no language filter, and so does a
    CV that lists none of Switzerland's: hiding every company is no answer."""
    row = settings_row if settings_row is not None else await queries.get_app_settings()
    chosen = row.get("company_languages")
    if chosen is not None:
        return {c for c in chosen.split(",") if c}, "chosen"
    profile = await queries.get_profile_full()
    found = (language.cv_languages(profile.get("resume_text"))
             | language.accepted_languages(profile.get("instructions")))
    return (found if found & set(language.OFFERED) else set()), "cv"


async def run(terms_text: str, automatic: bool = False) -> None:
    """One company search. Never raises: an error ends the run and is shown."""
    if _state.get("running"):
        return
    _state.clear()
    started = datetime.now(timezone.utc).isoformat()
    _set(running=True, stage="Searching the register", found=0, new=0, ranked=0,
         checked=0, sites=0, openings=0, foreign=0, error=None, automatic=automatic,
         started_at=started)
    await queries.save_company_last_run(started)
    try:
        await _run(terms_text)
    except Exception as exc:  # noqa: BLE001 - the page shows it; the next run retries
        logger.exception("Company search failed: %s", exc)
        _set(error=str(exc)[:300])
    finally:
        _set(running=False, stage="Done", finished_at=datetime.now(timezone.utc).isoformat())


async def _run(terms_text: str) -> None:
    from scheduler import jobs as sched     # the scan's helpers: same CV, same client settings

    settings_row = await queries.get_app_settings()
    countries = countries_of(settings_row)
    terms = register.clean_terms(terms_text)
    if "CH" in countries:
        if terms:
            await _swiss(terms, settings_row)
        elif countries == ["CH"]:
            _set(error="No usable search terms - one per line, letters only.")
            return
    mapped = [c for c in countries if c in osm_offices.COUNTRIES]
    if mapped:
        await _mapped(mapped, settings_row)

    cv_text = await sched.scan_cv()
    if not cv_text:
        _set(error="Upload your CV on the Profile page first - companies are ranked against it.")
        return
    ai_kw = dict(model=providers.resolve_model(settings_row, "fast"), **sched._ai(settings_row))
    if not ai_kw.get("api_key"):
        _set(error="No API key for the scanning service - add one in Settings.")
        return
    mine, _ = await my_languages(settings_row)

    # Rank. A failed batch stays unranked for the next run; never a zero.
    todo = await queries.unranked_companies()
    _set(stage="Ranking companies against your CV", total=len(todo))
    for start in range(0, len(todo), ai.RANK_BATCH):
        batch = todo[start:start + ai.RANK_BATCH]
        try:
            fits = await ai.rank_companies(batch, cv_text, **ai_kw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ranking batch failed, left for the next run: %s", exc)
            fits = {}
        for uid, (fit, reason) in fits.items():
            await queries.save_company_fit(uid, fit, reason)
        _set(ranked=_state["ranked"] + len(fits))
        await asyncio.sleep(_PACE)

    # Websites and careers pages of the best fits.
    to_check = await queries.companies_to_check(MIN_FIT, CHECK_LIMIT)
    _set(stage="Looking for their websites and careers pages", total=len(to_check))
    stored = 0
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (JobFinder company check)"}) as client:
        # Websites six at a time - plain page fetches; the model calls after
        # them stay one at a time and paced.
        gate = asyncio.Semaphore(6)

        async def site_of(company: dict):
            async with gate:
                if company.get("website_proof") == "map" and company.get("website"):
                    # The map gave the site. A homepage that does not answer
                    # today leaves it as it is - "" reads as nothing new.
                    page = await register.fetch_page(client, company["website"])
                    return company, {"site": company["website"], "proof": "map",
                                     "home": page[0] if page else ""}
                return company, await register.find_website(client, company["name"], company["uid"],
                                                            company.get("town"))

        for company, site in await asyncio.gather(*(site_of(c) for c in to_check)):
            stored += await _check(client, company, site, ai_kw, mine)
            _set(checked=_state["checked"] + 1)
    if stored:
        _set(stage="Scoring the openings found")
        await sched.process_new_jobs(force=True)


async def _swiss(terms: list[str], settings_row: dict) -> None:
    """The next page of Swiss register matches for the terms."""
    joined = "\n".join(terms)
    offset = int(settings_row.get("company_offset") or 0)
    if joined != (settings_row.get("company_terms") or ""):
        offset = 0                          # new terms: start their list from the top
    rows = await register.find_companies(terms, PAGE, offset)
    if not rows and offset:
        # Past the end of the list: start again from the top. Companies
        # registered since sit among the known ones there (the list is in UID
        # order and UIDs are not sequential), and the known ones cost nothing.
        offset = 0
        rows = await register.find_companies(terms, PAGE, offset)
    _set(found=_state["found"] + len(rows), new=_state["new"] + await queries.add_companies(rows))
    await queries.save_company_search(joined, offset + len(rows))


def _offsets(settings_row: dict) -> dict[str, int]:
    try:
        raw = json.loads(settings_row.get("company_offsets") or "{}")
    except ValueError:
        return {}
    return {k: int(v) for k, v in raw.items() if isinstance(v, int)} if isinstance(raw, dict) else {}


async def _mapped(countries: list[str], settings_row: dict) -> None:
    """The next map offices of each country, each described by its own
    homepage - the map says nothing about what a company does, and the ranking
    needs something to read. One country failing (the map service is shared
    and busy) does not stop the others."""
    offsets = _offsets(settings_row)
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (JobFinder company check)"}) as client:
        for i, country in enumerate(countries):
            if i:
                await asyncio.sleep(_PACE * 4)  # the map service is shared: one country at a time
            _set(stage=f"Reading the map: {COUNTRY_NAMES[country]}")
            offset = offsets.get(country, 0)
            try:
                rows = await osm_offices.find_offices(country, OSM_PAGE, offset)
                if not rows and offset:
                    offset = 0              # past the end: from the top, as for the register
                    rows = await osm_offices.find_offices(country, OSM_PAGE, offset)
            except Exception as exc:  # noqa: BLE001 - the next run retries the same page
                logger.warning("Map offices for %s failed: %s", country, exc)
                _set(map_errors=[*_state.get("map_errors", []), COUNTRY_NAMES[country]])
                continue
            offsets[country] = offset + len(rows)
            known = await queries.known_company_uids([r["uid"] for r in rows])
            described = await _describe(client, [r for r in rows if r["uid"] not in known])
            _set(found=_state["found"] + len(rows),
                 new=_state["new"] + await queries.add_companies(described))
            await queries.save_company_offsets(offsets)


async def _describe(client, rows: list[dict]) -> list[dict]:
    """Each office with its homepage read: what it does, and the languages its
    site is in. An office whose site does not answer is left out - a map entry
    can outlive the company - and comes back when the walk passes it again."""
    gate = asyncio.Semaphore(6)

    async def one(row: dict):
        async with gate:
            page = await register.fetch_page(client, row["website"])
        if not page:
            return None
        html, url = page
        parts = urlparse(url)
        return {**row, "website": f"{parts.scheme}://{parts.netloc}", "website_proof": "map",
                "purpose": osm_offices.describe(html, row.get("office"), row.get("purpose")),
                "site_languages": ",".join(company_language.site_languages(html)) or None}

    return [r for r in await asyncio.gather(*(one(r) for r in rows)) if r]


def next_auto_run(settings_row: dict) -> datetime | None:
    """When the next automatic run is due (UTC), or None: automatic runs are
    off, or there is nothing to search yet - Switzerland alone needs saved
    terms, a mapped country needs none."""
    every = AUTO.get(settings_row.get("company_auto") or "")
    countries = countries_of(settings_row)
    searchable = any(c in osm_offices.COUNTRIES for c in countries) or bool(
        register.clean_terms(settings_row.get("company_terms") or ""))
    if every is None or not searchable:
        return None
    try:
        last = datetime.fromisoformat(settings_row.get("company_last_run") or "")
    except ValueError:
        return datetime.now(timezone.utc)
    return (last if last.tzinfo else last.replace(tzinfo=timezone.utc)) + every


async def run_if_due() -> bool:
    """The half-hourly check. Starts a run when one is due and nothing else is
    using the scanning model - a job scan and a company run together would
    double the calls a free-tier key is allowed. Returns whether it ran."""
    from scheduler import jobs as sched

    row = await queries.get_app_settings()
    due = next_auto_run(row)
    if due is None or due > datetime.now(timezone.utc):
        return False
    if _state.get("running") or sched.is_scanning():
        return False                        # the next check tries again
    await run(row["company_terms"], automatic=True)
    return True


def schedule(scheduler) -> None:
    """Register the half-hourly check. It reads the setting each time, so
    turning automatic runs on or off needs no rescheduling. The first check is
    two minutes after start: a laptop opened for ten minutes still gets its run."""
    if scheduler is None:
        return
    scheduler.add_job(run_if_due, trigger=IntervalTrigger(seconds=TICK.total_seconds()),
                      id=_JOB_ID, max_instances=1, coalesce=True, replace_existing=True,
                      next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2))


async def _check(client, company: dict, site: dict | None, ai_kw: dict,
                 mine: set[str] = frozenset()) -> int:
    """Careers page and openings of one company, given its proven site (or
    None). Returns how many jobs were stored.

    `mine` are the user's languages. A careers page written in none of them is
    not read: the model call and the page fetches would only find adverts the
    user cannot answer. Its language is still saved, which is what hides the
    company from the list."""
    if not site:
        await queries.save_company_check(company["uid"], None, None, None, None, None)
        return 0
    _set(sites=_state["sites"] + 1)
    # Nothing read from the homepage today keeps what was read before.
    site_langs = (",".join(company_language.site_languages(site["home"]))
                  or company.get("site_languages") or None)
    careers = register.careers_link(site["home"], site["site"])
    page = await register.fetch_page(client, careers) if careers else None
    if not page:
        await queries.save_company_check(company["uid"], site["site"], site["proof"],
                                         careers, None, None, site_languages=site_langs)
        return 0
    html, url = page
    careers_lang = company_language.page_language(html)
    read = None
    if not mine or careers_lang is None or careers_lang in mine:
        read = await ai.read_careers_page(strip_html(html), register.page_links(html, url),
                                          company["name"], **ai_kw)
        await asyncio.sleep(_PACE)
    stored = kept = 0
    if read:
        for opening in read["openings"]:
            added = await _store_opening(client, company, opening, url, html, mine)
            if added is None:
                _set(foreign=_state["foreign"] + 1)
                continue
            kept += 1
            stored += added
        _set(openings=_state["openings"] + kept)
    await queries.save_company_check(
        company["uid"], site["site"], site["proof"], url,
        None if read is None else kept,
        None if read is None else read["open_applications"],
        site_languages=site_langs, careers_language=careers_lang)
    return stored


async def _store_opening(client, company: dict, opening: dict, careers_url: str,
                         careers_html: str, mine: set[str] = frozenset()) -> int | None:
    """One opening as a job: 1 when stored, 0 when it was already there, None
    when it is left out - written in, or demanding, a language the user does not
    list. Its own page is the description when it has one; otherwise the careers
    page, which is where the title was read."""
    page = await register.fetch_page(client, opening["url"]) if opening["url"] else None
    description = strip_html(page[0] if page else careers_html)[:8000]
    if language.blocker(description, set(mine)):
        return None
    listing = JobListing(
        source="company_site",
        external_id=opening["url"] or f"{company['uid']}:{opening['title']}",
        title=opening["title"],
        company=company["name"],
        location=_where(company),
        description=description,
        url=opening["url"] or careers_url,
    )
    return 1 if await queries.insert_job(listing, fetched_via="company_site") else 0


async def approach(uid: str) -> int | None:
    """The company as a "Speculative application" in the Jobs list, so the cover
    letter, the people finder and the pipeline all work for it. Returns the job
    id; the same one on a second click."""
    company = await queries.get_company(uid)
    if company is None:
        return None
    if company.get("job_id") and await queries.get_job(company["job_id"]):
        return company["job_id"]
    swiss = (company.get("country") or "CH") == "CH"
    form = f" ({company['legal_form']})" if company.get("legal_form") else ""
    lines = [f"{company['name']}{form}, {_where(company)}.",
             "No open position is posted: this is an unsolicited application.",
             ("Registered purpose: " if swiss else "What they do, from their website: ")
             + (company.get("purpose") or "")]
    if company.get("website"):
        lines.append(f"Website: {company['website']}")
    if company.get("careers_url"):
        lines.append(f"Careers page: {company['careers_url']}")
    listing = JobListing(
        source="direct_approach", external_id=uid, title="Speculative application",
        company=company["name"], location=_where(company),
        description="\n\n".join(lines), url=company.get("website") or link(uid))
    job_id = await queries.insert_job(listing, fetched_via="company_register")
    if job_id is None:                       # stored before, e.g. under an older link
        return None
    await queries.save_quick_score(job_id, int(company.get("fit") or 0),
                                   f"Direct approach: {company.get('fit_reason') or ''}".strip(), [])
    await queries.set_company_status(uid, "approached", job_id)
    return job_id

"""Company career boards, read straight from the ATS that hosts them.

These are the jobs that never reach LinkedIn: a company posts to its own
Greenhouse/Lever/Ashby/Personio/Teamtailor/Recruitee/SmartRecruiters page, and
only some of those postings get syndicated. Each ATS exposes one public,
key-free JSON (or XML) endpoint **per company**, so coverage is a list of
company slugs rather than a search query - that list is the registry in
`registry/eu_boards.json`, and every slug in it was verified with a live
request.

Why this matters here: the registry is seeded with European companies. The
remote-job APIs skew US/global and LinkedIn is one basket; this is the only
source that reaches a Berlin or Tallinn company's own careers page directly.

Three things the ATS payloads give us that a job board does not:

* the **full description in the list call** (except SmartRecruiters, which needs
  one extra fetch per posting - capped accordingly);
* a **structured workplace flag** (Lever `workplaceType`, Ashby `isRemote`,
  Recruitee `remote`/`hybrid`/`on_site`, SmartRecruiters `location.remote`),
  which is folded into the location string so `matcher.detect_workplace` reads
  it the same way it reads the extension's LinkedIn chip;
* a real posting date, so the scheduler's recency window can do its job.

Boards are fetched once per scan cycle and cached, because the scheduler calls
each fetcher once PER KEYWORD - without the cache a three-keyword search would
download all 117 boards three times.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from bs4 import BeautifulSoup

from config import settings
from scrapers.base import JobListing, strip_html
from scrapers.base import kw_matches as _kw_matches

logger = logging.getLogger(__name__)

REGISTRY_PATH = Path(__file__).parent / "registry" / "eu_boards.json"

# How long a downloaded board stays fresh. One scan cycle runs well inside this;
# a manual re-scan minutes later still re-uses it rather than hammering the ATS.
_CACHE_TTL_S = 15 * 60

# SmartRecruiters lists postings without descriptions, so each one costs an extra
# request. Cap per board, newest first, or a 1074-posting board eats the cycle.
_SR_DETAIL_CAP = 40

# Parallel boards in flight. Small on purpose: these are other people's public
# endpoints and the whole registry still finishes in well under a minute.
_CONCURRENCY = 8

_HEADERS = {
    "User-Agent": settings.user_agent,
    "Accept": "application/json, text/xml;q=0.9, */*;q=0.8",
}

_cache: dict[tuple[str, str], tuple[float, list[JobListing]]] = {}


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _text(markup: str | None) -> str:
    """HTML (or double-escaped HTML - Greenhouse does that) to readable text."""
    if not markup:
        return ""
    if "&lt;" in markup and "<" not in markup:
        markup = html.unescape(markup)
    return strip_html(markup)


def _with_workplace(location: str | None, workplace: str | None) -> str | None:
    """Fold a structured workplace flag into the location string.

    `matcher.detect_workplace` already reads "Berlin (Hybrid)" - this puts the
    ATS's own flag where that gate will find it, instead of leaving the model to
    guess from prose."""
    loc = (location or "").strip()
    wp = (workplace or "").strip()
    if not wp:
        return loc or None
    if wp.lower() in loc.lower():
        return loc
    return f"{loc} ({wp})" if loc else wp


def _epoch_ms_to_iso(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


# Geography markers. The registry holds European *companies*, but they hire
# worldwide: one Berlin company's board also lists Kuwait, Bangalore and São
# Paulo, and every one of those costs a title-screen call before being rejected.
#
# This gate is deliberately coarse - continent, not city. Deciding whether a
# Barcelona role suits someone in Tallinn is the scoring blocker's job, and it
# already does it with the user's own stated location. Trying to do it here
# needs a city→country map, and the first version of this function got it
# wrong in both directions: it kept "Anywhere in Belgium" for a Spain search
# (the word "anywhere") and dropped "Barcelona (Remote)" (no literal "spain").
_EUROPE = (
    "europe", "eu ", "emea", "united kingdom", "england", "scotland", "wales",
    "ireland", "germany", "deutschland", "france", "spain", "españa", "portugal",
    "italy", "italia", "netherlands", "nederland", "holland", "belgium", "belgië",
    "luxembourg", "austria", "österreich", "switzerland", "schweiz", "suisse",
    "poland", "polska", "czech", "slovak", "hungary", "romania", "bulgaria",
    "greece", "croatia", "slovenia", "serbia", "denmark", "danmark", "sweden",
    "sverige", "norway", "norge", "finland", "suomi", "iceland", "estonia",
    "eesti", "latvia", "lithuania", "ukraine", "moldova", "cyprus", "malta",
    # Cities that appear without their country on ATS boards.
    "london", "berlin", "munich", "münchen", "hamburg", "cologne", "frankfurt",
    "paris", "lyon", "toulouse", "madrid", "barcelona", "valencia", "lisbon",
    "lisboa", "porto", "amsterdam", "rotterdam", "utrecht", "brussels", "antwerp",
    "vienna", "wien", "zurich", "zürich", "geneva", "milan", "milano", "rome",
    "roma", "warsaw", "warszawa", "krakow", "kraków", "wrocław", "prague",
    "praha", "budapest", "bucharest", "sofia", "athens", "copenhagen",
    "københavn", "stockholm", "gothenburg", "oslo", "helsinki", "tallinn",
    "riga", "vilnius", "dublin", "edinburgh", "manchester", "kyiv", "kiev",
)

_NOT_EUROPE = (
    "united states", "usa", "u.s.", "canada", "mexico", "brazil", "brasil",
    "argentina", "chile", "colombia", "peru", "india", "china", "japan",
    "singapore", "korea", "indonesia", "malaysia", "thailand", "vietnam",
    "philippines", "australia", "new zealand", "israel", "uae", "dubai",
    "abu dhabi", "kuwait", "qatar", "saudi", "bahrain", "egypt", "nigeria",
    "kenya", "south africa", "morocco", "pakistan", "bangladesh",
    "new york", "san francisco", "seattle", "austin", "boston", "chicago",
    "los angeles", "denver", "atlanta", "toronto", "vancouver", "bangalore",
    "bengaluru", "mumbai", "hyderabad", "gurgaon", "pune", "delhi", "chennai",
    "são paulo", "sao paulo", "buenos aires", "mexico city", "tokyo", "seoul",
    "sydney", "melbourne", "shanghai", "beijing", "tel aviv", "riyadh",
    "california", "texas", "florida", "virginia", "washington", "arizona",
)


def _is_european(location: str | None) -> bool:
    """Keep the posting unless its location clearly names somewhere else.

    Unknown locations pass: dropping a job we merely failed to parse is worse
    than scoring one we did not need. A location naming both ("Berlin; New
    York") passes on the European half."""
    loc = (location or "").strip().lower()
    if not loc:
        return True
    if any(marker in loc for marker in _EUROPE):
        return True
    return not any(marker in loc for marker in _NOT_EUROPE)


# --------------------------------------------------------------------------- #
# One parser per ATS. Each returns JobListing rows for a single company board.  #
# --------------------------------------------------------------------------- #

async def _fetch_greenhouse(client: httpx.AsyncClient, slug: str) -> list[JobListing]:
    r = await client.get(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        params={"content": "true"},
    )
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        out.append(JobListing(
            source="greenhouse",
            external_id=str(j.get("id")),
            title=j.get("title") or "",
            company=j.get("company_name") or slug,
            location=(j.get("location") or {}).get("name"),
            description=_text(j.get("content")),
            url=j.get("absolute_url"),
            posted_at=j.get("first_published") or j.get("updated_at"),
        ))
    return out


async def _fetch_lever(client: httpx.AsyncClient, slug: str) -> list[JobListing]:
    r = await client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if r.status_code != 200:
        return []
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        out.append(JobListing(
            source="lever",
            external_id=str(j.get("id")),
            title=j.get("text") or "",
            company=slug,
            location=_with_workplace(cats.get("location"), j.get("workplaceType")),
            description=j.get("descriptionPlain") or _text(j.get("description")),
            url=j.get("hostedUrl"),
            posted_at=_epoch_ms_to_iso(j.get("createdAt")),
        ))
    return out


async def _fetch_ashby(client: httpx.AsyncClient, slug: str) -> list[JobListing]:
    r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        workplace = j.get("workplaceType") or ("Remote" if j.get("isRemote") else "")
        out.append(JobListing(
            source="ashby",
            external_id=str(j.get("id")),
            title=j.get("title") or "",
            company=slug,
            location=_with_workplace(j.get("location"), workplace),
            description=j.get("descriptionPlain") or _text(j.get("descriptionHtml")),
            url=j.get("jobUrl") or j.get("applyUrl"),
            posted_at=j.get("publishedAt"),
        ))
    return out


async def _fetch_recruitee(client: httpx.AsyncClient, slug: str) -> list[JobListing]:
    r = await client.get(f"https://{slug}.recruitee.com/api/offers/")
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("offers", []):
        workplace = ("Remote" if j.get("remote") else
                     "Hybrid" if j.get("hybrid") else
                     "On-site" if j.get("on_site") else "")
        body = "\n\n".join(filter(None, [_text(j.get("description")),
                                         _text(j.get("requirements"))]))
        out.append(JobListing(
            source="recruitee",
            external_id=str(j.get("id")),
            title=j.get("title") or "",
            company=j.get("company_name") or slug,
            location=_with_workplace(j.get("location"), workplace),
            description=body,
            url=j.get("careers_url") or j.get("careers_apply_url"),
            posted_at=j.get("published_at") or j.get("created_at"),
        ))
    return out


async def _fetch_smartrecruiters(client: httpx.AsyncClient, slug: str) -> list[JobListing]:
    """SmartRecruiters lists postings without a description - one extra call each.

    Capped at `_SR_DETAIL_CAP` newest postings per board; the big boards here
    carry a thousand openings, most of them warehouse roles in other countries.
    """
    r = await client.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                         params={"limit": 100})
    if r.status_code != 200:
        return []
    postings = r.json().get("content", [])
    postings.sort(key=lambda p: str(p.get("releasedDate") or ""), reverse=True)
    out = []
    for p in postings[:_SR_DETAIL_CAP]:
        loc = p.get("location") or {}
        workplace = ("Remote" if loc.get("remote") else "Hybrid" if loc.get("hybrid") else "")
        where = ", ".join(filter(None, [loc.get("city"), (loc.get("country") or "").upper()]))
        description = ""
        try:
            d = await client.get(
                f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{p.get('id')}")
            if d.status_code == 200:
                sections = ((d.json().get("jobAd") or {}).get("sections") or {})
                description = "\n\n".join(
                    _text((sections.get(k) or {}).get("text"))
                    for k in ("companyDescription", "jobDescription", "qualifications")
                ).strip()
        except httpx.HTTPError:
            pass
        out.append(JobListing(
            source="smartrecruiters",
            external_id=str(p.get("id")),
            title=p.get("name") or "",
            company=(p.get("company") or {}).get("name") or slug,
            location=_with_workplace(where, workplace),
            description=description,
            url=f"https://jobs.smartrecruiters.com/{slug}/{p.get('id')}",
            posted_at=p.get("releasedDate"),
        ))
    return out


async def _fetch_personio(client: httpx.AsyncClient, slug: str) -> list[JobListing]:
    """Personio publishes an XML feed - the standard board for German SMEs."""
    r = await client.get(f"https://{slug}.jobs.personio.de/xml")
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "xml")
    out = []
    for pos in soup.find_all("position"):
        def field(name: str) -> str:
            el = pos.find(name)
            return el.get_text(strip=True) if el else ""

        job_id = field("id")
        if not job_id:
            continue
        body = "\n\n".join(
            _text(d.get_text()) for d in pos.find_all("jobDescription")) or _text(pos.get_text())
        out.append(JobListing(
            source="personio",
            external_id=job_id,
            title=field("name"),
            company=slug,
            location=_with_workplace(field("office"), field("employmentType")),
            description=body,
            url=f"https://{slug}.jobs.personio.de/job/{job_id}",
            posted_at=field("createdAt") or None,
        ))
    return out


async def _fetch_teamtailor(client: httpx.AsyncClient, slug: str) -> list[JobListing]:
    """Teamtailor's public RSS. Titles + links only, so the description comes
    from the feed summary - thin, but enough for the title screen to decide."""
    r = await client.get(f"https://{slug}.teamtailor.com/jobs.rss")
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "xml")
    out = []
    for item in soup.find_all("item"):
        link = item.link.get_text(strip=True) if item.link else ""
        if not link:
            continue
        out.append(JobListing(
            source="teamtailor",
            external_id=link.rstrip("/").rsplit("/", 1)[-1],
            title=item.title.get_text(strip=True) if item.title else "",
            company=slug,
            location=None,
            description=_text(item.description.get_text()) if item.description else "",
            url=link,
            posted_at=item.pubDate.get_text(strip=True) if item.pubDate else None,
        ))
    return out


_PARSERS: dict[str, Callable[..., Any]] = {
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
    "ashby": _fetch_ashby,
    "recruitee": _fetch_recruitee,
    "smartrecruiters": _fetch_smartrecruiters,
    "personio": _fetch_personio,
    "teamtailor": _fetch_teamtailor,
}


# --------------------------------------------------------------------------- #
# Registry                                                                     #
# --------------------------------------------------------------------------- #

def load_registry() -> list[dict[str, str]]:
    """The verified board list. Never raises - a broken file means no source,
    not a failed scan."""
    import json

    try:
        data = json.loads(REGISTRY_PATH.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("[ats] registry unreadable: %s", exc)
        return []
    return [b for b in data.get("boards", []) if b.get("ats") in _PARSERS and b.get("slug")]


async def _board(client: httpx.AsyncClient, board: dict[str, str]) -> list[JobListing]:
    key = (board["ats"], board["slug"])
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    try:
        rows = await _PARSERS[board["ats"]](client, board["slug"])
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.info("[ats] %s/%s failed: %s", board["ats"], board["slug"], exc)
        rows = []
    _cache[key] = (now, rows)
    return rows


async def iter_ats_boards(cfg: dict):
    """Stream the whole registry, newest boards first to finish, filtered by the
    search's keywords. Yields JobListing, never raises - the scheduler treats it
    like any other source."""
    keywords = (cfg.get("keywords") or "").strip()
    max_jobs = int(cfg.get("max_jobs") or settings.max_jobs_per_cycle)
    boards = load_registry()
    if not boards:
        return

    queue: asyncio.Queue = asyncio.Queue()
    sem = asyncio.Semaphore(_CONCURRENCY)

    async with httpx.AsyncClient(headers=_HEADERS, timeout=20.0,
                                 follow_redirects=True) as client:
        async def worker(board: dict[str, str]) -> None:
            async with sem:
                await queue.put(await _board(client, board))

        tasks = [asyncio.create_task(worker(b)) for b in boards]
        # No single board may eat the whole quota. Without this the first big
        # board to answer filled all 50 slots and the other 116 never showed up.
        per_board = max(2, max_jobs // 8)
        emitted = matched = 0
        try:
            for _ in boards:
                taken = 0
                for job in await queue.get():
                    emitted += 1
                    if matched >= max_jobs or taken >= per_board:
                        continue
                    # Title only. Matching the description too is what a search
                    # box never means: "project manager" appears somewhere in
                    # almost every 7 000-character posting, so a data-scientist
                    # ad passed the filter and went on to cost tokens.
                    if not _kw_matches(keywords, job.title or ""):
                        continue
                    if not _is_european(job.location):
                        continue
                    matched += 1
                    taken += 1
                    yield job
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    logger.info("[ats] %d boards, %d postings seen, %d matched '%s' (Europe only)",
                len(boards), emitted, matched, keywords or "*")

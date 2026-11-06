"""Additional job sources via PUBLIC, key-free JSON APIs.

Unlike LinkedIn (guest HTML scraping), these are official public APIs - no login,
no account risk, and each returns the full description in one call, so no
per-job detail fetch is needed.

  * Remotive   - https://remotive.com/api/remote-jobs        (remote jobs)
  * RemoteOK   - https://remoteok.com/api                    (remote jobs)
  * Arbeitnow  - https://www.arbeitnow.com/api/job-board-api (EU / Germany jobs)
  * Jobicy     - https://jobicy.com/api/v2/remote-jobs       (remote, EU geo filter)
  * Himalayas  - https://himalayas.app/jobs/api              (remote jobs)
  * Workable   - https://jobs.workable.com/api/v1/jobs       (ATS: company boards)
  * WeWorkRemotely - https://weworkremotely.com/remote-jobs.rss (remote, RSS)

**Workable is the ATS aggregator**: every posting comes straight from a company's
own Workable-hosted careers page, i.e. jobs that never reach LinkedIn. It is the
only probed ATS with a GLOBAL search (Greenhouse/Lever/Ashby/SmartRecruiters
expose one board per company slug and would need a company list - see the
notebook §6). It supports a real location filter, so it also covers the regional
angle (Spain, Estonia, anywhere) that the remote-only boards miss.

Each function is an async generator yielding JobListing, mirroring the LinkedIn
fetcher so the scheduler treats all sources uniformly. Never raises - a failed
request just ends the stream.
"""

from __future__ import annotations

import logging

import httpx

from config import settings
from scrapers.base import JobListing, strip_html
from scrapers.base import kw_matches as _kw_matches

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": settings.user_agent, "Accept": "application/json"}


def _with_wp(location: str | None, workplace: str | None) -> str | None:
    """Fold the arrangement into the location, where detect_workplace reads it."""
    loc = (location or "").strip()
    wp = (workplace or "").strip()
    if not wp:
        return loc or None
    return loc if wp.lower() in loc.lower() else (f"{loc} ({wp})" if loc else wp)



def _employment(value) -> str | None:
    """Normalise whatever the source calls it. A list ('Full-time') is common."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    text = str(value or "").strip()
    return text.title() if text else None


# Boards that only ever list remote work. Saying so costs nothing and is more
# reliable than hoping the phrase appears in the prose.
_REMOTE_ONLY = "Remote"


async def iter_remotive(cfg: dict):
    keywords = (cfg.get("keywords") or "").strip()
    max_jobs = int(cfg.get("max_jobs") or settings.max_jobs_per_cycle)
    params: dict = {"limit": max_jobs}
    if keywords:
        params["search"] = keywords
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=25.0) as client:
            r = await client.get("https://remotive.com/api/remote-jobs", params=params)
            logger.info("[remotive] GET -> %s", r.status_code)
            jobs = r.json().get("jobs", []) if r.status_code == 200 else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[remotive] failed: %s", exc)
        return
    for j in jobs[:max_jobs]:
        # Remotive's own search is loose: "RPA" brought back an AI video
        # annotation job that never mentions it, "Head of Engineering" a
        # freelance writer. Held to the same word test as every other board.
        description = strip_html(j.get("description", ""))
        if not _kw_matches(keywords, j.get("title") or "", description,
                           " ".join(j.get("tags") or []), j.get("category") or ""):
            continue
        yield JobListing(
            source="remotive",
            external_id=str(j.get("id")),
            title=j.get("title") or "(no title)",
            company=j.get("company_name"),
            location=_with_wp(j.get("candidate_required_location") or "Remote", _REMOTE_ONLY),
            description=description,
            url=j.get("url"),
            posted_at=j.get("publication_date"),
            employment_type=_employment(j.get("job_type")),
        )


async def iter_remoteok(cfg: dict):
    keywords = (cfg.get("keywords") or "").strip()
    max_jobs = int(cfg.get("max_jobs") or settings.max_jobs_per_cycle)
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=25.0) as client:
            r = await client.get("https://remoteok.com/api")
            logger.info("[remoteok] GET -> %s", r.status_code)
            data = r.json() if r.status_code == 200 else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[remoteok] failed: %s", exc)
        return
    count = 0
    for j in data:
        if count >= max_jobs:
            break
        if not isinstance(j, dict) or not j.get("id") or not j.get("position"):
            continue  # first element is a legal/metadata notice
        tags = " ".join(j.get("tags", []) or [])
        if not _kw_matches(keywords, j.get("position", ""), tags, j.get("description", "")):
            continue
        yield JobListing(
            source="remoteok",
            external_id=str(j.get("id")),
            title=j.get("position") or "(no title)",
            company=j.get("company"),
            location=_with_wp(j.get("location") or "Remote", _REMOTE_ONLY),
            description=strip_html(j.get("description", "")),
            url=j.get("url"),
            posted_at=j.get("date"),
        )
        count += 1


async def iter_jobicy(cfg: dict):
    """Jobicy remote-jobs API. Supports a geo filter - we request EU regions."""
    keywords = (cfg.get("keywords") or "").strip()
    max_jobs = int(cfg.get("max_jobs") or settings.max_jobs_per_cycle)
    params: dict = {"count": min(max_jobs, 50), "geo": "europe"}
    if keywords:
        params["tag"] = keywords
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=25.0) as client:
            r = await client.get("https://jobicy.com/api/v2/remote-jobs", params=params)
            logger.info("[jobicy] GET -> %s", r.status_code)
            jobs = r.json().get("jobs", []) if r.status_code == 200 else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jobicy] failed: %s", exc)
        return
    count = 0
    for j in jobs:
        if count >= max_jobs:
            break
        if not _kw_matches(keywords, j.get("jobTitle", ""), j.get("jobDescription", ""),
                           " ".join(j.get("jobIndustry", []) or [])):
            continue
        yield JobListing(
            source="jobicy",
            external_id=str(j.get("id")),
            title=j.get("jobTitle") or "(no title)",
            company=j.get("companyName"),
            location=_with_wp(j.get("jobGeo") or "Remote", _REMOTE_ONLY),
            description=strip_html(j.get("jobDescription", "")),
            url=j.get("url"),
            posted_at=str(j.get("pubDate") or ""),
            employment_type=_employment(j.get('jobType')),
        )
        count += 1


async def iter_himalayas(cfg: dict):
    """Himalayas remote-jobs API (JSON). No key required."""
    keywords = (cfg.get("keywords") or "").strip()
    max_jobs = int(cfg.get("max_jobs") or settings.max_jobs_per_cycle)
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=25.0) as client:
            r = await client.get("https://himalayas.app/jobs/api",
                                 params={"limit": min(max_jobs, 50)})
            logger.info("[himalayas] GET -> %s", r.status_code)
            jobs = r.json().get("jobs", []) if r.status_code == 200 else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[himalayas] failed: %s", exc)
        return
    count = 0
    for j in jobs:
        if count >= max_jobs:
            break
        locs = j.get("locationRestrictions") or []
        if not _kw_matches(keywords, j.get("title", ""), j.get("description", ""),
                           " ".join(j.get("categories", []) or [])):
            continue
        yield JobListing(
            source="himalayas",
            external_id=str(j.get("guid") or j.get("applicationLink") or j.get("title")),
            title=j.get("title") or "(no title)",
            company=j.get("companyName"),
            location=_with_wp(", ".join(locs) if locs else "Remote", _REMOTE_ONLY),
            description=strip_html(j.get("description", "")),
            url=j.get("applicationLink") or j.get("url"),
            posted_at=str(j.get("pubDate") or ""),
            employment_type=_employment(j.get('employmentType')),
        )
        count += 1


async def iter_arbeitnow(cfg: dict):
    keywords = (cfg.get("keywords") or "").strip()
    max_jobs = int(cfg.get("max_jobs") or settings.max_jobs_per_cycle)
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=25.0) as client:
            r = await client.get("https://www.arbeitnow.com/api/job-board-api")
            logger.info("[arbeitnow] GET -> %s", r.status_code)
            data = r.json().get("data", []) if r.status_code == 200 else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[arbeitnow] failed: %s", exc)
        return
    count = 0
    for j in data:
        if count >= max_jobs:
            break
        if not _kw_matches(keywords, j.get("title", ""), j.get("description", ""),
                           " ".join(j.get("tags", []) or [])):
            continue
        yield JobListing(
            source="arbeitnow",
            external_id=str(j.get("slug") or j.get("url")),
            title=j.get("title") or "(no title)",
            company=j.get("company_name"),
            # Arbeitnow states it as a boolean; most of its board is on-site.
            location=_with_wp(j.get("location"), "Remote" if j.get("remote") else None),
            description=strip_html(j.get("description", "")),
            url=j.get("url"),
            posted_at=str(j.get("created_at") or ""),
            employment_type=_employment(j.get('job_types')),
        )
        count += 1


async def iter_workable(cfg: dict):
    """Workable's public job search - postings served directly from companies'
    own ATS career pages (never posted to LinkedIn in many cases).

    Honors the search's `location` field (country/city name, e.g. "Spain",
    "Estonia", "Berlin"); leave it empty for a worldwide sweep. Descriptions come
    inline, so no per-job detail fetch. Paginated via `nextPageToken`."""
    keywords = (cfg.get("keywords") or "").strip()
    location = (cfg.get("location") or "").strip()
    max_jobs = int(cfg.get("max_jobs") or settings.max_jobs_per_cycle)
    url = "https://jobs.workable.com/api/v1/jobs"
    params: dict = {}
    if keywords:
        params["query"] = keywords
    if location:
        params["location"] = location

    count = 0
    token = None
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=25.0) as client:
            # Page until we have enough or the source runs out (20 per page).
            while count < max_jobs:
                page_params = {**params, **({"nextPageToken": token} if token else {})}
                r = await client.get(url, params=page_params)
                logger.info("[workable] GET %s -> %s", page_params, r.status_code)
                if r.status_code != 200:
                    return
                data = r.json()
                jobs = data.get("jobs") or []
                if not jobs:
                    return
                for j in jobs:
                    if count >= max_jobs:
                        return
                    loc = j.get("location") or {}
                    place = ", ".join(
                        p for p in (loc.get("city"), loc.get("countryName")) if p
                    )
                    if str(j.get("workplace") or "").lower() == "remote":
                        place = f"Remote — {place}" if place else "Remote"
                    # description + requirements: the requirements block holds the
                    # years/seniority/language signals the quick screen gates on.
                    body = "\n\n".join(
                        strip_html(j.get(k) or "")
                        for k in ("description", "requirementsSection", "benefitsSection")
                    ).strip()
                    yield JobListing(
                        source="workable",
                        external_id=str(j.get("id")),
                        title=j.get("title") or "(no title)",
                        company=(j.get("company") or {}).get("title"),
                        location=place or None,
                        description=body,
                        url=j.get("url"),
                        posted_at=j.get("created") or j.get("updated"),
                    )
                    count += 1
                token = data.get("nextPageToken")
                if not token:
                    return
    except Exception as exc:  # noqa: BLE001
        logger.warning("[workable] failed: %s", exc)
        return


async def iter_weworkremotely(cfg: dict):
    """WeWorkRemotely's public RSS feed (100 newest remote jobs, no query
    parameter - keywords are filtered client-side via `_kw_matches`).

    Item titles are "Company: Job Title"; the description is HTML."""
    import xml.etree.ElementTree as ET

    keywords = (cfg.get("keywords") or "").strip()
    max_jobs = int(cfg.get("max_jobs") or settings.max_jobs_per_cycle)
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent}, timeout=25.0
        ) as client:
            r = await client.get("https://weworkremotely.com/remote-jobs.rss")
            logger.info("[weworkremotely] GET -> %s", r.status_code)
            if r.status_code != 200:
                return
            root = ET.fromstring(r.content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[weworkremotely] failed: %s", exc)
        return

    def field(item, tag: str) -> str:
        el = item.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    count = 0
    for item in root.findall(".//item"):
        if count >= max_jobs:
            break
        raw_title = field(item, "title")
        company, _, title = raw_title.partition(":")
        title = (title or raw_title).strip()
        company = company.strip() if title != raw_title else None
        # Match on the TITLE only. The feed has no query parameter, and a word
        # like "product" appears in nearly every description and skills list -
        # matching those made the keyword filter meaningless (a "Head of
        # Marketing" came back for "product").
        if not _kw_matches(keywords, title):
            continue
        body = strip_html(field(item, "description"))
        link = field(item, "link")
        yield JobListing(
            source="weworkremotely",
            external_id=field(item, "guid") or link,
            title=title or "(no title)",
            company=company,
            location=_with_wp(field(item, "region") or "Remote", _REMOTE_ONLY),
            description=body,
            url=link,
            posted_at=field(item, "pubDate"),
        )
        count += 1

"""Companies from OpenStreetMap, for the countries whose register cannot be
searched by what a company does - not for free, and not without an ID of the
country's own.

Checked 12 Nov 2026: Austria's official register API (the BMJ's free "HVD"
service) needs a token granted to an ID Austria holder and searches by NAME
only; the Dutch and Luxembourg registers are paid, and the free Dutch dataset
leaves out names and numbers; Belgium's KBO open data is complete but behind an
account the user would have to create. OpenStreetMap maps offices - IT firms,
consultancies, engineering offices - and many carry their website. Offices with
a website, counted the same day: Netherlands 7,206, Austria about 3,200,
Belgium 1,933, Luxembourg 175. Fewer than a register holds, only the ones
somebody mapped - but each comes with the site the ranking then reads, since
the map says nothing about what a company does. Open data (ODbL): the source
must be named.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

import httpx

from scrapers.base import strip_html

ENDPOINT = "https://overpass-api.de/api/interpreter"
SOURCE_NOTE = "© OpenStreetMap contributors (ODbL), via the Overpass API"
COUNTRIES = {"AT": "Austria", "BE": "Belgium", "NL": "Netherlands", "LU": "Luxembourg"}
# The kinds of office that employ the people this app is for. Shops, doctors,
# lawyers, estate agents and government offices are left out.
OFFICE_TYPES = {
    "it": "IT company", "company": "Company office", "consulting": "Consultancy",
    "engineer": "Engineering office", "research": "Research", "financial": "Financial services",
    "telecommunication": "Telecommunications", "advertising_agency": "Advertising agency",
}
_UID = re.compile(r"osm-([nwr])(\d+)")
_KIND = {"n": "node", "w": "way", "r": "relation"}


def office_query(country: str) -> str:
    """Named offices of the useful kinds that list a website, in one country."""
    types = "|".join(OFFICE_TYPES)
    return f"""[out:json][timeout:180];
area["ISO3166-1"="{country}"][admin_level=2]->.a;
(
  nwr["office"~"^({types})$"]["name"]["website"](area.a);
  nwr["office"~"^({types})$"]["name"]["contact:website"](area.a);
);
out tags;"""


def normalise_url(raw: str | None) -> str | None:
    """A mapped website as a fetchable URL: the first of several, a scheme
    added when the mapper left it out. None when it is not a web address."""
    url = (raw or "").split(";")[0].strip()
    if not url:
        return None
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    host = urlparse(url).hostname or ""
    return url if "." in host and " " not in url else None


def offices(elements: list[dict], country: str) -> list[dict]:
    """Rows from the map, in one fixed order - so an offset walks the same list
    run after run - and one per website: a chain's offices all point to one
    site, and one company is one target, not one per branch."""
    out: list[dict] = []
    seen: set[str] = set()
    for el in sorted(elements, key=lambda e: (str(e.get("type")), int(e.get("id") or 0))):
        tags = el.get("tags") or {}
        name = " ".join((tags.get("name") or "").split())
        site = normalise_url(tags.get("website") or tags.get("contact:website"))
        if not name or not site or el.get("type") not in ("node", "way", "relation"):
            continue
        host = (urlparse(site).hostname or "").lower().removeprefix("www.")
        if host in seen:
            continue
        seen.add(host)
        out.append({
            "uid": f"osm-{el['type'][0]}{el['id']}",
            "name": name,
            "country": country,
            "town": tags.get("addr:city") or None,
            "postcode": tags.get("addr:postcode") or None,
            "office": tags.get("office"),
            "website": site,
            "purpose": tags.get("description") or None,
        })
    return out


async def find_offices(country: str, limit: int, offset: int,
                       client: httpx.AsyncClient | None = None) -> list[dict]:
    """The next `limit` offices of one country from `offset`. The map has no
    paging, so the whole country is fetched (a few MB) and sliced here."""
    if country not in COUNTRIES:
        return []
    own = client is None
    client = client or httpx.AsyncClient(timeout=240.0, headers={
        "User-Agent": "JobFinder (company search; personal use)"})
    try:
        elements = (await _ask(client, office_query(country))).get("elements") or []
    finally:
        if own:
            await client.aclose()
    return offices(elements, country)[offset:offset + limit]


# Seconds to wait before each try. The service is shared and often busy: on
# 12 Nov 2026 three countries asked straight after a fourth all got a 502
# within a second, and the same query answered a minute later. Mirrors were
# no better that day (one 504 after two minutes, one timed out).
_ATTEMPTS = (0.0, 30.0, 90.0)
_BUSY = {429, 502, 503, 504}


async def _ask(client, query: str) -> dict:
    """The service's answer, asking again after a pause while it is busy.
    Raises the last error when every try failed."""
    error: Exception | None = None
    for wait in _ATTEMPTS:
        await asyncio.sleep(wait)
        try:
            r = await client.post(ENDPOINT, data={"data": query})
        except httpx.TransportError as exc:     # timeouts included
            error = exc
            continue
        if r.status_code in _BUSY:
            error = httpx.HTTPStatusError(f"the map service is busy ({r.status_code})",
                                          request=r.request, response=r)
            continue
        r.raise_for_status()
        return r.json()
    raise error or RuntimeError("the map service did not answer")


def element_url(uid: str) -> str | None:
    """The office's page on openstreetmap.org, or None for another kind of id."""
    m = _UID.fullmatch(uid or "")
    return f"https://www.openstreetmap.org/{_KIND[m.group(1)]}/{m.group(2)}" if m else None


_META = re.compile(
    r"<meta\b(?=[^>]*\bname=[\"'](?:description|og:description)[\"'])[^>]*\bcontent=[\"']([^\"']*)",
    re.I)
_META_PROP = re.compile(
    r"<meta\b(?=[^>]*\bproperty=[\"']og:description[\"'])[^>]*\bcontent=[\"']([^\"']*)", re.I)


def describe(html: str | None, office: str | None = None, mapped: str | None = None) -> str:
    """What a company says it does, for the ranking: the kind of office, the
    map's own description, the page title and summary, then the start of the
    page's text. The homepage stands in for the register's purpose text."""
    html = html or ""
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    meta = _META.search(html) or _META_PROP.search(html)
    parts = [OFFICE_TYPES.get(office or "", "Office"), mapped,
             strip_html(title.group(1)) if title else None,
             strip_html(meta.group(1)) if meta else None,
             strip_html(html)[:400]]
    text = " | ".join(" ".join(p.split()) for p in parts if p and p.strip())
    return text[:800]

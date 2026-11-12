"""Swiss companies from the commercial register, and their own websites.

The hidden job market: companies that hire but never post on a job board. They
are found here by what they DO - the registered purpose (Zweck) every Swiss
company files - rather than by an advert, and then looked up on their own site.

The register is Zefix, the Federal Office of Justice's central business name
index, read through LINDAS: the federal linked-data service, public SPARQL, no
login, the whole register refreshed daily (open data, source must be named).
The REST API next door wants credentials and searches by name only. What the
register does NOT hold is a website, so that is guessed from the name and then
PROVEN: a site counts only when it shows the company's UID or exact legal name,
which Swiss imprints carry and Swiss law keeps unique. Measured 11 Nov 2026 on
40 AI/fintech/software companies: 19 sites found and proven, 8 with a careers
link, 1 using a known job system - the other careers pages exist nowhere else.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from urllib.parse import urljoin, urlparse

import httpx

from scrapers.base import strip_html

logger = logging.getLogger(__name__)

ENDPOINT = "https://ld.admin.ch/query"
GRAPH = "https://lindas.admin.ch/foj/zefix"
SOURCE_NOTE = "Zefix, Federal Office of Justice, via LINDAS (open data)"
# eCH-0097 legal-form codes, the same in every language: AG/SA and GmbH/Sàrl.
# Sole traders and associations do not employ anyone worth a search.
LEGAL_FORMS = {"0106": "AG", "0107": "GmbH"}
_FORM_URI = "https://ld.admin.ch/ech/97/legalforms/{}"

# A search term is letters (any language), digits, spaces and hyphens - nothing
# else. It goes into a SPARQL string used as a regex, and a quote, a backslash
# or a bracket from the model or the form must never reach the query.
_TERM = re.compile(r"^\w[\w \-]{1,38}\w$")


def clean_terms(raw) -> list[str]:
    """The purpose terms safe to search with, lowercased and deduplicated."""
    items = raw.splitlines() if isinstance(raw, str) else list(raw or [])
    out: list[str] = []
    for item in items:
        term = " ".join(str(item).split()).lower()
        if _TERM.match(term) and "_" not in term and term not in out:
            out.append(term)
    return out[:15]


def uid_display(uid: str) -> str:
    """CHE123456789 -> CHE-123.456.789, as the register and imprints write it."""
    d = re.sub(r"\D", "", uid or "")
    return f"CHE-{d[:3]}.{d[3:6]}.{d[6:]}" if len(d) == 9 else uid


def register_url(uid: str) -> str:
    """The company's public entry in the federal UID register."""
    return f"https://www.uid.admin.ch/Detail.aspx?uid_id={uid_display(uid)}"


def company_query(terms: list[str], limit: int, offset: int) -> str:
    """Active AG/GmbH companies whose registered purpose contains a term.

    Ordered by identifier so that `offset` walks the same list run after run.
    Companies being wound up stay in the register as "... in Liquidation" and
    are left out: they hire no one."""
    forms = ", ".join(f"<{_FORM_URI.format(c)}>" for c in LEGAL_FORMS)
    pattern = "|".join(clean_terms(terms))
    return f"""
PREFIX schema: <http://schema.org/>
PREFIX admin: <https://schema.ld.admin.ch/>
SELECT ?id ?name ?form ?purpose ?town ?canton WHERE {{
  GRAPH <{GRAPH}> {{
    ?c a admin:ZefixOrganisation ;
       schema:legalName ?name ;
       schema:description ?purpose ;
       schema:identifier ?id ;
       schema:additionalType ?form ;
       schema:address ?a .
    ?a schema:addressLocality ?town .
    OPTIONAL {{ ?a schema:addressRegion ?canton }}
    FILTER(?form IN ({forms}))
    FILTER(CONTAINS(STR(?id), "/UID/"))
    FILTER(!REGEX(?name, "liquidation|liquidazione", "i"))
    FILTER(REGEX(?purpose, "{pattern}", "i"))
  }}
}} ORDER BY ?id LIMIT {int(limit)} OFFSET {int(offset)}
"""


async def find_companies(terms: list[str], limit: int = 200, offset: int = 0,
                         client: httpx.AsyncClient | None = None) -> list[dict]:
    """Companies from the register whose purpose matches. [] when no term is usable."""
    if not clean_terms(terms):
        return []
    own = client is None
    client = client or httpx.AsyncClient(timeout=180.0, headers={
        "Accept": "application/sparql-results+json", "User-Agent": "JobFinder"})
    try:
        r = await client.post(ENDPOINT, data={"query": company_query(terms, limit, offset)})
        r.raise_for_status()
        rows = r.json()["results"]["bindings"]
    finally:
        if own:
            await client.aclose()
    out, seen = [], set()
    for row in rows:
        uid = row["id"]["value"].rsplit("/", 1)[-1]
        if uid in seen:            # a company with two addresses comes back twice
            continue
        seen.add(uid)
        code = row["form"]["value"].rsplit("/", 1)[-1]
        out.append({
            "uid": uid,
            "name": row["name"]["value"],
            "legal_form": LEGAL_FORMS.get(code, code),
            "purpose": row["purpose"]["value"],
            "town": row["town"]["value"],
            "canton": (row.get("canton") or {}).get("value", "").rsplit("/", 1)[-1] or None,
        })
    return out


# --------------------------------------------------------------------------- #
# The company's own website, proven to be theirs                               #
# --------------------------------------------------------------------------- #

_LEGAL_WORDS = re.compile(
    r"\b(ag|sa|gmbh|sàrl|sarl|ltd|llc|inc|holding|switzerland|schweiz|suisse|svizzera|"
    r"international|group|gruppe)\b")
# Where an imprint usually lives when the homepage does not carry the UID.
_IMPRINT_PATHS = ("", "/impressum", "/imprint", "/en/imprint", "/de/impressum",
                  "/legal", "/mentions-legales")
_CAREERS = re.compile(
    r"karriere|careers?|\bjobs?\b|stellen|offene[- ]stellen|emplois?|carri[eè]re|"
    r"lavora con noi|join[- ]?us|work with us", re.I)


def domain_candidates(name: str) -> list[str]:
    """Domains a company is likely to own, from its legal name: core.ch first."""
    base = unicodedata.normalize("NFKD", name.lower()).encode("ascii", "ignore").decode()
    base = re.sub(r"\(.*?\)", " ", base.replace("&", " "))
    words = re.findall(r"[a-z0-9]+", _LEGAL_WORDS.sub(" ", base)) or re.findall(r"[a-z0-9]+", base)[:1]
    if not words:
        return []
    cores = list(dict.fromkeys(["".join(words), "-".join(words), words[0]]))
    return [f"{core}.{tld}" for core in cores if len(core) >= 3 for tld in ("ch", "com")][:5]


def uid_forms(uid: str) -> list[str]:
    """The ways an imprint writes a UID."""
    d = re.sub(r"\D", "", uid or "")
    if len(d) != 9:
        return []
    return [f"CHE-{d[:3]}.{d[3:6]}.{d[6:]}", f"CHE{d}", f"CHE-{d}", f"CHE {d[:3]}.{d[3:6]}.{d[6:]}"]


def proof_of(text: str, name: str, uid: str, swiss: bool = True) -> str | None:
    """'UID' or 'name' when the page shows it belongs to this company, else None.

    The UID is proof anywhere. The legal name is unique only inside
    Switzerland - a German "Markant AG" can sit on markant.com - so it counts
    only when `swiss` says the site is Swiss (a .ch domain, or the page names
    the company's registered town)."""
    if any(form in text for form in uid_forms(uid)):
        return "UID"
    if swiss and " ".join(name.lower().split()) in " ".join(text.lower().split()):
        return "name"
    return None


async def _html(client, url: str):
    try:
        r = await client.get(url)
    except Exception:  # noqa: BLE001 - an unreachable guess is just a wrong guess
        return None
    if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
        return None
    return r


async def find_website(client, name: str, uid: str, town: str | None = None) -> dict | None:
    """The company's site, only when a page on it proves it is theirs.

    {"site", "proof", "home"} or None. A guessed domain that merely answers is
    not enough: "markant.ch" may be any Markant. None is the honest answer
    for about half the companies, and a wrong site is worse than none."""
    for domain in domain_candidates(name):
        home = await _html(client, f"https://{domain}")
        if home is None:
            continue
        site = str(home.url).rstrip("/")
        for path in _IMPRINT_PATHS:
            page = home if path == "" else await _html(client, site + path)
            if page is None:
                continue
            swiss = (urlparse(site).hostname or "").endswith(".ch") or bool(
                town and town.lower() in page.text.lower())
            proof = proof_of(page.text, name, uid, swiss)
            if proof:
                return {"site": site, "proof": proof, "home": home.text}
    return None


def careers_link(html: str, site: str) -> str | None:
    """A link on the homepage that looks like the careers page, on the same site
    or a job system's, or None."""
    for href, label in re.findall(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', html or "", re.S | re.I):
        if _CAREERS.search(href) or _CAREERS.search(strip_html(label)):
            url = urljoin(site + "/", href)
            if urlparse(url).scheme in ("http", "https"):
                return url
    return None


def page_links(html: str, base: str, limit: int = 80) -> list[tuple[str, str]]:
    """(link text, absolute URL) pairs, so a model reading the page can cite a
    posting's own link instead of inventing one."""
    out: list[tuple[str, str]] = []
    for href, label in re.findall(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', html or "", re.S | re.I):
        text = " ".join(strip_html(label).split())
        url = urljoin(base, href)
        if text and urlparse(url).scheme in ("http", "https") and (text, url) not in out:
            out.append((text[:80], url))
        if len(out) >= limit:
            break
    return out


async def fetch_page(client, url: str) -> tuple[str, str] | None:
    """(html, final url) of a page, or None."""
    page = await _html(client, url)
    return (page.text, str(page.url)) if page is not None else None

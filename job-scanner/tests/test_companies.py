"""The company search: Swiss companies from the commercial register, their own
websites, and what the user decides about them. No network.

What is guarded is what can hide or destroy something. A re-run must not undo
a decision the user made about a company; a website must not be credited to the
wrong company; a failed ranking must not bury a company at zero; and a search
term must reach the register query as words and nothing else.
"""

from __future__ import annotations

import httpx

from matcher import companies as ai
from scheduler import companies as cs
from scrapers import company_language as cl
from scrapers import osm_offices as osm
from scrapers import swiss_register as reg

_ACME = {"uid": "CHE111222333", "name": "Acme Robotics AG", "legal_form": "AG",
         "town": "Zug", "canton": "ZG", "purpose": "Entwicklung von Software."}


# --------------------------------------------------------------------------- #
# The register query                                                           #
# --------------------------------------------------------------------------- #

def test_a_search_term_reaches_the_register_as_words_only():
    """Terms come from a model and a form, and go into a SPARQL string used as a
    regex. A quote or a bracket must never reach the query."""
    got = reg.clean_terms(['Künstliche Intelligenz', 'ki" } } INSERT DATA {', 'soft\\ware',
                           'fintech', 'fintech', 'ab', 'x' * 60, 'prozess-automatisierung'])

    assert got == ['künstliche intelligenz', 'fintech', 'prozess-automatisierung']
    query = reg.company_query(['fintech', 'ki" } }'], 10, 0)
    assert 'FILTER(REGEX(?purpose, "fintech", "i"))' in query


def test_the_register_query_leaves_out_companies_being_wound_up():
    """They stay in the register as "... in Liquidation" and hire no one."""
    query = reg.company_query(["software"], 10, 0)

    assert 'FILTER(!REGEX(?name, "liquidation|liquidazione", "i"))' in query
    assert "legalforms/0106" in query and "legalforms/0107" in query     # AG/SA, GmbH/Sàrl


# --------------------------------------------------------------------------- #
# Websites                                                                     #
# --------------------------------------------------------------------------- #

class _Page:
    status_code = 200
    headers = {"content-type": "text/html; charset=utf-8"}

    def __init__(self, url: str, text: str):
        self.url, self.text = url, text


class _Web:
    """Stands in for httpx: the pages that exist, and nothing else."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages

    async def get(self, url):
        if url not in self.pages:
            raise httpx.ConnectError("no such site")
        return _Page(url, self.pages[url])


async def test_a_website_counts_only_when_it_proves_it_is_theirs():
    """A guessed domain that merely answers could be any Acme. Measured: half of
    the companies have no findable site - and none is better than a wrong one."""
    stranger = _Web({"https://acmerobotics.ch": "<h1>Acme Robotics - toys and games</h1>"})
    assert await reg.find_website(stranger, "Acme Robotics AG", "CHE111222333") is None

    theirs = _Web({"https://acmerobotics.ch": "<h1>Acme Robotics</h1>",
                   "https://acmerobotics.ch/impressum": "Acme Robotics AG · CHE-111.222.333"})
    found = await reg.find_website(theirs, "Acme Robotics AG", "CHE111222333")
    assert (found["site"], found["proof"]) == ("https://acmerobotics.ch", "UID")


async def test_a_namesake_abroad_is_not_taken_for_the_swiss_company():
    """Legal names are unique only inside Switzerland: a German "Acme Robotics
    AG" can sit on acmerobotics.com. Its name alone proves nothing there."""
    german = _Web({"https://acmerobotics.com": "Acme Robotics AG, Hauptstraße 1, Stuttgart"})
    assert await reg.find_website(german, "Acme Robotics AG", "CHE111222333", "Zug") is None

    swiss = _Web({"https://acmerobotics.com": "Acme Robotics AG, Baarerstrasse 2, Zug"})
    found = await reg.find_website(swiss, "Acme Robotics AG", "CHE111222333", "Zug")
    assert found["proof"] == "name"


# --------------------------------------------------------------------------- #
# What the user decided                                                        #
# --------------------------------------------------------------------------- #

async def test_a_rerun_never_undoes_what_the_user_decided(db):
    await db.add_companies([_ACME])
    await db.save_company_fit(_ACME["uid"], 85, "Builds software")
    await db.set_company_status(_ACME["uid"], "dismissed")

    assert await db.add_companies([{**_ACME, "purpose": "Something else"}]) == 0

    company = await db.get_company(_ACME["uid"])
    assert (company["status"], company["fit"], company["purpose"]) == (
        "dismissed", 85, "Entwicklung von Software.")
    assert await db.list_companies() == [], "a dismissed company came back"


async def _ready(db, monkeypatch, rows):
    """A CV, a scanning key, a register that answers with `rows`, no waiting."""
    await db.save_profile(resume_text="CTO | Northwind Traders | 2024 – Present", instructions="",
                          case_bank="")
    await db.save_ai_provider("openai", "sk-test")

    async def register(terms, limit, offset):
        return rows

    monkeypatch.setattr(reg, "find_companies", register)
    monkeypatch.setattr(cs, "_PACE", 0)


async def test_a_failed_ranking_leaves_companies_unranked_not_zero(db, monkeypatch):
    """A zero nobody decided on buries the company; unranked, the next run asks again."""
    await _ready(db, monkeypatch, [_ACME])

    async def failing(*a, **kw):
        raise RuntimeError("429 rate limit")

    monkeypatch.setattr(ai, "rank_companies", failing)
    await cs.run("software")

    assert (await db.get_company(_ACME["uid"]))["fit"] is None
    assert [c["uid"] for c in await db.unranked_companies()] == [_ACME["uid"]]


async def test_openings_on_a_careers_page_become_scored_jobs(db, monkeypatch):
    """The hidden market in one company: a careers page no job board lists."""
    await _ready(db, monkeypatch, [_ACME])

    async def rank(batch, cv_text, **kw):
        return {c["uid"]: (90, "Builds what the CV shows") for c in batch}

    async def website(client, name, uid, town=None):
        return {"site": "https://acmerobotics.ch", "proof": "UID",
                "home": '<a href="/karriere">Karriere</a>'}

    async def page(client, url):
        return ("<h1>Offene Stellen</h1>", url)

    async def read(text, links, company, **kw):
        return {"openings": [{"title": "Platform Engineer", "url": ""},
                             {"title": "Head of Automation", "url": ""}],
                "open_applications": True}

    scored = []

    async def process_new_jobs(force=False):
        scored.append(force)

    from scheduler import jobs as sched
    monkeypatch.setattr(ai, "rank_companies", rank)
    monkeypatch.setattr(reg, "find_website", website)
    monkeypatch.setattr(reg, "fetch_page", page)
    monkeypatch.setattr(ai, "read_careers_page", read)
    monkeypatch.setattr(sched, "process_new_jobs", process_new_jobs)

    await cs.run("software")

    jobs = await db.get_new_jobs()
    assert sorted(j["title"] for j in jobs) == ["Head of Automation", "Platform Engineer"]
    assert {(j["source"], j["company"], j["location"]) for j in jobs} == {
        ("company_site", "Acme Robotics AG", "Zug, Switzerland")}
    assert scored == [True], "the openings were stored but never scored"
    company = await db.get_company(_ACME["uid"])
    assert (company["careers_url"], company["openings"], company["open_applications"]) == (
        "https://acmerobotics.ch/karriere", 2, 1)


async def test_approaching_a_company_makes_one_speculative_application(db):
    """So the letter, the people finder and the pipeline all work for a company
    that posts nothing - and a second click does not make a second one."""
    from fastapi.testclient import TestClient

    import main

    await db.add_companies([_ACME])
    await db.save_company_fit(_ACME["uid"], 82, "Builds what the CV shows")
    with TestClient(main.app) as client:
        first = client.post(f"/companies/{_ACME['uid']}/approach", follow_redirects=False)
        second = client.post(f"/companies/{_ACME['uid']}/approach", follow_redirects=False)

    assert first.headers["location"] == second.headers["location"]
    job = await db.get_job(int(first.headers["location"].rsplit("/", 1)[-1]))
    assert (job["title"], job["company"], job["score"], job["source"]) == (
        "Speculative application", "Acme Robotics AG", 82, "direct_approach")
    assert (await db.get_company(_ACME["uid"]))["status"] == "approached"


# --------------------------------------------------------------------------- #
# Languages: only companies that work in one of the user's                     #
# --------------------------------------------------------------------------- #
#
# What is guarded is what the filter hides. A company must not be hidden for a
# language nobody knows, nor shown on the strength of an English homepage when
# its careers page hires in German - and the user's languages must come from
# the CV's languages section, not from every language the CV happens to name.

_GENEVA = {**_ACME, "uid": "CHE444555666", "name": "Léman Logiciels SA", "town": "Genève",
           "canton": "GE",
           "purpose": "La société a pour but le développement et la vente de logiciels pour des entreprises."}
_ZURICH = {**_ACME, "uid": "CHE777888999", "name": "Limmat Software AG", "town": "Zürich",
           "canton": "ZH", "purpose": "Die Gesellschaft bezweckt die Entwicklung von Software und die Beratung."}

_EN = ("We are a small team in Zug and we are hiring. Join us if you want to work with people who care "
       "about their craft. You will build the products our customers use every day, and your ideas "
       "will shape what we do next. We offer flexible hours, a modern office, and a team that is "
       "happy to help you grow. Apply now with your CV and a few lines about you.")
_DE = ("Wir suchen eine engagierte Persönlichkeit für unser Team in Zürich. Du bist verantwortlich für "
       "die Entwicklung und den Betrieb unserer Plattform und arbeitest eng mit dem Produktteam "
       "zusammen. Wir bieten dir ein spannendes Umfeld, flexible Arbeitszeiten sowie eine "
       "moderne Infrastruktur. Sehr gute Deutschkenntnisse sind für diese Stelle von Vorteil, "
       "und wir freuen uns auf deine Bewerbung mit Lebenslauf.")


def test_a_company_is_listed_only_when_it_works_in_your_languages():
    me = {"en", "fr"}

    assert cl.works_in(_GENEVA, me), "a French-speaking company was hidden"
    assert not cl.works_in(_ZURICH, me), "a company seen only in German was listed"

    english_site = {**_ZURICH, "site_languages": "de,en"}
    assert cl.works_in(english_site, me), "a site offered in English did not count"
    assert not cl.works_in({**english_site, "careers_language": "de"}, me), (
        "an English homepage outweighed a careers page that hires in German")

    assert cl.works_in(_ZURICH, set()), "with no languages of the user's, a company was hidden"
    assert cl.works_in({**_ZURICH, "canton": None, "purpose": "Software"}, me), (
        "a company whose language nobody knows was hidden")


def test_the_register_language_is_read_from_the_purpose_before_the_canton():
    """Bern registers in German and French: its canton alone would say German."""
    assert cl.region_languages({**_GENEVA, "canton": "BE"}) == ["fr"]
    assert cl.region_languages({**_ZURICH, "canton": "GE"}) == ["de"]
    # Too short to read: the canton's language.
    assert cl.region_languages({**_ACME, "purpose": "Software", "canton": "TI"}) == ["it"]


def test_your_languages_come_from_the_cvs_languages_section_only():
    """Not "the German market" in a job line, not a beginner's "German (A2)",
    and not the interests section that follows."""
    from matcher import language

    cv = ("EXPERIENCE\n- Sold software across the German market.\n\n"
          "LANGUAGES\n- Portuguese (Native) | English (C1, Advanced) | French (B2) | German (A2)\n\n"
          "INTERESTS\nItalian cooking\n")

    assert language.cv_languages(cv) == {"pt", "en", "fr"}
    assert language.cv_languages("Languages: English (C1), German (A2), French: fluent") == {"en", "fr"}


async def test_a_careers_page_or_advert_in_a_language_you_lack_adds_nothing(db, monkeypatch):
    """A careers page written in German is not read at all - the model call
    would only find adverts the user cannot answer - and from an English page,
    a German advert is left out."""
    await _ready(db, monkeypatch, [_GENEVA, _ZURICH])
    await db.save_company_languages(["en", "fr"])
    homes = {_GENEVA["uid"]: "https://leman.ch", _ZURICH["uid"]: "https://limmat.ch"}
    pages = {"https://leman.ch/careers": _EN, "https://leman.ch/jobs/1": _EN,
             "https://leman.ch/jobs/2": _DE, "https://limmat.ch/careers": _DE}
    read_for = []

    async def rank(batch, cv_text, **kw):
        return {c["uid"]: (90, "Builds what the CV shows") for c in batch}

    async def website(client, name, uid, town=None):
        return {"site": homes[uid], "proof": "UID", "home": '<a href="/careers">Careers</a>'}

    async def page(client, url):
        return (pages[url], url) if url in pages else None

    async def read(text, links, company, **kw):
        read_for.append(company)
        return {"openings": [{"title": "Platform Engineer", "url": "https://leman.ch/jobs/1"},
                             {"title": "Plattform-Ingenieur", "url": "https://leman.ch/jobs/2"}],
                "open_applications": False}

    async def process_new_jobs(force=False):
        return None

    from scheduler import jobs as sched
    monkeypatch.setattr(ai, "rank_companies", rank)
    monkeypatch.setattr(reg, "find_website", website)
    monkeypatch.setattr(reg, "fetch_page", page)
    monkeypatch.setattr(ai, "read_careers_page", read)
    monkeypatch.setattr(sched, "process_new_jobs", process_new_jobs)

    await cs.run("software")

    assert read_for == [_GENEVA["name"]], "a careers page in German was sent to the model"
    assert [j["title"] for j in await db.get_new_jobs()] == ["Platform Engineer"]
    assert cs.status()["foreign"] == 1
    zurich = await db.get_company(_ZURICH["uid"])
    assert (zurich["careers_language"], zurich["openings"]) == ("de", None)
    assert (await db.get_company(_GENEVA["uid"]))["openings"] == 1


async def test_the_page_hides_companies_in_other_languages_and_one_click_shows_them(db):
    from fastapi.testclient import TestClient

    import main

    await db.add_companies([_GENEVA, _ZURICH])
    with TestClient(main.app) as client:
        before = client.get("/companies").text
        client.post("/companies/languages", data={"languages": ["en", "fr", "xx"]})
        mine = client.get("/companies").text
        everything = client.get("/companies?show=all").text

    assert _ZURICH["name"] in before, "with no CV languages, a company was hidden"
    assert (await db.get_app_settings())["company_languages"] == "en,fr"
    assert _GENEVA["name"] in mine and _ZURICH["name"] not in mine
    assert 'data-hidden="1"' in mine
    assert _ZURICH["name"] in everything, "a hidden company could not be shown"


# --------------------------------------------------------------------------- #
# Automatic runs                                                               #
# --------------------------------------------------------------------------- #
#
# What is guarded is what costs the user without their seeing it, or stops
# without their noticing: a run when they switched it off, a run every half
# hour instead of once a day, a run on top of a job scan (twice the calls a
# free-tier key allows), Settings quietly dropping the schedule, and a search
# that reached the end of the register list running empty from then on.

async def test_an_automatic_run_happens_only_when_on_due_and_nothing_else_runs(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from scheduler import jobs as sched

    await _ready(db, monkeypatch, [_ACME])

    async def rank(batch, cv_text, **kw):
        return {}

    monkeypatch.setattr(ai, "rank_companies", rank)
    await db.save_company_search("software", 0)

    assert await cs.run_if_due() is False, "ran while automatic runs were off"

    await db.save_company_auto("daily")
    monkeypatch.setattr(sched, "is_scanning", lambda: True)
    assert await cs.run_if_due() is False, "ran on top of a job scan"

    monkeypatch.setattr(sched, "is_scanning", lambda: False)
    assert await cs.run_if_due() is True
    assert await db.get_company(_ACME["uid"]) is not None
    assert await cs.run_if_due() is False, "ran again straight after a run"

    yesterday = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    await db.save_company_last_run(yesterday)
    assert await cs.run_if_due() is True, "a day later, the daily run did not come"


def test_saving_the_scan_schedule_keeps_the_automatic_company_run():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from scheduler.jobs import configure_scan_jobs

    scheduler = AsyncIOScheduler()
    cs.schedule(scheduler)
    configure_scan_jobs(scheduler, {"scan_times": "09:00", "scan_days": ""})
    configure_scan_jobs(scheduler, {"scan_times": "", "scan_days": ""})

    assert [j.id for j in scheduler.get_jobs()] == ["companies_auto"]


async def test_the_end_of_the_register_list_starts_again_from_the_top(db, monkeypatch):
    """Past the end, a run that found nothing would find nothing forever -
    while new companies keep being registered in between the known ones."""
    await _ready(db, monkeypatch, [])

    async def register(terms, limit, offset):
        return [] if offset else [_ACME]

    async def rank(batch, cv_text, **kw):
        return {}

    monkeypatch.setattr(reg, "find_companies", register)
    monkeypatch.setattr(ai, "rank_companies", rank)
    await db.save_company_search("software", 400)

    await cs.run("software")

    assert await db.get_company(_ACME["uid"]) is not None
    assert (await db.get_app_settings())["company_offset"] == 1


# --------------------------------------------------------------------------- #
# Austria and Benelux: offices from OpenStreetMap                              #
# --------------------------------------------------------------------------- #
#
# What is guarded: a map entry must reach the list as one company with a site
# that can be fetched, the ranking must read the company's own homepage (the
# map says nothing about what it does), a Belgian office must be judged by its
# own language area, an approach must work for a mapped company, and a homepage
# that is down on the day of the check must not wipe what was read before.

_NL_HOME = ("<html lang='nl'><head><title>Datawerk - AI voor de zorg</title>"
            "<meta name='description' content='Wij bouwen AI-producten voor ziekenhuizen.'>"
            "<script>window.dataLayer = window.dataLayer || [];</script></head>"
            "<body><a href='/en/'>English</a><p>Datawerk bouwt software.</p></body></html>")
_MAPPED = {"uid": "osm-n42", "name": "Datawerk B.V.", "country": "NL", "town": "Utrecht",
           "postcode": "3511 AB", "office": "it", "website": "https://datawerk.nl", "purpose": None}


def test_a_map_entry_becomes_one_company_with_a_site_that_can_be_fetched():
    elements = [
        {"type": "way", "id": 7, "tags": {"office": "it", "name": "Late Way", "website": "late.nl"}},
        {"type": "node", "id": 30, "tags": {"office": "it", "name": "Chain Utrecht",
                                            "website": "https://www.chain.nl/utrecht"}},
        {"type": "node", "id": 20, "tags": {"office": "it", "name": "Chain Amsterdam",
                                            "website": "https://chain.nl"}},
        {"type": "node", "id": 10, "tags": {"office": "it", "name": "  Datawerk  B.V.",
                                            "contact:website": "datawerk.nl;https://x.nl"}},
        {"type": "node", "id": 5, "tags": {"office": "it", "website": "https://noname.nl"}},
    ]

    rows = osm.offices(elements, "NL")

    assert [(r["uid"], r["name"], r["website"]) for r in rows] == [
        ("osm-n10", "Datawerk B.V.", "https://datawerk.nl"),
        ("osm-n20", "Chain Amsterdam", "https://chain.nl"),     # one company, not one per branch
        ("osm-w7", "Late Way", "https://late.nl"),
    ]
    assert osm.element_url("osm-w7") == "https://www.openstreetmap.org/way/7"


def test_a_belgian_office_is_judged_by_its_own_language_area():
    me = {"en", "fr"}
    assert cl.belgian_languages("1000") == ["fr", "nl"]           # Brussels is both
    assert cl.belgian_languages("4000") == ["fr"]                 # Liège
    assert cl.belgian_languages("4700") == ["de"]                 # Eupen
    assert cl.belgian_languages("2000") == ["nl"]                 # Antwerp

    antwerp = {"uid": "osm-n1", "country": "BE", "postcode": "2000"}
    assert not cl.works_in(antwerp, me), "a Dutch-speaking office was listed on its postcode alone"
    assert cl.works_in({**antwerp, "site_languages": "nl,en"}, me), "its English site did not count"
    assert cl.works_in({**antwerp, "postcode": "1050"}, me), "a Brussels office was hidden"
    assert cl.region_languages({"country": "AT"}) == ["de"]
    assert cl.region_languages({"country": "LU"}) == ["fr", "de"]


async def test_a_mapped_office_is_ranked_on_its_own_homepage(db, monkeypatch):
    await _ready(db, monkeypatch, [])
    await db.save_company_countries(["NL"])
    await db.save_company_offsets({"NL": 5})        # past the end: the walk starts again
    dead = {**_MAPPED, "uid": "osm-n43", "name": "Gone B.V.", "website": "https://gone.nl"}
    ranked_on = {}

    async def offices(country, limit, offset):
        return [_MAPPED, dead] if offset == 0 else []

    async def register(*a, **kw):
        raise AssertionError("the Swiss register was searched with Switzerland unticked")

    async def page(client, url):
        return (_NL_HOME, "https://datawerk.nl/nl/home") if "datawerk" in url else None

    async def rank(batch, cv_text, **kw):
        ranked_on.update({c["uid"]: c["purpose"] for c in batch})
        return {}

    monkeypatch.setattr(osm, "find_offices", offices)
    monkeypatch.setattr(reg, "find_companies", register)
    monkeypatch.setattr(reg, "fetch_page", page)
    monkeypatch.setattr(ai, "rank_companies", rank)

    await cs.run("")

    company = await db.get_company("osm-n42")
    assert (company["country"], company["website"], company["website_proof"]) == (
        "NL", "https://datawerk.nl", "map")
    assert "AI voor de zorg" in ranked_on["osm-n42"], "the ranking did not read the homepage"
    assert "dataLayer" not in ranked_on["osm-n42"], "the homepage's script was read as its text"
    assert company["site_languages"].split(",")[:2] == ["nl", "en"]
    assert await db.get_company("osm-n43") is None, "an office whose site is gone was stored"
    assert (await db.get_app_settings())["company_offsets"] == '{"NL": 2}'


async def test_a_homepage_down_on_the_day_keeps_what_was_read_before(db, monkeypatch):
    await _ready(db, monkeypatch, [])
    await db.save_company_countries(["NL"])
    await db.add_companies([{**_MAPPED, "website_proof": "map", "site_languages": "en"}])
    await db.save_company_fit("osm-n42", 90, "Builds AI products")

    async def offices(country, limit, offset):
        return []

    async def nothing(*a, **kw):
        return None

    monkeypatch.setattr(osm, "find_offices", offices)
    monkeypatch.setattr(reg, "fetch_page", nothing)

    await cs.run("")

    company = await db.get_company("osm-n42")
    assert company["checked_at"] is not None
    assert (company["website"], company["site_languages"]) == ("https://datawerk.nl", "en")


async def test_approaching_a_mapped_company_names_its_country_and_site(db):
    from fastapi.testclient import TestClient

    import main

    await db.add_companies([{**_MAPPED, "website_proof": "map",
                             "purpose": "IT company | AI products for hospitals"}])
    await db.save_company_fit("osm-n42", 88, "Builds AI products")
    with TestClient(main.app) as client:
        r = client.post("/companies/osm-n42/approach", follow_redirects=False)

    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/"), (
        "a mapped company could not be approached")
    job = await db.get_job(int(r.headers["location"].rsplit("/", 1)[-1]))
    assert (job["location"], job["url"]) == ("Utrecht, Netherlands", "https://datawerk.nl")
    assert "from their website: IT company" in job["description"]


async def test_a_busy_map_service_is_asked_again_not_given_up_on(monkeypatch):
    """On 12 Nov 2026 three countries in a row got a 502 within a second; the
    same query answered a minute later. Giving up at once skips the country."""
    answers = [502, 429, 200]

    class _Busy:
        async def post(self, url, data=None):
            status = answers.pop(0)
            request = httpx.Request("POST", url)
            body = {"elements": [{"type": "node", "id": 1, "tags": {
                "office": "it", "name": "Datawerk B.V.", "website": "datawerk.nl"}}]}
            return httpx.Response(status, json=body if status == 200 else {}, request=request)

    monkeypatch.setattr(osm, "_ATTEMPTS", (0, 0, 0))

    rows = await osm.find_offices("NL", 10, 0, client=_Busy())

    assert [r["name"] for r in rows] == ["Datawerk B.V."]

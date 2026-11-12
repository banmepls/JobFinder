"""The languages a company works in, and whether one of them is the user's.

Three sources, strongest first: the careers page (the language they hire in),
the website (the languages they offer it in), and where the company sits.
Measured on the 12 Swiss websites found on 11 Nov 2026: the <html lang>
attribute was wrong on two, so a page's text is read before its declaration.

Where a company sits: for Switzerland, the register entry - every register
writes in its canton's official language, so a Zurich company's purpose is
German even when the office speaks English. For Belgium, the postcode's
language area. For Austria, the Netherlands and Luxembourg, the country's.
"""

from __future__ import annotations

import re

from matcher import language
from scrapers.base import strip_html

# A canton's register language, for a purpose too short to read. Bilingual
# cantons by majority; the rest of Switzerland writes in German.
_CANTON_LANGUAGE = {"GE": "fr", "VD": "fr", "NE": "fr", "JU": "fr", "FR": "fr", "VS": "fr",
                    "TI": "it"}
# Luxembourg's administration works in French and German alike.
_COUNTRY_LANGUAGES = {"AT": ["de"], "NL": ["nl"], "LU": ["fr", "de"]}
_HTML_LANG = re.compile(r"<html\b[^>]*\blang=[\"']?([a-z]{2})\b", re.I)
_HREFLANG = re.compile(r"\bhreflang=[\"']?([a-z]{2})\b", re.I)
_LANG_PATH = re.compile(r"\bhref=[\"'](?:https?://[^\"'/]+)?/(en|fr|de|it|nl)(?:[/\"'?#])", re.I)


def belgian_languages(postcode: str | None) -> list[str]:
    """The language area of a Belgian postcode: Brussels is both French and
    Dutch, the German-speaking community sits at 4700-4799."""
    m = re.match(r"\s*(\d{4})", str(postcode or ""))
    if not m:
        return []
    n = int(m.group(1))
    if 1000 <= n <= 1299:
        return ["fr", "nl"]
    if 4700 <= n <= 4799:
        return ["de"]
    if 1300 <= n <= 1499 or 4000 <= n <= 7999:
        return ["fr"]
    if 1500 <= n <= 3999 or 8000 <= n <= 9999:
        return ["nl"]
    return []


def region_languages(company: dict) -> list[str]:
    """The language of where the company sits. A Swiss company's is read off
    its registered purpose when that can be read, else its canton's - bilingual
    Bern, Fribourg and Valais register in both."""
    country = company.get("country") or "CH"
    if country == "CH":
        code = language.detect_short(company.get("purpose")) or (
            _CANTON_LANGUAGE.get(company.get("canton"), "de") if company.get("canton") else None)
        return [code] if code else []
    if country == "BE":
        return belgian_languages(company.get("postcode"))
    return list(_COUNTRY_LANGUAGES.get(country, []))


def page_language(html: str | None) -> str | None:
    """What a page is written in: its text when long enough to judge, else the
    language it declares. None when neither says."""
    code = language.detect(strip_html(html or ""))
    if code is None:
        m = _HTML_LANG.search(html or "")
        code = m.group(1).lower() if m else None
    return code


def site_languages(html: str | None) -> list[str]:
    """The languages a site is offered in: the page's own, then every version
    it links to (hreflang, or a /en/ /fr/ /de/ /it/ /nl/ path)."""
    found = [page_language(html)]
    found += [c.lower() for c in _HREFLANG.findall(html or "")]
    found += [c.lower() for c in _LANG_PATH.findall(html or "")]
    return list(dict.fromkeys(c for c in found if c))


def company_languages(company: dict) -> tuple[list[str], str]:
    """The languages a company is known to work in, and where that was seen.

    Only the strongest source counts: a careers page in German says more about
    who they hire than an English version of their homepage does."""
    if company.get("careers_language"):
        return [company["careers_language"]], "careers page"
    if company.get("site_languages"):
        return company["site_languages"].split(","), "website"
    where = "register" if (company.get("country") or "CH") == "CH" else "region"
    return region_languages(company), where


def works_in(company: dict, mine: set[str]) -> bool:
    """Whether the company works in one of the user's languages. With no
    languages of the user's, or nothing known about the company's, nothing is
    ruled out - an unknown language is never a reason to hide a company."""
    langs, _ = company_languages(company)
    return not mine or not langs or bool(mine & set(langs))

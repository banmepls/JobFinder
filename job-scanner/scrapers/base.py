"""Shared data model.

Architecture note: scraping is done by a Chrome Extension (Manifest V3) that
reads the LinkedIn DOM inside the user's own logged-in session and POSTs results
to /api/ingest. The backend performs NO external requests and runs NO Playwright
or Selenium. This module therefore holds the normalized `JobListing` dataclass
shared between the ingest endpoint, storage and matcher, plus the one HTML-to-
text reader every source goes through.
"""

from __future__ import annotations

import html as html_module
import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# LinkedIn puts the job title in the card twice - a visible span and a
# screen-reader copy that adds "with verification". Older extension builds read
# the wrapper element and sent the two glued together
# ("Project Manager (Remote)Project Manager (Remote)"). The extension reads the
# right span now, but the server normalises anyway: a stale extension is still
# installed in someone's browser, and a bad title poisons the dedup fingerprint.
_DOUBLED_TITLE = re.compile(r"^(.{6,}?)\s*\1(?:\s+with verification)?$", re.IGNORECASE)


def clean_title(title: str | None) -> str:
    """Collapse a title that repeats itself. Leaves normal titles untouched."""
    text = " ".join((title or "").split())
    match = _DOUBLED_TITLE.match(text)
    return match.group(1).strip() if match else text


# Any tag, for the fallback below. Crude on purpose: it runs only when the
# proper parser cannot.
_TAGS = re.compile(r"<[^>]+>")


def strip_html(markup: str | None) -> str:
    """An HTML fragment as readable text. Never raises.

    BeautifulSoup resolves a numeric entity in the 128-159 range - `&#150;`, the
    en dash that job ads are full of - by decoding it through windows-1252. The
    packaged app did not carry that codec, so `LookupError: unknown encoding:
    windows-1252` came out of a parser call and travelled all the way up to the
    scan loop: 84 whole searches died on it over seven days, every one of them
    on a source that had answered 200. Nothing in the dev tree could reproduce
    it, because a normal Python install has every codec.

    The build now carries the codecs (desktop/JobFinder.spec). This is the belt:
    a description read without the parser is worth incomparably more than a
    search that ends in a traceback."""
    if not markup:
        return ""
    try:
        text = BeautifulSoup(markup, "html.parser").get_text("\n")
    except LookupError as exc:
        logger.warning("Falling back to plain tag-stripping: %s", exc)
        text = html_module.unescape(_TAGS.sub("\n", markup))
    return "\n".join(ln.strip() for ln in text.splitlines() if ln.strip()).strip()


def kw_matches(query: str, *fields: str) -> bool:
    """True if every word of the query STARTS a word somewhere in the fields.

    At the start of a word, not anywhere inside one. A plain substring test let
    "rpa" match the German "verpasst", "bpm" a medicines code "(cbpm)" and "llm"
    "fulfillment": 4 of the 13 adverts a skills search brought back on 6 Nov
    2026 were such accidents, each costing a model call. A prefix still
    matches - "engineer" finds "Engineers" and "engineering" - and punctuation
    is a word edge, so "(RPA)" and "RPA-based" match.

    One copy for every board that filters on its own side; there used to be
    two, which is how drift starts."""
    words = (query or "").lower().split()
    if not words:
        return True
    hay = " ".join(f.lower() for f in fields if f)
    return all(re.search(r"(?<!\w)" + re.escape(w), hay) for w in words)


@dataclass
class JobListing:
    """Normalized vacancy record produced by the extension / ingest endpoint."""

    source: str
    external_id: str
    title: str
    company: str | None = None
    location: str | None = None
    description: str | None = None
    url: str | None = None
    posted_at: str | None = None  # ISO string or source-provided text
    # Full-time / Part-time / Contract / Internship / Freelance, when the source
    # states it. Every board carries this and none of it was being read, so an
    # internship and a freelance gig scored like permanent roles.
    employment_type: str | None = None

    def __post_init__(self) -> None:
        # Normalise here rather than at each call site, so every source (ingest,
        # public fetchers) stores the same shape and dedup sees a clean title.
        self.title = clean_title(self.title)

    def is_valid(self) -> bool:
        return bool(self.source and self.external_id and self.title)

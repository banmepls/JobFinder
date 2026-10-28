"""Default CV document - the user's résumé recreated as editable HTML.

Rendered inside the .cv template (see templates/cv.html) which reproduces the
original layout: name with a heavy rule, uppercase section headers with a thin
rule, experience entries with a right-aligned italic date, and bullet lists.
This HTML is the initial content of profile.cv_html; the user edits it in place,
and the server renders it to a real downloadable PDF with the same CSS.
"""

import re
from datetime import date


def build_cv_css(base_px: float = 12.5) -> str:
    """Résumé (.cv-doc) CSS at a given base font size. Everything scales in `em`
    off `base_px`, so shrinking the base tightens both text and spacing - used by
    the PDF auto-fit loop to keep the CV on a single page."""
    return f"""
.cv-doc {{ font-family: Calibri, Carlito, "Segoe UI", Helvetica, Arial, sans-serif;
          color:#111; font-size:{base_px}px; line-height:1.4; }}
.cv-doc .cv-name {{ font-size:1.76em; font-weight:700; letter-spacing:.5px;
                   border-bottom:.18em solid #111; padding-bottom:.4em; margin-bottom:.55em; }}
.cv-doc .cv-subtitle {{ font-size:1em; }}
.cv-doc .cv-contact {{ font-size:1em; margin-top:.1em; }}
.cv-doc h2 {{ font-size:1em; font-weight:700; text-transform:uppercase; letter-spacing:.4px;
             border-bottom:1px solid #111; padding-bottom:.15em; margin:1.1em 0 .45em; }}
.cv-doc .entry {{ margin-bottom:.6em; }}
.cv-doc .entry-head {{ display:flex; justify-content:space-between; gap:1em; }}
.cv-doc .entry-head .date {{ font-style:italic; white-space:nowrap; }}
.cv-doc .entry-desc {{ font-style:italic; }}
.cv-doc ul {{ margin:.2em 0 0; padding-left:1.4em; }}
.cv-doc li {{ margin-bottom:.22em; }}
.cv-doc p {{ margin:0; }}
.cv-doc a {{ color:inherit; text-decoration:underline; }}
.cv-doc .cv-li {{ font-size:.7em; font-weight:700; color:#0a66c2; text-decoration:none;
                 border:1px solid #0a66c2; border-radius:3px; padding:0 3px; vertical-align:middle; }}
""".strip()


# Screen default (unchanged look for the editor).
CV_DOC_CSS = build_cv_css(12.5)


# --------------------------------------------------------------------------- #
# Dates on the CV: how long each role lasted, and years across several roles.  #
# Arithmetic, so it is done here rather than asked of a model - which counted  #
# eight years as short of "5+" when left to read dates itself.                 #
# --------------------------------------------------------------------------- #

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_NOW = r"present|now|current(?:ly)?|today|ongoing|to date"
_POINT = r"(?:(?P<{p}m>[A-Za-z]{{3,9}}\.?|\d{{1,2}})[\s/.]*)?(?P<{p}y>(?:19|20)\d\d)"
_RANGE = re.compile(
    _POINT.format(p="a") + r"\s*(?:-|–|—|to|until)\s*(?:" + _POINT.format(p="b")
    + r"|(?P<now>" + _NOW + r"))\b", re.IGNORECASE)


def _month(raw: str | None) -> int | None:
    """1-12 from "Aug", "August", "08"; None when it is not a month."""
    if not raw:
        return None
    raw = raw.rstrip(".").lower()
    if raw.isdigit():
        return int(raw) if 1 <= int(raw) <= 12 else None
    return _MONTHS.index(raw[:3]) + 1 if raw[:3] in _MONTHS else None


def date_span(text: str | None, today: date) -> tuple[int, int] | None:
    """The first date range in `text` as (first, last) month numbers, both
    counted in. None when there is no range to read - never a guess.

    A bare year starts in January and, as an end, stops at the December before
    it: "2018 - 2021" is three years, not four."""
    m = _RANGE.search(text or "")
    if not m:
        return None
    start = int(m["ay"]) * 12 + ((_month(m["am"]) or 1) - 1)
    if m["now"]:
        end = today.year * 12 + today.month - 1
    else:
        end_month = _month(m["bm"])
        end = int(m["by"]) * 12 + (end_month - 1 if end_month else -1)
        if not end_month and int(m["by"]) == int(m["ay"]):
            end = start + 11 - ((_month(m["am"]) or 1) - 1)   # "2021 - 2021": that year
    end = min(end, today.year * 12 + today.month - 1)
    return (start, end) if end >= start else None


def months_in(spans) -> int:
    """Months covered by any of the spans; overlapping roles count once."""
    covered: set[int] = set()
    for first, last in spans:
        covered.update(range(first, last + 1))
    return len(covered)


def years_in(spans) -> int | None:
    """Whole years across the spans, rounded to nearest. None when no span
    could be read: an unknown is not zero, and zero years would mark every
    advert that asks for experience as out of reach."""
    spans = [s for s in spans if s]
    return int(months_in(spans) / 12 + 0.5) if spans else None


def length_text(months: int) -> str:
    years, rest = divmod(months, 12)
    parts = ([f"{years} yr{'s' if years > 1 else ''}"] if years else []) + \
            ([f"{rest} mo{'s' if rest > 1 else ''}"] if rest else [])
    return " ".join(parts) or "under a month"


def _norm(el) -> str:
    return " ".join(el.get_text(" ").split())


def _entry_head(el, today: date) -> tuple[str, tuple[int, int] | None]:
    """An entry heading as one line - role, company, place | dates - and the
    span of those dates. The dates sit in their own span when the importer put
    them there; the no-AI fallback leaves them in the heading text."""
    date_el = el.select_one(".date")
    when = _norm(date_el) if date_el else ""
    if date_el:
        date_el.decompose()
    what = _norm(el)
    line = f"{what} | {when}" if what and when else what or when
    return line, date_span(when or what, today)


def cv_entries(cv_html: str, today: date | None = None) -> list[dict]:
    """Each entry on the CV, in document order: its heading line and the span
    of its dates (None when they cannot be read). Numbered from 1 in the same
    order as `cv_to_text(..., numbered=True)`."""
    from bs4 import BeautifulSoup

    today = today or date.today()
    soup = BeautifulSoup(cv_html or "", "html.parser")
    out = []
    for el in soup.select(".entry-head"):
        line, span = _entry_head(el, today)
        out.append({"heading": line, "span": span})
    return out


def cv_to_text(cv_html: str, today: date | None = None, numbered: bool = False) -> str:
    """Plain-text version of the WHOLE CV document (headline, summary, section
    headers, each entry's heading, skills and bullets), used as the résumé
    everywhere the AI needs it. The CV document is the single source of truth
    for the résumé.

    The entry heading - role, company, place and dates - is a div, not a p or
    li, and was skipped until 28 Oct 2026: the AI got every bullet as one list
    with no job title, employer or date, while the page showed the CV whole.
    Each heading now also carries how long the role lasted, computed here.
    `numbered` prefixes the headings [1], [2]… in `cv_entries` order."""
    from bs4 import BeautifulSoup

    today = today or date.today()
    soup = BeautifulSoup(cv_html or "", "html.parser")
    lines: list[str] = []
    sub = soup.select_one(".cv-subtitle")
    if sub and _norm(sub):
        lines.append(f"HEADLINE: {_norm(sub)}")
    n = 0
    for el in soup.find_all(["h2", "p", "li", "div"]):
        if el.name == "div" and "entry-head" not in (el.get("class") or []):
            continue
        if el.name == "div":
            txt, span = _entry_head(el, today)
            if span:
                txt += f" ({length_text(span[1] - span[0] + 1)})"
            n += 1
            if numbered:
                txt = f"[{n}] {txt}"
        else:
            txt = _norm(el)
        if not txt:
            continue
        if el.name == "h2":
            lines.append(f"\n{txt.upper()}")
        elif el.name == "li":
            lines.append(f"- {txt}")
        elif el.name == "div":  # entry heading: a blank line before each job
            lines.append(f"\n{txt}")
        else:  # summary paragraph, or an entry's one-line description
            lines.append(txt)
    return "\n".join(lines).strip()

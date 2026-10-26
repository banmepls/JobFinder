"""Seniority signals extracted in code, not left to the model.

Two facts decide most seniority calls, and both are mechanical:

* the level word in the TITLE ("Senior Technical Project Manager"), and
* the years of experience the ad actually REQUIRES.

The model kept scoring titles that literally say "Senior" at 65-75 even though
the candidate's instructions list Senior/Lead/Principal under "Reject" - so the
title check is enforced here rather than asked for. Nothing personal is
hardcoded: which levels are unacceptable is read from the user's own
instructions, and if they never say to reject a level, nothing is blocked.
"""

from __future__ import annotations

import re

# Level words as they appear in a job TITLE. Ordered longest-first so
# "head of" wins over a bare "head".
_LEVELS: tuple[str, ...] = (
    "manager of managers", "head of", "c-level", "chief", "director", "vp",
    "principal", "staff", "senior", "sr.", "sr", "lead",
)

# "Lead" in these phrases is a noun, not a level ("Lead Generation Specialist").
_LEAD_NOT_A_LEVEL = re.compile(r"\blead\s+(generation|gen\b|qualification|nurtur)", re.I)

# Parenthetical asides carry words that aren't about the role's own level:
# "Project Manager (Senior Stakeholders)" is not a senior role.
_PARENS = re.compile(r"\([^)]*\)|\[[^\]]*\]")

# "5+ years of experience", "3-6 years experience", "minimum 4 yrs experience".
# The number must be tied to experience, otherwise "7 years on the market" and
# "a CEO with 15 years' experience" get read as requirements (both real cases).
_YEARS_REQ = re.compile(
    r"\b(\d{1,2})\s*\+?\s*(?:[-–]\s*(\d{1,2})\s*)?(?:years?|yrs?)\b[^.;]{0,40}?experience",
    re.IGNORECASE,
)
# Phrases that mean the years belong to the COMPANY or a COLLEAGUE, not to the
# person being hired. Deliberately narrow: a bare "we"/"our" would throw away
# "We need 5+ years of experience", which is how most ads phrase the real
# requirement.
_NOT_A_REQUIREMENT = re.compile(
    r"\b(on the market|in business|founded|established|led by|ceo|founder|"
    r"cto|co-?founder|our (?:company|team|founders) (?:has|have|bring))\b",
    re.IGNORECASE,
)


def title_level(title: str | None) -> str | None:
    """The seniority word the TITLE claims for the role itself, if any."""
    text = _PARENS.sub(" ", title or "").lower()
    if _LEAD_NOT_A_LEVEL.search(text):
        text = _LEAD_NOT_A_LEVEL.sub(" ", text)
    for level in _LEVELS:
        if re.search(rf"(?<![\w-]){re.escape(level)}(?![\w-])", text):
            return level
    return None


def required_years(description: str | None) -> int | None:
    """Lowest number of years the ad REQUIRES, or None when it doesn't say.

    The lower bound of a range is what the candidate has to clear: "3-6 years"
    asks for 3."""
    text = " ".join((description or "").split())
    found: list[int] = []
    for match in _YEARS_REQ.finditer(text):
        prefix = text[max(0, match.start() - 70):match.start()]
        if _NOT_A_REQUIREMENT.search(prefix):
            continue          # the company's age, or someone else's experience
        years = int(match.group(1))
        if 0 < years <= 40:
            found.append(years)
    return min(found) if found else None


def years_shortfall(job: dict) -> tuple[int, int] | None:
    """(required, have) when the advert asks for more years than the candidate
    has, by more than the margin they allowed. None when either number is
    unknown - this never invents a rule.

    Both numbers are stated facts: one is in the advert, the other is what the
    candidate entered on the search that found the job (`search_years`). The
    model was left to compare them against the CV and miscounted: eight years
    of experience read as short of "5+" (measured live, 26 Oct 2026)."""
    have = job.get("search_years")
    if have is None:
        return None
    need = required_years(job.get("description"))
    if need is None:
        return None
    margin = job.get("search_years_margin") or 0
    if need - int(have) > int(margin):
        return need, int(have)
    return None


def rejected_levels(instructions: str | None) -> set[str]:
    """Level words the candidate listed as unacceptable, in their own words.

    Reads the lines under a 'Reject'/'Not interested' heading. Empty set when
    they never said so - this never invents a rule."""
    text = (instructions or "")
    out: set[str] = set()
    for match in re.finditer(r"(reject|not interested|avoid|exclude)\b", text, re.I):
        # Their list follows the heading; stop at a blank line or a new heading.
        chunk = text[match.end(): match.end() + 600]
        chunk = re.split(r"\n\s*\n|\n[A-Z][A-Z ]{3,}:", chunk)[0].lower()
        for level in _LEVELS:
            if re.search(rf"(?<![\w-]){re.escape(level)}(?![\w-])", chunk):
                out.add(level)
    return out


def blocking_level(job: dict, instructions: str | None) -> str | None:
    """The title's level word when the candidate said to reject that level."""
    level = title_level(job.get("title"))
    if level and level in rejected_levels(instructions):
        return level
    return None


def signals(job: dict) -> list[str]:
    """Human-readable seniority facts for the vacancy block sent to the model."""
    lines: list[str] = []
    level = title_level(job.get("title"))
    if level:
        lines.append(f"Title seniority: {level.title()} (stated in the job title)")
    years = required_years(job.get("description"))
    if years is not None:
        lines.append(f"Experience required: {years}+ years")
    have = job.get("search_years")
    if have is not None:
        lines.append(f"Candidate experience: {have} years (the candidate's own figure)")
    return lines

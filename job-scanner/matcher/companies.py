"""The model's part of the company search: which purpose terms to look for,
which companies fit the CV, and what a careers page offers.

Three judgements, split by role like everything else here. The terms are a
handful of words a person reviews before anything runs, so they are WRITING.
Ranking hundreds of registered purposes and reading careers pages is judgement
at volume that returns numbers and lists, so it is SCANNING. Searching the
register, proving a website and counting years stay in code.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI

from db import usage

logger = logging.getLogger(__name__)

_TIMEOUT = 60.0
RANK_BATCH = 15

_TERMS_SYSTEM = (
    "You read a candidate's CV and list the words a Swiss company's REGISTERED "
    "PURPOSE (the 'Zweck' filed in the commercial register) would contain if the "
    "company does the kind of work this candidate does, so the register can be "
    "searched for employers that never post on job boards.\n"
    "Purposes are written in German, French or Italian, and sometimes English, in "
    "plain legal prose: 'Entwicklung und Vertrieb von Software', 'conseil en "
    "informatique', 'sviluppo di software'.\n"
    "Return ONLY valid JSON: {\"terms\": [\"...\", \"...\"]}\n"
    "- 8 to 12 terms, covering German, French and Italian for each area of the CV.\n"
    "- Each term 1 to 3 words, letters only, lowercase, as the purpose text would "
    "write it (a word stem is fine: 'automatisierung', 'informatique').\n"
    "- Specific enough to find the right companies: never words found in almost "
    "every purpose, such as 'dienstleistungen', 'beratung', 'handel', 'services', "
    "'conseil', 'gestion'.\n"
    "Output nothing outside the JSON object."
)

_RANK_SYSTEM = (
    "You judge which companies could employ this candidate, from what each "
    "company does: for a Swiss company its REGISTERED PURPOSE - the legal "
    "description filed in the register - and for others the opening of ITS OWN "
    "HOMEPAGE (kind of office | title | summary | first text). Any language.\n"
    "For each numbered company, rate 0-100 how likely its business is to need "
    "the kind of work the candidate's CV shows:\n"
    "  80-100 its core business is that kind of work, or relies on it centrally\n"
    "  50-79  related: the work would be a real part of the company\n"
    "  0-49   unrelated, or a description so generic it says nothing (holding, "
    "investments, trade in goods of all kinds, real estate, a cookie banner)\n"
    "Judge only from that description and the CV. Never guess the company's "
    "size, location or whether it is hiring.\n"
    'Reply with JSON: {"results": [{"n": 1, "fit": 0-100, "reason": "at most 12 '
    'words"}, ...]} - one entry for every number you were given.'
)

_CAREERS_SYSTEM = (
    "You read the careers page of one company and report what it offers right "
    "now. You get the page's text and its links as 'text -> url'.\n"
    'Return ONLY valid JSON: {"openings": [{"title": "...", "url": "..."}], '
    '"open_applications": true|false}\n'
    "- openings: the positions the page advertises now, each title exactly as "
    "written. url is that posting's own link from the list, or \"\" when it has "
    "none. Never invent a position or a link. No positions listed -> [].\n"
    "- open_applications: true when the page invites unsolicited applications "
    "(Spontanbewerbung, Initiativbewerbung, candidature spontanée, candidatura "
    "spontanea, open application).\n"
    "Output nothing outside the JSON object."
)


def _client(api_key: str, base_url: str | None, timeout: float | None) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, base_url=base_url or None,
                       timeout=timeout or _TIMEOUT, max_retries=2)


async def suggest_terms(cv_text: str, model: str | None = None, api_key: str | None = None,
                        base_url: str | None = None) -> list[str]:
    """Purpose terms from the CV, for the person to review. [] on any failure."""
    if not (api_key or "").strip() or not (cv_text or "").strip():
        return []
    try:
        resp = await _client(api_key, base_url, None).chat.completions.create(
            model=model, temperature=0.2, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": _TERMS_SYSTEM},
                      {"role": "user", "content": cv_text[:12000]}])
        await usage.record("write", model, getattr(resp, "usage", None))
        terms = json.loads(resp.choices[0].message.content or "{}").get("terms") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Company terms failed: %s", exc)
        return []
    return [str(t) for t in terms if isinstance(t, str)]


async def rank_companies(companies: list[dict[str, Any]], cv_text: str, *, model: str,
                         api_key: str, base_url: str | None = None,
                         timeout: float | None = None, no_reasoning: dict | None = None,
                         reasoning_reserve: int = 0) -> dict[str, tuple[int, str]]:
    """{uid: (fit, reason)} for one batch. A company the model skipped is simply
    absent - it stays unranked and the next run asks again, rather than being
    stored as a zero nobody decided on. Raises on a failed call, for the same
    reason: the caller leaves the whole batch unranked."""
    listing = "\n".join(
        f"{i}. {c['name']} ({c.get('town') or '?'}): {(c.get('purpose') or '')[:400]}"
        for i, c in enumerate(companies, start=1))
    # The CV in the SYSTEM message: identical for every batch, so it is cached.
    system = _RANK_SYSTEM + f"\n\n=== CANDIDATE CV ===\n{cv_text.strip()}\n"
    resp = await _client(api_key, base_url, timeout).chat.completions.create(
        model=model, temperature=0.0, max_tokens=40 * len(companies) + 200 + reasoning_reserve,
        extra_body=no_reasoning or None, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": "=== COMPANIES ===\n" + listing}])
    await usage.record("company", model, getattr(resp, "usage", None))
    data = json.loads(resp.choices[0].message.content or "{}")
    out: dict[str, tuple[int, str]] = {}
    for item in data.get("results") or []:
        try:
            n, fit = int(item.get("n")), int(item.get("fit"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= n <= len(companies):
            out[companies[n - 1]["uid"]] = (max(0, min(100, fit)),
                                            " ".join(str(item.get("reason") or "").split())[:120])
    return out


async def read_careers_page(text: str, links: list[tuple[str, str]], company: str, *,
                            model: str, api_key: str, base_url: str | None = None,
                            timeout: float | None = None, no_reasoning: dict | None = None,
                            reasoning_reserve: int = 0) -> dict | None:
    """{"openings": [{"title", "url"}], "open_applications": bool}, or None when
    the page could not be read. A link the page does not contain is dropped:
    the model cites, it does not invent."""
    known = {url for _, url in links}
    listing = "\n".join(f"{t} -> {u}" for t, u in links)
    try:
        resp = await _client(api_key, base_url, timeout).chat.completions.create(
            model=model, temperature=0.0, max_tokens=1200 + reasoning_reserve,
            extra_body=no_reasoning or None, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": _CAREERS_SYSTEM},
                      {"role": "user", "content": f"Company: {company}\n\n=== PAGE TEXT ===\n"
                                                  f"{text[:6000]}\n\n=== LINKS ===\n{listing}"}])
        await usage.record("company", model, getattr(resp, "usage", None))
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Careers page of %s not read: %s", company, exc)
        return None
    openings = []
    for item in data.get("openings") or []:
        title = " ".join(str((item or {}).get("title") or "").split())[:140]
        url = str((item or {}).get("url") or "")
        if title:
            openings.append({"title": title, "url": url if url in known else ""})
    return {"openings": openings[:30], "open_applications": bool(data.get("open_applications"))}

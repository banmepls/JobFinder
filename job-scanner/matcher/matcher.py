"""AI matcher - two-stage, token-efficient scoring against the user profile.

Stage 1 (quick, automatic on every scan):
    quick_score(job, instructions) - uses ONLY the candidate's instructions
    (no resume, no case bank) with a short capped output. Cheap; filters trash.

Stage 2 (advanced, on demand for good matches):
    advanced(job, profile) - uses resume + case bank + instructions to produce
    concrete strengths/weaknesses, also length-capped.

Both degrade gracefully: on any API/parse error they return a safe result so one
bad call never crashes the pipeline. The resume is passed as user-message
context, never baked into the system prompt.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from config import settings
from db import usage
from matcher import language, residency, seniority, workplace

logger = logging.getLogger(__name__)

try:
    from paths import app_root as _app_root
    _PROMPTS = _app_root() / "matcher" / "prompts"
except Exception:  # pragma: no cover - fallback for standalone use
    _PROMPTS = Path(__file__).resolve().parent / "prompts"
_MAX_DESC_CHARS = 6000        # advanced (on-demand, few jobs) keeps full context
# Quick pass: keep enough of the description that the REQUIREMENTS block (years
# of experience, required languages, work mode) is usually included - those are
# the obvious blockers the quick screen must catch. 2200 was too short and cut
# them off, letting blocked roles slip through with a high score.
_QUICK_DESC_CHARS = 4500
_REQUEST_TIMEOUT = 18.0
# Rate limits are the common failure, not outages. A dense scan runs straight
# into the 200k tokens-per-minute ceiling, and the SDK's own backoff is the
# cheapest way through: one retry was not enough, and every exhausted call used
# to be written to the database as a score of 0.
_MAX_RETRIES = 4
# Output caps (tokens) - keep responses short to save money without hurting the
# signal we actually use.
_TITLE_MAX_TOKENS = 50
_QUICK_MAX_TOKENS = 160
# The advanced answer is not just score + pros/cons any more: it also carries
# 6-12 cited coverage findings, each with a quote, a source and a line of
# evidence. That is ~1200-1800 output tokens, and 800 truncated the JSON
# mid-array so json.loads failed and the whole check silently produced nothing.
# On a provider with no reasoning_reserve (OpenAI adds 0) this cap IS the whole
# budget, so it has to fit the real answer on its own. A ceiling, not a charge:
# unused tokens are not billed.
_ADVANCED_MAX_TOKENS = 2500
# The advanced check also runs as two separate calls (advanced_verdict +
# advanced_coverage) so the page can show the verdict the moment it lands while
# the evidence is still being cited. The verdict is small - a score and a few
# short phrases - so it returns fast; coverage carries the 6-12 findings and
# needs the room `advanced()` used to give the whole answer.
_VERDICT_MAX_TOKENS = 600
_COVERAGE_MAX_TOKENS = 2200


@dataclass
class TitleResult:
    plausible: bool
    score: int
    reason: str


@dataclass
class QuickResult:
    score: int
    summary: str
    red_flags: list[str] = field(default_factory=list)
    # True when no verdict was reached: an empty answer, unreadable JSON, or a
    # dead endpoint. The score field is then meaningless and MUST NOT be stored.
    # Writing 0 for these is permanent - the job leaves the queue carrying a
    # number the model never produced, below every threshold, invisible.
    failed: bool = False


@dataclass
class AdvancedResult:
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    score: int | None = None
    error: str | None = None
    # Raw findings: each a quote from the advert plus what answers it. Kept as
    # the model returned them; matching them to the real text is the caller's
    # job (matcher.coverage), because that is where they get verified.
    coverage: list[dict] = field(default_factory=list)


class OutOfTokens(RuntimeError):
    """The model used its whole budget thinking and never wrote an answer.

    Reasoning models spend tokens before they emit content. Given too small a
    ceiling they return an EMPTY string with finish_reason="length", which is
    indistinguishable from a broken provider unless it is named - it was read
    as "DeepSeek does not work" for a day."""


def _require_content(resp) -> str:
    choice = resp.choices[0]
    text = choice.message.content or ""
    if not text.strip() and getattr(choice, "finish_reason", "") == "length":
        raise OutOfTokens(
            "the model spent its entire output budget on reasoning and wrote "
            "nothing; raise the provider's reasoning_reserve")
    return text


def _read(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8")


# LinkedIn shows the work arrangement as a chip next to the location, not in the
# description prose, so the model never sees it unless we surface it ourselves.
# Score ceiling for a posting that requires living in a country the candidate
# did not name. Deliberately above the hard-blocker cap of 25: the job is real
# and in reach, it just costs a move - so it stays visible, below the review
# threshold rather than at the top of it.
_RESIDENCY_CAP = 55
# Score ceiling for an advert asking more years than the search's figure plus
# its margin. The same soft tier, chosen by the user over the blocker cap: ads
# routinely ask 5+ and hire 3, so the job stays in the list, below the strong ones.
_YEARS_CAP = 55


_WORKPLACE_PATTERNS = (
    ("Hybrid", re.compile(r"\bhybrid\b", re.IGNORECASE)),
    ("On-site", re.compile(r"\bon[\s-]?site\b|\bin[\s-]office\b|\bonsite\b", re.IGNORECASE)),
    ("Remote", re.compile(r"\bremote\b|\btelecommute\b|\bwork from home\b", re.IGNORECASE)),
)


def detect_workplace(job: dict[str, Any]) -> str | None:
    """Best-effort work arrangement for a vacancy.

    Order matters: "Hybrid" wins over "Remote" because hybrid ads almost always
    also say "remote" somewhere ("2 days remote"), and hybrid is the stricter
    fact for someone who needs full remote."""
    haystack = " ".join(str(job.get(k) or "") for k in ("location", "title"))
    head = (job.get("description") or "")[:1200]      # requirements live up top
    for name, pattern in _WORKPLACE_PATTERNS:
        if pattern.search(haystack) or pattern.search(head):
            return name
    return None


def _vacancy_block(job: dict[str, Any], max_chars: int = _MAX_DESC_CHARS) -> str:
    desc = (job.get("description") or "")[:max_chars]
    # State the description's language explicitly - models read a foreign
    # posting fluently and forget that the CANDIDATE would have to.
    code = language.detect(job.get("description"))
    lang_line = f"Description language: {language.name(code)}\n" if code else ""
    # Named `arrangement`, not `workplace`: the module of that name is imported
    # above and a local of the same name shadowed it.
    arrangement = detect_workplace(job)
    wp_line = f"Work arrangement: {arrangement}\n" if arrangement else ""
    # Straight from the source's own field. Without it an internship and a
    # freelance gig read like permanent roles.
    kind = (job.get("employment_type") or "").strip()
    kind_line = f"Employment type: {kind}\n" if kind else ""
    # Seniority read off the title and the requirements, so the model cannot
    # miss a "Senior" that is right there in the job title.
    sen_lines = "".join(f"{line}\n" for line in seniority.signals(job))
    # A remote posting can still demand you LIVE somewhere; say so plainly.
    res_lines = "".join(f"{line}\n" for line in residency.signals(job))
    # An office requirement stated deep in the body, which the metadata missed.
    res_lines += "".join(f"{line}\n" for line in workplace.signals(job))
    return (
        "=== VACANCY ===\n"
        f"Title: {job.get('title')}\n"
        f"Company: {job.get('company')}\n"
        f"Location: {job.get('location')}\n"
        f"{wp_line}"
        f"{kind_line}"
        f"{sen_lines}"
        f"{res_lines}"
        f"{lang_line}"
        f"Description:\n{desc or '(no description)'}\n"
    )


class AIMatcher:
    def __init__(self, model: str | None = None, api_key: str | None = None,
                 advanced_model: str | None = None, base_url: str | None = None,
                 reasoning_reserve: int = 0, timeout: float | None = None,
                 no_reasoning: dict | None = None, cv_text: str = "") -> None:
        # Room the model needs to think before it writes. Zero for models that
        # answer directly; see providers.Provider.reasoning_reserve.
        self.reserve = max(0, int(reasoning_reserve))
        # The CV as the scanning stages read it (cv_content.cv_to_text). Its own
        # block, never folded into the instructions: the gates parse those for
        # languages and countries, and a language a CV merely mentions is not
        # one the candidate asked to work in. Measured 28 Oct 2026: with
        # empty instructions and no CV, 22 of 22 real adverts scored 80-100 -
        # accountants and inside sales included.
        self.cv_text = (cv_text or "").strip()
        self.timeout = float(timeout or _REQUEST_TIMEOUT)
        # Applied to the scanning stages only. The deep check is a handful of
        # calls a day and is the one place the thinking is worth paying for.
        #
        # Sent through extra_body rather than as keyword arguments: the SDK
        # validates its own parameter names and rejects anything it does not
        # know, so a provider-specific field has to be forwarded verbatim.
        self.no_reasoning = dict(no_reasoning or {})
        self.init_error: str | None = None
        # FAST model - title screen + quick score (runs on every scanned job).
        self.model = model or settings.openai_model
        # DEEP model - the on-demand advanced check. Falls back to the fast one.
        self.advanced_model = advanced_model or self.model
        self._client: AsyncOpenAI | None = None
        key = (api_key or "").strip() or settings.openai_api_key
        if not key:
            self.init_error = "OPENAI_API_KEY not configured (set it in Settings)."
            logger.warning("OPENAI_API_KEY missing — matcher unavailable")
        else:
            try:
                self._client = AsyncOpenAI(
                    api_key=key,
                    # Any OpenAI-compatible endpoint; None keeps the SDK default.
                    base_url=base_url or None,
                    timeout=self.timeout,
                    max_retries=_MAX_RETRIES,
                )
            except Exception as exc:  # noqa: BLE001
                self.init_error = f"OpenAI client init failed: {exc}"
                logger.error(self.init_error)

    def _cv_block(self) -> str:
        """The CV for the scanning prompts, or nothing when there is none - the
        prompts then read exactly as they did before a CV could be given. Sits
        after the instructions in the SYSTEM message: identical for every job,
        so it rides in the cached prefix rather than being billed per job."""
        if not self.cv_text:
            return ""
        return ("\n=== CANDIDATE CV (what they have done; each role heading gives "
                f"its dates and length) ===\n{self.cv_text}\n")

    # ------------------------------------------------------------------ #
    # Stage 0 - title screen (title + metadata only, no description)      #
    # ------------------------------------------------------------------ #
    async def title_screen(self, job: dict[str, Any], instructions: str) -> TitleResult:
        """Ultra-cheap screen on the TITLE only. If not plausible, the caller
        skips fetching the description AND the full quick score - saving tokens."""
        if self._client is None:
            # Can't screen - let it through so nothing is silently dropped.
            return TitleResult(True, 0, "screen unavailable")
        # The instructions go in the SYSTEM message, not the user one.
        # OpenAI caches a prompt prefix automatically once it reaches 1024
        # tokens, and only if it is byte-identical between calls. With the
        # instructions in the user message the stable prefix was just this
        # prompt - 364 tokens, below the threshold - so the title screen was
        # paying full price for the same 1230 tokens on every single job.
        system = _read("title.txt") + (
            "\n\n=== CANDIDATE INSTRUCTIONS ===\n"
            f"{(instructions or '').strip() or '(none)'}\n"
        ) + self._cv_block()
        user = (
            "=== JOB (title/metadata only) ===\n"
            f"Title: {job.get('title')}\n"
            f"Company: {job.get('company')}\n"
            f"Location: {job.get('location')}\n"
        )
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                temperature=0.0,
                max_tokens=_TITLE_MAX_TOKENS + self.reserve,
                extra_body=self.no_reasoning or None,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            await usage.record("title", self.model, getattr(resp, "usage", None))
            data = json.loads(_require_content(resp) or "{}")
        except Exception as exc:  # noqa: BLE001 - on any error, don't drop the job
            logger.warning("Title screen failed for job %s: %s", job.get("id"), exc)
            return TitleResult(True, 0, "screen error")
        return TitleResult(
            plausible=bool(data.get("plausible", True)),
            score=_clamp_score(data.get("score", 0)),
            reason=str(data.get("reason", "")).strip()[:60],
        )

    # ------------------------------------------------------------------ #
    # Stage 1 - quick score (instructions only)                          #
    # ------------------------------------------------------------------ #

    # How many titles go into one screening call. The preamble - this prompt
    # plus the candidate's instructions - is 1230 tokens and used to be paid
    # once per job, while the job itself contributed 17. Ten at a time turns
    # 10 × 1247 into 1230 + 10 × 25.
    #
    # Ten and not fifty: a longer list gives the model more room to lose track
    # of which verdict belongs to which number, and a mis-aligned answer is
    # worse than a slightly larger bill.
    BATCH_SIZE = 10

    async def title_screen_batch(
        self, jobs: list[dict[str, Any]], instructions: str,
    ) -> list[TitleResult]:
        """Screen several titles in one call. Returns one result per job, in order.

        Anything the model fails to answer for is let THROUGH, never dropped: a
        missing verdict means the screen did not run, and a job silently
        discarded because of a malformed response is invisible to the user."""
        if not jobs:
            return []
        if self._client is None or len(jobs) == 1:
            # One job is cheaper unbatched (no numbering overhead), and without
            # a client there is nothing to batch.
            return [await self.title_screen(job, instructions) for job in jobs]

        system = _read("title.txt") + (
            "\n\n=== CANDIDATE INSTRUCTIONS ===\n"
            f"{(instructions or '').strip() or '(none)'}\n"
        ) + self._cv_block() + (
            "\nYou will be given several jobs at once, numbered. Judge each one "
            "independently and answer for every number you were given.\n"
            'Reply with JSON: {"results": [{"n": 1, "plausible": true, '
            '"score": 0-100, "reason": "a few words"}, ...]}\n'
        )
        listing = "\n".join(
            f"{i}. Title: {j.get('title')} | Company: {j.get('company')} "
            f"| Location: {j.get('location')}"
            for i, j in enumerate(jobs, start=1)
        )
        user = "=== JOBS (title/metadata only) ===\n" + listing

        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                temperature=0.0,
                max_tokens=_TITLE_MAX_TOKENS * len(jobs) + self.reserve,
                extra_body=self.no_reasoning or None,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            await usage.record("title", self.model, getattr(resp, "usage", None))
            data = json.loads(_require_content(resp) or "{}")
        except Exception as exc:  # noqa: BLE001 - never drop jobs on an API error
            logger.warning("Batch title screen failed (%d jobs): %s", len(jobs), exc)
            return [TitleResult(True, 0, "screen error") for _ in jobs]

        by_number: dict[int, dict[str, Any]] = {}
        for item in data.get("results") or []:
            try:
                by_number[int(item.get("n"))] = item
            except (TypeError, ValueError):
                continue
        missing = len(jobs) - len(by_number)
        if missing > 0:
            logger.info("Batch title screen: %d of %d verdicts missing, letting through",
                        missing, len(jobs))

        results = []
        for i in range(1, len(jobs) + 1):
            item = by_number.get(i)
            if item is None:
                results.append(TitleResult(True, 0, "no verdict returned"))
                continue
            results.append(TitleResult(
                plausible=bool(item.get("plausible", True)),
                score=_clamp_score(item.get("score", 0)),
                reason=str(item.get("reason") or "").strip() or "no reason given",
            ))
        return results

    async def quick_score(self, job: dict[str, Any], instructions: str) -> QuickResult:
        if self._client is None:
            return QuickResult(0, f"Scoring skipped: {self.init_error or 'OpenAI unavailable.'}")

        # Language gate BEFORE the API call: a posting written in a language the
        # candidate never listed is a hard miss, and there is nothing to pay a
        # model to find out. Detected in code because models don't self-enforce it.
        foreign = language.violates(job.get("description"), instructions)
        if foreign:
            return QuickResult(
                score=15,
                summary=f"Job description is written in {foreign}, not a language you listed.",
                red_flags=[f"description language: {foreign}"],
            )
        # The advert may be in English yet DEMAND a working language you lack.
        # A hard miss the model does not treat as a blocker, so it is gated here
        # too - no point paying to score a role you cannot be hired into.
        required = language.required_language_missing(job.get("description"), instructions)
        if required:
            return QuickResult(
                score=15,
                summary=f"The role requires {required}, which you did not list as a language you speak.",
                red_flags=[f"requires {required}"],
            )

        # Same reason as the title screen: a stable system prefix is cacheable,
        # a user message that changes per job is not. This lifts the cached
        # prefix from 1709 tokens to 2575 - the instructions stop being billed
        # at full rate on every job.
        system = _read("quick.txt") + (
            "\n\n=== CANDIDATE INSTRUCTIONS (preferences, must-haves, hard excludes) ===\n"
            f"{(instructions or '').strip() or '(none)'}\n"
        ) + self._cv_block()
        user = _vacancy_block(job, max_chars=_QUICK_DESC_CHARS)
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                temperature=0.2,
                max_tokens=_QUICK_MAX_TOKENS + self.reserve,
                extra_body=self.no_reasoning or None,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            await usage.record("quick", self.model, getattr(resp, "usage", None))
            data = json.loads(_require_content(resp) or "{}")
        except json.JSONDecodeError:
            return QuickResult(0, "Could not parse model response.",
                               ["parse_error"], failed=True)
        except Exception as exc:  # noqa: BLE001
            logger.error("Quick score failed for job %s: %s", job.get("id"), exc)
            return QuickResult(0, "Scoring failed (API error).",
                               ["scoring_error"], failed=True)

        # An answer with no score in it is not a low score, it is no answer.
        # This is the exact shape the 854 lost jobs arrived in: valid JSON,
        # empty object, stored as a permanent zero with "No summary.".
        if not isinstance(data, dict) or data.get("score") is None:
            logger.warning("Quick score for job %s came back without a score: %.120s",
                           job.get("id"), data)
            return QuickResult(0, "The model returned no score.",
                               ["empty_response"], failed=True)

        # Deterministic enforcement: the model judges, the code decides. It
        # returns an honest fit score plus a blocker flag; _apply_blocker caps
        # the score and throws out seniority claims the ad doesn't support.
        score = _apply_blocker(_clamp_score(data.get("score", 0)), data, job, instructions)
        red_flags = _as_list(data.get("red_flags"))
        # The model is told not to mark the years down itself, so its summary
        # may read as a clean fit. Say why the number is capped.
        short = seniority.years_shortfall(job)
        if short:
            red_flags = [f"asks {short[0]}+ years; this search is set to {short[1]}"] + red_flags
        return QuickResult(
            score=score,
            summary=str(data.get("summary", "")).strip() or "No summary.",
            red_flags=red_flags,
        )

    # ------------------------------------------------------------------ #
    # Stage 2 - advanced analysis (resume + case bank + instructions)    #
    # ------------------------------------------------------------------ #
    async def _advanced_call(self, prompt: str, job: dict[str, Any],
                             profile: dict[str, Any], max_tokens: int) -> dict[str, Any]:
        """One advanced-stage model call. Raises on transport/parse failure so
        each public method can shape its own AdvancedResult around it."""
        resp = await self._client.chat.completions.create(
            model=self.advanced_model,
            temperature=0.3,
            max_tokens=max_tokens + self.reserve,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _read(prompt)},
                {"role": "user", "content": _advanced_user(job, profile)},
            ],
        )
        await usage.record("advanced", self.advanced_model, getattr(resp, "usage", None))
        return json.loads(_require_content(resp) or "{}")

    async def advanced(self, job: dict[str, Any], profile: dict[str, Any]) -> AdvancedResult:
        """The whole deep check in one call - verdict AND coverage. Kept for the
        no-JS fallback route; the page normally runs the two halves separately
        (advanced_verdict + advanced_coverage) so the verdict paints first."""
        if self._client is None:
            return AdvancedResult(error=self.init_error or "OpenAI unavailable.")
        try:
            data = await self._advanced_call("advanced.txt", job, profile,
                                             _ADVANCED_MAX_TOKENS)
        except json.JSONDecodeError:
            return AdvancedResult(error="Could not parse model response.")
        except Exception as exc:  # noqa: BLE001
            logger.error("Advanced check failed for job %s: %s", job.get("id"), exc)
            return AdvancedResult(error=f"Analysis failed: {exc}")
        score, strengths, weaknesses = _verdict_from_data(data, job, profile)
        findings = data.get("coverage")
        findings = [f for f in findings if isinstance(f, dict)] if isinstance(findings, list) else []
        return AdvancedResult(strengths=strengths, weaknesses=weaknesses,
                              score=score, coverage=findings)

    async def advanced_verdict(self, job: dict[str, Any],
                               profile: dict[str, Any]) -> AdvancedResult:
        """Half one: score + pros/cons, no coverage. Small answer, fast to land,
        so the page can show the verdict while the citations are still coming."""
        if self._client is None:
            return AdvancedResult(error=self.init_error or "OpenAI unavailable.")
        try:
            data = await self._advanced_call("advanced_verdict.txt", job, profile,
                                             _VERDICT_MAX_TOKENS)
        except json.JSONDecodeError:
            return AdvancedResult(error="Could not parse model response.")
        except Exception as exc:  # noqa: BLE001
            logger.error("Advanced verdict failed for job %s: %s", job.get("id"), exc)
            return AdvancedResult(error=f"Analysis failed: {exc}")
        score, strengths, weaknesses = _verdict_from_data(data, job, profile)
        return AdvancedResult(strengths=strengths, weaknesses=weaknesses, score=score)

    async def advanced_coverage(self, job: dict[str, Any],
                                profile: dict[str, Any]) -> AdvancedResult:
        """Half two: the advert's demands cited one by one. Only `coverage` is
        populated; the caller verifies each quote against the real advert."""
        if self._client is None:
            return AdvancedResult(error=self.init_error or "OpenAI unavailable.")
        try:
            data = await self._advanced_call("advanced_coverage.txt", job, profile,
                                             _COVERAGE_MAX_TOKENS)
        except json.JSONDecodeError:
            return AdvancedResult(error="Could not parse model response.")
        except Exception as exc:  # noqa: BLE001
            logger.error("Advanced coverage failed for job %s: %s", job.get("id"), exc)
            return AdvancedResult(error=f"Analysis failed: {exc}")
        findings = data.get("coverage")
        findings = [f for f in findings if isinstance(f, dict)] if isinstance(findings, list) else []
        return AdvancedResult(coverage=findings)


def _advanced_user(job: dict[str, Any], profile: dict[str, Any]) -> str:
    """The shared context for every advanced-stage call: the candidate's record
    and the vacancy. Byte-identical between the verdict and coverage calls, so
    the provider caches the prefix and the second call pays little for it."""
    return (
        "=== CANDIDATE RESUME ===\n"
        f"{(profile.get('resume_text') or '').strip() or '(empty)'}\n\n"
        "=== CASE BANK (project stories) ===\n"
        f"{(profile.get('case_bank') or '').strip() or '(none)'}\n\n"
        "=== INSTRUCTIONS ===\n"
        f"{(profile.get('instructions') or '').strip() or '(none)'}\n\n"
        + _vacancy_block(job)
    )


def _verdict_from_data(data: dict[str, Any], job: dict[str, Any],
                       profile: dict[str, Any]) -> tuple[int | None, list[str], list[str]]:
    """Turn a model verdict into (score, strengths, weaknesses) with every
    deterministic gate applied - the model judges, the code decides. Shared by
    the combined `advanced()` and the split `advanced_verdict()` so a blocker is
    enforced identically no matter which path produced the verdict."""
    raw_score = data.get("score")
    score = _clamp_score(raw_score) if raw_score is not None else None
    if score is not None:
        score = _apply_blocker(score, data, job,   # verified hard-blocker cap
                               profile.get("instructions"))
    weaknesses = _as_list(data.get("weaknesses"))[:4]
    # Same language gate as the quick pass - enforced here too, since the
    # deep check can be run by hand on any job.
    instructions = profile.get("instructions")
    foreign = language.violates(job.get("description"), instructions)
    if foreign:
        weaknesses = [f"The job is advertised in {foreign} — you asked for "
                      f"roles in the language(s) you listed."] + weaknesses[:3]
        score = min(score if score is not None else 25, 25)

    # And the other half of the language gate: an English advert that DEMANDS a
    # working language the candidate does not have. The model is told to score
    # role fit "as if every constraint were satisfied" and only writes-in-a-
    # foreign-language counts as its language blocker, so a required-but-not-
    # written language slips past it entirely - 85% for a role needing Hungarian.
    # Caught in code, like the rest: the model judges, the code decides.
    required = language.required_language_missing(job.get("description"), instructions)
    if required:
        weaknesses = [f"The advert requires {required} as a working language, "
                      f"which you did not list."] + weaknesses[:3]
        score = min(score if score is not None else 25, 25)

    # The deep check is where a job is looked at seriously, so a constraint
    # that decides the answer has to be SAID here, not just applied to the
    # number. A score quietly capped with no gap explaining it reads as the
    # model being wrong.
    countries = residency.blocking_countries(job, instructions)
    if countries:
        weaknesses = [
            f"The posting ties this role to {residency.as_text(countries)}. "
            f"You would have to live there."
        ] + weaknesses[:3]

    office = workplace.requires_office(job.get("description"))
    where = residency.country_of(job.get("location"))
    home = residency.home_countries(instructions)
    if office and where and home and where not in home:
        weaknesses = [
            f'The body states "{office}" and the role sits in {where}, '
            f"so it is not remote for you."
        ] + weaknesses[:3]

    short = seniority.years_shortfall(job)
    if short:
        weaknesses = [
            f"The advert asks for {short[0]}+ years of experience; the search "
            f"that found it is set to {short[1]}."
        ] + weaknesses[:3]

    return score, _as_list(data.get("strengths"))[:4], weaknesses


def _clamp_score(raw: Any) -> int:
    try:
        return max(0, min(100, int(raw)))
    except (TypeError, ValueError):
        return 0


def _is_blocker(data: dict[str, Any]) -> bool:
    """True if the model marked a hard-constraint violation (structured flag)."""
    v = data.get("blocker")
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return False


def _apply_blocker(score: int, data: dict[str, Any], job: dict[str, Any],
                   instructions: str | None = None) -> int:
    """Cap the score when the model reports a hard blocker.

    There is deliberately NO second-guessing of the model's call here. An
    earlier version verified "seniority" claims in code, on the belief that the
    model had invented a years ceiling - it had not: the ceiling was written in
    the user's own AI Instructions, and the model was applying it, just bluntly.
    Personal calibration belongs in those instructions, and a capable fast model
    (gpt-4.1-mini; 4o-mini is not one - see the notebook) reads their tiers
    correctly on its own. The one gate still enforced in code is LANGUAGE, and
    only because it is checked against objective evidence rather than a claim.

    Years of experience joined it on 26 Oct 2026 for the same reason, and only
    when the search states a figure: both sides are then numbers someone wrote
    down - the advert's requirement and the candidate's own years - so the code
    compares them rather than checking the model's account of the CV."""
    # Enforced in code: the candidate listed levels they will not take, and the
    # title says one of them outright. Models kept scoring these 65-75 despite
    # the instruction, so the title check does not depend on the model agreeing.
    level = seniority.blocking_level(job, instructions)
    if level:
        logger.info("Seniority block: title says %s on %.60s",
                    level, job.get("title") or "")
        return min(score, 25)
    # Residency is a SOFT cap, unlike language and seniority. "Remote, but you
    # must live in Switzerland" is not wrong for the candidate the way a Senior
    # title or a German-only workplace is - it is a job in Europe that costs a
    # move. Capping it keeps it visible and off the top of the review list; the
    # decision to relocate is not one the code should make.
    countries = residency.blocking_countries(job, instructions)
    if countries:
        logger.info("Residency cap: %s required on %.60s",
                    residency.as_text(countries), job.get("title") or "")
        score = min(score, _RESIDENCY_CAP)
    # An office requirement buried in the body is a residency requirement in
    # disguise: two days a week in Birmingham means living near Birmingham.
    # The card said "United Kingdom (Remote)" and the metadata gate believed it.
    office = workplace.requires_office(job.get("description"))
    if office:
        where = residency.country_of(job.get("location"))
        home = residency.home_countries(instructions)
        if where and home and where not in home:
            logger.info("Office cap: \"%s\" in %s on %.60s",
                        office, where, job.get("title") or "")
            score = min(score, _RESIDENCY_CAP)
    # More years asked than the search says the candidate has, beyond the margin
    # they allowed. Soft, like residency: lowered, never hidden.
    short = seniority.years_shortfall(job)
    if short:
        logger.info("Years cap: asks %d+, search set to %d on %.60s",
                    short[0], short[1], job.get("title") or "")
        score = min(score, _YEARS_CAP)
    if not _is_blocker(data):
        return score
    reason = str(data.get("blocker_reason") or "unspecified").strip().lower()
    # No instructions, no rules, nothing to break. With the CV in the prompt
    # the model knows where the candidate lives and what they speak, and it
    # turned those facts into blockers despite being told not to - measured 28
    # Oct 2026 on 22 real adverts, an on-site Berlin role it called "a strong
    # technical and leadership fit" came back capped at 25. The model's score is
    # already "as if every constraint were met", so it stands.
    if not (instructions or "").strip():
        logger.info("Blocker (%s) on %.60s ignored — the instructions state no rules",
                    reason, job.get("title") or "")
        return score
    logger.info("Blocker (%s) on %.60s — score %d capped",
                reason, job.get("title") or "", score)
    return min(score, 25)


def _as_list(val: Any) -> list[str]:
    if isinstance(val, str):
        val = [val]
    if not isinstance(val, list):
        return []
    return [str(x).strip() for x in val if x]

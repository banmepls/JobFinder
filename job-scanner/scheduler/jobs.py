"""Processing pipeline + server-side scheduler.

Primary source is now the PUBLIC LinkedIn guest endpoint (no login, no account):
a scheduled cycle fetches public jobs, stores new ones, scores them and sends a
Telegram digest. The same process_new_jobs() is also invoked by /api/ingest, so
the optional Chrome extension can still feed data if desired.

A module-level asyncio.Lock ensures overlapping runs don't double-score.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import providers
from config import settings
from db import queries
from matcher import AIMatcher
from scheduler import screening
from scrapers.ats import iter_ats_boards
from scrapers.national import iter_arbeitsagentur, iter_platsbanken

if TYPE_CHECKING:
    from scrapers.base import JobListing
from scrapers.api_sources import (
    iter_arbeitnow,
    iter_himalayas,
    iter_jobicy,
    iter_remoteok,
    iter_remotive,
    iter_weworkremotely,
    iter_workable,
)
from scrapers.linkedin_public import (
    _with_workplace,
    fetch_job_detail,
    fetch_job_detail_full,
    iter_public_cards,
    workplace_allowed,
)
from telegram_bot import send_digest

# source name -> async generator yielding JobListing for a search config.
SOURCE_FETCHERS = {
    "linkedin": iter_public_cards,   # HTML cards (description fetched per job)
    "remotive": iter_remotive,       # public JSON API, full description included
    "remoteok": iter_remoteok,
    "arbeitnow": iter_arbeitnow,     # EU / Germany
    "jobicy": iter_jobicy,           # remote, EU geo filter
    "himalayas": iter_himalayas,     # remote
    "workable": iter_workable,       # ATS boards (company career pages), any location
    "weworkremotely": iter_weworkremotely,   # remote, RSS
    # Company career pages read straight from the ATS that hosts them - the
    # jobs that never reach LinkedIn. Seeded with European companies.
    "eu_boards": iter_ats_boards,
    # State employment services - the half of the market that never reaches an
    # English aggregator. Mostly local-language; the language gate does the rest.
    "arbeitsagentur": iter_arbeitsagentur,   # Germany
    "platsbanken": iter_platsbanken,         # Sweden
}

# Human labels for the UI (search form checkboxes, etc.).
SOURCE_LABELS = {
    "linkedin": "LinkedIn",
    "remotive": "Remotive",
    "remoteok": "RemoteOK",
    "arbeitnow": "Arbeitnow (EU)",
    "jobicy": "Jobicy (EU)",
    "himalayas": "Himalayas",
    "workable": "Workable (company boards)",
    "weworkremotely": "WeWorkRemotely",
    "eu_boards": "European company boards",
    "arbeitsagentur": "Arbeitsagentur (DE)",
    "platsbanken": "Platsbanken (SE)",
}

logger = logging.getLogger(__name__)

_pipeline_lock = asyncio.Lock()

# Shortest recency window applied to the non-LinkedIn API sources (7 days).
# See the call site in run_scan_cycle for why a tighter window kills them.
_API_MIN_WINDOW = 7 * 86400

# How many matches must pile up before a Telegram digest goes out. The
# extension posts a batch every few seconds and each batch triggers this
# pipeline, so without a floor the phone gets one message per job.
MIN_DIGEST_JOBS = 5

# Score a job needs to reach the phone. Higher than the app's own review
# threshold on purpose: the app is where you look through everything worth a
# glance, the digest is only for the handful worth interrupting you. At the
# current rate (four or five a day) that lands at roughly one digest a day.
DIGEST_MIN_SCORE = 80

# --- Scan state so the UI can show progress and stop it instantly ---------- #
_scan_running = False
_scan_stop = asyncio.Event()
_scan_task: "asyncio.Task | None" = None
_scan_done = 0
_scan_total = 0
# Summary of the most recent finished scan, so the UI can explain the outcome.
_last_scan: dict | None = None



def _ai(app_settings: dict) -> dict:
    """Key, endpoint and thinking room for whichever provider is configured."""
    provider, key = providers.credentials(app_settings)
    return {"api_key": key, "base_url": provider.base_url,
            "reasoning_reserve": provider.reasoning_reserve,
            "timeout": provider.timeout,
            "no_reasoning": providers.no_reasoning(app_settings)}


async def scan_cv() -> str:
    """The CV as every scanning call reads it, '' when none was uploaded.

    One helper for every place a scanning matcher is built, so the scan, the
    extension's path, the repair script and /api/diag cannot disagree about
    whether the candidate has a CV."""
    return (await queries.get_profile_full()).get("resume_text") or ""


def _parse_posted(raw) -> "datetime | None":
    """Best-effort parse of a job's posted date from the API sources.
    Handles unix timestamps, ISO 8601 (with 'Z'), and a few common formats."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Unix timestamp (seconds).
    if s.isdigit():
        try:
            return datetime.fromtimestamp(int(s), tz=timezone.utc)
        except (ValueError, OSError):
            return None
    iso = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None




def _is_transient(exc: Exception) -> bool:
    """Whether a failed call is worth retrying later rather than recording.

    A rate limit or a timeout says nothing about the job. Writing a score of 0
    for it hides a real match behind a number the model never produced, and the
    job never comes back - its status has moved on."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(w in text for w in
               ("rate limit", "ratelimit", "429", "timeout", "timed out",
                "connection", "temporarily", "503", "502", "overloaded"))


def _too_old(posted_at, tpr_seconds: int) -> bool:
    """True if the job was posted more than tpr_seconds ago. Unknown/unparseable
    dates are kept (return False) so we never drop a job just because we can't
    read its date."""
    dt = _parse_posted(posted_at)
    if dt is None:
        return False
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age > tpr_seconds


def is_scanning() -> bool:
    return _scan_running


def get_scan_progress() -> tuple[int, int]:
    """(cards processed so far, expected total) for the current scan."""
    return _scan_done, _scan_total


def get_last_scan() -> dict | None:
    """Outcome of the last finished scan: {added, searches, ai_error, error, stopped}."""
    return _last_scan


def request_stop() -> None:
    """Stop a running scan immediately. Sets the flag AND cancels the task so
    even a hung network / OpenAI call is interrupted right away. Already-found
    jobs are kept untouched."""
    _scan_stop.set()
    if _scan_task is not None and not _scan_task.done():
        _scan_task.cancel()
        logger.info("Stop requested — cancelling the running scan task.")


def split_keywords(raw: "str | None") -> list[str]:
    """A search may list SEVERAL role phrases (one per line, or comma-separated),
    e.g. "Product Manager\\nAssociate PM\\nProduct Operations". Returns the distinct
    non-empty phrases, or [""] to run the source's own default query."""
    if not raw or not raw.strip():
        return [""]
    seen: set[str] = set()
    out: list[str] = []
    for p in re.split(r"[\n,]", raw):
        p = p.strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out or [""]


async def _fetch_all_keywords(fetcher, search):
    """Run one source's fetcher once PER keyword phrase and stream all cards, so a
    single saved search can cover many role titles. Sequential + paced (never
    parallel) to stay gentle on the source."""
    keywords = split_keywords(search.get("keywords"))
    for i, kw in enumerate(keywords):
        if _scan_stop.is_set():
            return
        cfg = {**search, "keywords": kw} if kw else search
        async for card in fetcher(cfg):
            yield card
        if i < len(keywords) - 1:
            await asyncio.sleep(random.uniform(0.6, 1.4))  # polite gap between queries


async def notify_scored(scheduled: bool = False) -> int:
    """Telegram digest for freshly scored jobs. Returns how many were sent.

    Two triggers, deliberately different:

    * **Scheduled** (`scheduled=True`) - fires after a scan the user planned in
      Settings, and sends even when there is nothing. An empty digest is
      information: it says the scan ran and found nothing, which is otherwise
      indistinguishable from a scan that never happened.
    * **Opportunistic** - the extension posts a batch every few seconds and each
      batch reaches this function. Those only send once `MIN_DIGEST_JOBS`
      matches have piled up; without the floor the phone got five messages in
      three minutes, one job each.

    Either way only jobs at or above `DIGEST_MIN_SCORE` go out. Everything below
    leaves the queue immediately, or every later digest reconsiders it forever.
    """
    await queries.mark_below_threshold_notified(DIGEST_MIN_SCORE)
    to_notify = await queries.get_jobs_to_notify(DIGEST_MIN_SCORE)

    if not scheduled and len(to_notify) < MIN_DIGEST_JOBS:
        if to_notify:
            logger.info("Digest held: %d match(es), waiting for %d",
                        len(to_notify), MIN_DIGEST_JOBS)
        return 0

    if not await send_digest(to_notify, allow_empty=scheduled):
        return 0                # keep the queue so the next cycle retries
    if to_notify:
        await queries.mark_notified([j["id"] for j in to_notify])
    return len(to_notify)


async def run_scan_cycle() -> None:
    """Streaming cycle: for every enabled search, fetch jobs ONE AT A TIME and
    store + quick-score each immediately, so results appear progressively.

    Cancellable: a stop request halts cleanly after the current job without
    touching jobs already found. Only one scan runs at a time.
    """
    global _scan_running, _scan_task, _scan_done, _scan_total, _last_scan
    if _scan_running:
        logger.info("Scan already running — ignoring duplicate trigger.")
        return
    _scan_running = True
    _scan_stop.clear()
    _scan_task = asyncio.current_task()
    _scan_done = 0
    _scan_total = 0
    logger.info("=== scan cycle start ===")

    processed = 0
    n_searches = 0
    ai_error: str | None = None
    top_error: str | None = None
    try:
        searches = await queries.list_enabled_searches()
        n_searches = len(searches)
        # Each search runs once PER source (CSV), and _scan_done counts cards from
        # every source - so the denominator must multiply by the source count,
        # otherwise progress can exceed 100%.
        def _n_sources(s: dict) -> int:
            return max(1, len([x for x in (s.get("source") or "linkedin").split(",") if x.strip()]))
        _scan_total = sum(int(s.get("max_jobs") or 0) * _n_sources(s) * len(split_keywords(s.get("keywords")))
                          for s in searches)
        if not searches:
            logger.info("No saved searches — nothing to fetch. Add one at /search.")

        app_settings = await queries.get_app_settings()
        matcher = AIMatcher(model=providers.resolve_model(app_settings, "fast"),
                          cv_text=await scan_cv(), **_ai(app_settings))
        if matcher.init_error:
            ai_error = matcher.init_error
            logger.error("Matcher unavailable: %s", matcher.init_error)
        profile = await queries.get_profile()
        instructions = profile.get("instructions", "")

        # Title-screen cutoff: even if the model marks a title "plausible", a very
        # low title score also skips the description fetch.
        for search in searches:
            if _scan_stop.is_set():
                break
            # A search may target several sources (CSV). Run each in turn so one
            # saved search covers LinkedIn + Remotive + Jobicy… without duplicates.
            sources = [s.strip().lower() for s in (search.get("source") or "linkedin").split(",") if s.strip()]
            for source in sources:
              if _scan_stop.is_set():
                break
              fetcher = SOURCE_FETCHERS.get(source)
              if fetcher is None:
                logger.info("[%s] unknown source '%s' — skipping",
                            search.get("name"), source)
                continue
              allowed_wt = {p.strip() for p in (search.get("remote") or "").split(",") if p.strip()}
              # Recency window (seconds). LinkedIn filters server-side via f_TPR;
              # for the API sources we enforce it here from each job's post date.
              tpr = int(search.get("tpr_seconds") or 0)
              # …but only down to a floor. The small boards publish a handful of
              # matching roles per WEEK, so a 24h window silently zeroes them out:
              # measured on a real scan, 1014 of 1014 remotive/jobicy/remoteok
              # cards were dropped as "too old" while LinkedIn kept 52. LinkedIn's
              # index is big enough for 24h to still return plenty, hence the
              # floor applies to the API sources only.
              if source != "linkedin" and tpr:
                  tpr = max(tpr, _API_MIN_WINDOW)
              screened = full = dropped = stale = 0
              try:
                # Short timeout so a throttled source fails fast instead of
                # crawling for minutes (LinkedIn guest detail fetches can hang).
                async with httpx.AsyncClient(
                    headers={"User-Agent": settings.user_agent},
                    timeout=httpx.Timeout(10.0, connect=5.0),
                    follow_redirects=True,
                ) as client:
                    # Titles are screened in batches. The preamble of that call
                    # - the prompt plus the candidate's instructions - is 1230
                    # tokens; the title itself is 17. One call per job spent 98%
                    # of its budget re-sending the same text.
                    #
                    # Only the screen batches. Everything after it stays one job
                    # at a time, because that is the part the user watches
                    # arrive on the page.
                    pending: list[JobListing] = []

                    async def score_one(card: JobListing) -> None:
                        """Everything a plausible card still costs: description,
                        workplace filter, full score, store."""
                        nonlocal full, dropped, processed
                        is_remote = None
                        if not (card.description or "").strip():
                            try:
                                desc, is_remote, criteria = await fetch_job_detail_full(
                                    client, card.external_id)
                            except Exception as exc:  # noqa: BLE001 - slow/blocked detail
                                logger.warning("detail fetch failed (%s) — skipping card", exc)
                                dropped += 1
                                return
                            card.description = _with_workplace(desc, is_remote)
                            # The same request also carries the two facts the
                            # search results omit entirely.
                            card.employment_type = criteria.get("Employment type")
                            level = criteria.get("Seniority level")
                            if level and level.lower() not in ("not applicable", ""):
                                card.description = (
                                    f"Seniority level (stated by LinkedIn): {level}\n\n"
                                    + (card.description or ""))
                            await asyncio.sleep(random.uniform(0.2, 0.5))  # polite
                        if not workplace_allowed(card, allowed_wt, is_remote):
                            dropped += 1
                            return
                        jid = await queries.insert_job(card, search_id=search.get("id"))
                        if jid is None:
                            return
                        job = await queries.get_job(jid)
                        try:
                            result = await matcher.quick_score(job, instructions)
                            # No verdict means no score written. The job stays
                            # unscored and the next cycle tries again, which is
                            # the whole difference between a retry and a job
                            # silently buried at zero.
                            if result.failed:
                                logger.warning("Job %s left unscored: %s", jid, result.summary)
                            else:
                                await queries.save_quick_score(
                                    jid, result.score, result.summary, result.red_flags,
                                )
                        except Exception as exc:  # noqa: BLE001
                            logger.exception("Scoring job %s failed: %s", jid, exc)
                            # A rate limit is temporary, and writing 0 makes it
                            # permanent: the job leaves the queue with a score
                            # it never earned and is never looked at again.
                            # Leave it unscored so the next cycle picks it up.
                            if not _is_transient(exc):
                                await queries.save_quick_score(
                                    jid, 0, "Scoring failed — check OpenAI at /api/diag", [],
                                )
                            return
                        full += 1
                        processed += 1

                    async def flush() -> None:
                        """Screen the buffered titles, score the survivors."""
                        nonlocal pending, screened
                        if not pending:
                            return
                        batch, pending = pending, []
                        async for card, verdict, payload in screening.title_verdicts(
                                batch, matcher, instructions):
                            if _scan_stop.is_set():
                                return
                            if verdict == screening.KEEP:
                                await score_one(card)
                                continue
                            # Store with the cheap score; DON'T read the
                            # description or run the full score.
                            reason = (payload if verdict == screening.REJECT
                                      else payload.reason)
                            cheap = 25 if verdict == screening.REJECT else min(payload.score, 39)
                            jid = await queries.insert_job(card, search_id=search.get("id"))
                            if jid is not None:
                                await queries.save_quick_score(
                                    jid, cheap, f"Screened out on title: {reason}", [])
                            screened += 1

                    async for card in _fetch_all_keywords(fetcher, search):
                        if _scan_stop.is_set():
                            break
                        if not card.is_valid():
                            continue
                        _scan_done += 1
                        logger.info("[%s/%s] %d/%d · %.70s",
                                    search.get("name"), source, _scan_done, _scan_total,
                                    card.title or "")
                        # 0) Recency: drop jobs older than the search's window
                        #    (skip for LinkedIn - already filtered, dates relative).
                        if tpr and source != "linkedin" and _too_old(card.posted_at, tpr):
                            stale += 1
                            continue
                        # 1) Dedup FIRST - never spend tokens on jobs we already
                        #    have, whether by id or as a re-post/per-city clone.
                        if await queries.job_exists(card.source, card.external_id):
                            continue
                        if await queries.duplicate_exists(card.title, card.company):
                            continue

                        # 2) Buffer. The free gates and the batched screen both
                        #    live in scheduler.screening, so this path and the
                        #    extension path cannot drift apart again.
                        pending.append(card)
                        if len(pending) >= matcher.BATCH_SIZE:
                            await flush()

                    # Whatever is left when the source runs dry.
                    if not _scan_stop.is_set():
                        await flush()
                logger.info(
                    "[%s/%s] title-screened %d, fully scored %d, dropped %d (workplace), "
                    "%d (older than %dd)",
                    search.get("name"), source, screened, full, dropped, stale,
                    round(tpr / 86400, 1) if tpr else 0,
                )
              except Exception as exc:  # noqa: BLE001 - one bad source must not stop others
                logger.exception("search %s (%s) failed: %s", search.get("name"), source, exc)

        # The scheduled digest: goes out even when empty, so a quiet day looks
        # different from a scan that silently failed to run.
        if not _scan_stop.is_set():
            await notify_scored(scheduled=True)
    except asyncio.CancelledError:
        logger.info("Scan cancelled by user.")
    except Exception as exc:  # noqa: BLE001
        top_error = str(exc)
        logger.exception("Scan cycle failed: %s", exc)
    finally:
        stopped = _scan_stop.is_set()
        _last_scan = {
            "added": processed,
            "searches": n_searches,
            "ai_error": ai_error,
            "error": top_error,
            "stopped": stopped,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        _scan_running = False
        _scan_stop.clear()
        _scan_task = None
        logger.info("=== scan cycle %s (%d jobs processed) ===",
                    "stopped" if stopped else "done", processed)


def local_timezone_name() -> str:
    """Human-readable name of the timezone the schedule runs in (the machine's
    local zone), shown in Settings so 'HH:MM' isn't ambiguous."""
    try:
        from tzlocal import get_localzone
        tz = get_localzone()
        return getattr(tz, "key", None) or str(tz)
    except Exception:  # noqa: BLE001
        return "your computer's local time"


def create_scheduler() -> AsyncIOScheduler | None:
    """Create the APScheduler. The actual schedule (specific times or an interval)
    comes from the in-app Settings via configure_scan_jobs().

    Fully disabled only when JOBFINDER_NO_SCHEDULER=1 (the dev instance sets this)
    - so the packaged desktop app can honour Settings-based scan times. Runs in the
    machine's LOCAL timezone, so 'HH:MM' matches the user's clock."""
    if os.getenv("JOBFINDER_NO_SCHEDULER") == "1":
        logger.info("Scheduler disabled (JOBFINDER_NO_SCHEDULER=1)")
        return None
    tz = None
    try:
        from tzlocal import get_localzone
        tz = get_localzone()
    except Exception as exc:  # noqa: BLE001
        logger.info("Could not detect local timezone (%s) — falling back", exc)
    try:
        # APScheduler resolves the local zone ITSELF when none is given, so the
        # fallback above is not enough: on Windows without the `tzdata` package
        # that second lookup raised ZoneInfoNotFoundError and killed app startup
        # (real report: 'No time zone found with key Europe/Kiev'). Any failure
        # here must degrade to UTC, never take the app down - a schedule in the
        # wrong zone is a nuisance, no app at all is a brick.
        sched = AsyncIOScheduler(timezone=tz) if tz is not None else AsyncIOScheduler()
        logger.info("Scheduler created (timezone: %s)", tz or "system default")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Timezone lookup failed (%s) — scheduling in UTC. "
                       "Scan times will follow UTC, not your local clock.", exc)
        try:
            import pytz
            utc = pytz.utc
        except ImportError:
            from datetime import timezone as _tz
            utc = _tz.utc
        sched = AsyncIOScheduler(timezone=utc)
    return sched


def parse_scan_times(raw: str) -> list[tuple[int, int]]:
    """Parse 'HH:MM, HH:MM' into [(hour, minute), ...]; ignores bad entries."""
    out: list[tuple[int, int]] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            h, m = part.split(":")
            h, m = int(h), int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                out.append((h, m))
        except ValueError:
            continue
    return out


_VALID_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_scan_days(raw: str) -> str:
    """Normalise a csv of weekday codes into an APScheduler day_of_week string.
    Empty / invalid -> '' meaning every day."""
    days = [d.strip().lower()[:3] for d in (raw or "").split(",") if d.strip()]
    days = [d for d in _VALID_DAYS if d in days]  # keep canonical order, dedup
    return ",".join(days)


def resolve_timezone(name: "str | None"):
    """The zone the scan times are read in, or None for the machine's clock.

    A name the machine cannot resolve must not stop the schedule being built:
    Windows ships no tz database at all without the `tzdata` package, and a zone
    saved on one machine can be unknown on another. Falling back to the local
    clock means the scan runs at a surprising hour; raising means it never runs.
    """
    name = (name or "").strip()
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unknown timezone %r (%s) — using this computer's clock",
                       name, exc)
        return None


def available_timezones() -> list[str]:
    """Zone names to offer in Settings, or [] when the machine has no database.

    An empty list is a real answer, not a failure: the page then offers only the
    computer's own clock rather than a dropdown of names it would reject."""
    try:
        import zoneinfo

        return sorted(zoneinfo.available_timezones())
    except Exception as exc:  # noqa: BLE001
        logger.info("No timezone database available (%s)", exc)
        return []


def configure_scan_jobs(scheduler, app_settings: dict) -> None:
    """(Re)build the scan schedule from Settings: specific daily times, optionally
    on specific weekdays. Called at startup and whenever Settings change.

    Fixed-interval scanning was dropped (2026-07-27) along with its Settings
    field: a schedule the user cannot see in the UI must not run in the
    background. No times set = scans happen only via "Scan now"."""
    if scheduler is None:
        return
    scheduler.remove_all_jobs()
    times = parse_scan_times(app_settings.get("scan_times", ""))
    days = parse_scan_days(app_settings.get("scan_days", ""))
    if not times:
        logger.info("No scan times set — automatic scanning is off.")
        return
    tz = resolve_timezone(app_settings.get("scan_timezone"))
    dow = days or "*"  # '*' = every day
    for h, m in times:
        scheduler.add_job(
            run_scan_cycle,
            trigger=CronTrigger(day_of_week=dow, hour=h, minute=m, timezone=tz),
            id=f"scan_at_{h:02d}{m:02d}",
            max_instances=1, coalesce=True, replace_existing=True,
            # A laptop asleep at 10:00 must still get its morning scan when it
            # wakes. Without a grace window APScheduler drops the missed run
            # silently, which on a machine that sleeps every night means most
            # scheduled scans never happen at all. Three hours matches the
            # extension's own catch-up window.
            misfire_grace_time=3 * 3600,
        )
    logger.info(
        "Scan scheduled at %s on %s (%s)",
        ", ".join(f"{h:02d}:{m:02d}" for h, m in times),
        days or "every day",
        tz or "this computer's clock",
    )


async def process_new_jobs(force: bool = False) -> None:
    """Enrich + quick-score every 'new' job, then notify. This is the pipeline
    for jobs fed by the Chrome extension via /api/ingest.

    Extension search cards have no description, so we enrich each job from the
    PUBLIC job-detail endpoint (free, no login) before scoring - same quality as
    the server-side scan. Reflects the scanning flag so the Jobs page shows
    progress and auto-refreshes while jobs are processed.
    """
    global _scan_running
    own_flag = not _scan_running
    if own_flag:
        _scan_running = True
    try:
        async with _pipeline_lock:
            new_jobs = await queries.get_new_jobs()
            if new_jobs:
                app_settings = await queries.get_app_settings()
                matcher = AIMatcher(model=providers.resolve_model(app_settings, "fast"),
                          cv_text=await scan_cv(), **_ai(app_settings))
                if matcher.init_error:
                    logger.error("Matcher unavailable: %s", matcher.init_error)
                profile = await queries.get_profile()
                instructions = profile.get("instructions", "")
                scored = 0
                async with httpx.AsyncClient(
                    headers={"User-Agent": settings.user_agent}, timeout=20.0,
                    follow_redirects=True,
                ) as client:
                    # Same screening policy as the scheduled scan, from the
                    # same module. This path carries most of the volume - the
                    # extension posts a batch every few seconds - and used to
                    # have none of it.
                    async for job, verdict, payload in screening.title_verdicts(
                            new_jobs, matcher, instructions):
                        if _scan_stop.is_set():
                            break
                        if verdict != screening.KEEP:
                            reason = (payload if verdict == screening.REJECT
                                      else payload.reason)
                            cheap = (25 if verdict == screening.REJECT
                                     else min(payload.score, 39))
                            await queries.save_quick_score(
                                job["id"], cheap, f"Screened out on title: {reason}", [])
                            scored += 1
                            continue
                        # Enrich missing description (free) for better scoring.
                        if not (job.get("description") or "").strip():
                            desc, is_remote = await fetch_job_detail(client, job["external_id"])
                            desc = _with_workplace(desc, is_remote)
                            if desc:
                                job["description"] = desc
                                await queries.update_job_description(job["id"], desc)
                        try:
                            result = await matcher.quick_score(job, instructions)
                        except Exception as exc:  # noqa: BLE001
                            logger.exception("Scoring job %s failed: %s", job.get("id"), exc)
                            continue
                        # Same rule as the scheduled path: a non-verdict is not
                        # written, so the job comes back round instead of being
                        # buried at zero.
                        if result.failed:
                            logger.warning("Job %s left unscored: %s",
                                           job.get("id"), result.summary)
                            continue
                        await queries.save_quick_score(
                            job["id"], result.score, result.summary, result.red_flags,
                        )
                        scored += 1
                logger.info("Quick-scored %d/%d ingested jobs", scored, len(new_jobs))

            await notify_scored()
    except Exception as exc:  # noqa: BLE001 - top-level guard
        logger.exception("process_new_jobs crashed: %s", exc)
    finally:
        if own_flag:
            _scan_running = False

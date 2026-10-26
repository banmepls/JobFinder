"""FastAPI application.

Run:  python main.py   (or)   uvicorn main:app --reload

Data source: a Chrome Extension (Manifest V3) reads the LinkedIn DOM in the
user's own session and POSTs vacancies to /api/ingest. The backend deduplicates,
scores via OpenAI, and notifies via Telegram. No Playwright, no login
automation, no external requests to LinkedIn.

Routes:
  GET  /                       job table, filterable by score/status/source
  GET  /profile                profile edit form
  POST /profile                save profile
  GET  /jobs/{id}              full job detail + AI breakdown
  POST /jobs/{id}/archive      hide job from main view
  POST /api/ingest             receive jobs from the extension (dedup+store+score)
  GET  /api/health             liveness probe for the extension popup

Binds to 127.0.0.1 by default (local tool, no auth - see README).
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

# How many applications per day the dashboard's Daily goal bar targets.
DAILY_APPLY_GOAL = 5

# Personalised invite notes a free LinkedIn account gets per month. Widely
# observed rather than published, so it is a guide shown to the user, not a
# limit the app enforces.
NOTE_ALLOWANCE_PER_MONTH = 5

# Sort orders the Jobs page offers, in the order they appear in the menu.
# "Recently applied" only makes sense on the Applied tab and is filtered out
# elsewhere by the route.
SORT_OPTIONS: dict[str, str] = {
    "recent": "Newest first",
    "applied": "Recently applied",
    "score": "Highest score",
    "title": "Title A-Z",
    "company": "Company A-Z",
}

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from config import settings
from db import queries
from db import usage
from db.models import init_db
from matcher import AIMatcher
import cv_content
import letter_template
import origins
import pipeline
import extension_health
import providers
from matcher import audience
from matcher import coverage
from matcher import generate as gen
from scheduler import (
    available_timezones,
    configure_scan_jobs,
    create_scheduler,
    get_last_scan,
    get_scan_progress,
    is_scanning,
    local_timezone_name,
    process_new_jobs,
    request_stop,
    resolve_timezone,
    run_scan_cycle,
)
from scrapers.base import JobListing

try:
    import markdown as _md

    def _md_to_html(text: str) -> str:
        return _md.markdown(text or "", extensions=["nl2br"])
except ImportError:  # graceful fallback if markdown isn't installed
    import html as _html

    def _md_to_html(text: str) -> str:
        return "<pre style='white-space:pre-wrap'>" + _html.escape(text or "") + "</pre>"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("job-scanner")

from paths import app_root

BASE_DIR = app_root()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# Let every template know the live scan state, so the header "Scan now" button
# renders correctly on first paint (no flash from Scan now → In progress…).
templates.env.globals["is_scanning"] = is_scanning


def _fast_model(s: dict) -> str:
    """Cheap model for scanning (title screen + quick score)."""
    return providers.resolve_model(s, "fast")


def _deep_model(s: dict) -> str:
    """Model for the advanced check - a few calls a day, each one waited for."""
    return providers.resolve_model(s, "deep")


def _write_model(s: dict) -> str:
    """Model for text a person reads: cover letters, CV edits, HR answers."""
    return providers.resolve_model(s, "write")


def _writer_client(s: dict) -> dict:
    """Key and endpoint for the three streaming helpers in generate.py, which
    build their own client and take no reasoning budget."""
    provider, key = providers.credentials(s, providers.WRITE)
    return {"api_key": key, "base_url": provider.base_url}


def _ai(s: dict, role: str = providers.SCAN) -> dict:
    """Keyword arguments every model client needs: key, endpoint, thinking room.

    One helper so a new provider never means hunting down six call sites, and
    `role` so the scanning provider and the writing provider can differ without
    any caller having to know they can."""
    provider, key = providers.credentials(s, role)
    return {"api_key": key, "base_url": provider.base_url,
            "reasoning_reserve": provider.reasoning_reserve,
            "timeout": providers.timeout_for(s, role),
            "no_reasoning": providers.no_reasoning(s, role)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Config: %s", settings.redacted())
    # Fingerprint older rows, then collapse duplicate groups that were stored
    # before content dedup existed (keeps applied/saved/pinned copies).
    filled = await queries.backfill_dedup_keys()
    # Repair titles stored twice over by an older extension build. Runs before
    # the purge so a repaired row can collapse into its correctly-read twin.
    repaired = await queries.repair_doubled_titles()
    if repaired:
        logger.info("Titles: repaired %d doubled titles", repaired)
    if filled or repaired:
        removed = await queries.purge_duplicates()
        logger.info("Dedup: fingerprinted %d jobs, repaired %d titles, removed %d duplicates",
                    filled, repaired, removed)
    # Invites that were never accepted stop being interesting long before they
    # stop existing. A 60-day window is far wider than one launch, so checking
    # at startup is often enough.
    stale = await queries.archive_stale_contacts()
    if stale:
        logger.info("Contacts: archived %d invites older than %d days",
                    stale, queries.STALE_CONTACT_DAYS)
    scheduler = create_scheduler()
    if scheduler is not None:
        scheduler.start()
        app.state.scheduler = scheduler
        # DB settings are authoritative - apply the saved schedule on startup.
        app_settings = await queries.get_app_settings()
        configure_scan_jobs(scheduler, app_settings)
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


app = FastAPI(title="Job Scanner", lifespan=lifespan)

# Who may talk to a server that has no password on it. The extension and this
# machine's own pages; nothing else. See origins.py for why the old "*" was not
# a permissive default but an open door.
@app.middleware("http")
async def refuse_foreign_origins(request: Request, call_next):
    """Enforced here as well as announced by CORS below, because CORS only asks
    the browser not to READ the answer. A cross-site form POST is sent and acted
    on regardless - /jobs/clear does not care who filled in the form."""
    origin = request.headers.get("origin")
    if not origins.allowed(origin, settings.cors_origins):
        logger.warning("Refused a request from %s to %s", origin, request.url.path)
        return JSONResponse(
            {"error": "This app only answers its own pages and its browser "
                      "extension. Nothing was changed."},
            status_code=403)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=origins.ALLOW_REGEX,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Ingest API (called by the Chrome extension)                                 #
# --------------------------------------------------------------------------- #

class IngestJob(BaseModel):
    job_id: str = Field(..., description="Stable LinkedIn data-job-id")
    title: str
    company: str | None = None
    location: str | None = None
    url: str | None = None
    posted_at: str | None = None
    description: str | None = None
    source: str = "linkedin"
    # Work arrangement chip read from the LinkedIn card ("Hybrid"/"On-site"/
    # "Remote"). It is not in the description, so without it the scorer rated
    # on-site roles as good remote matches.
    workplace: str | None = None


class IngestPayload(BaseModel):
    jobs: list[IngestJob]


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/scan-schedule")
async def scan_schedule(paused: str = "0", stopped: str = "0", error: str = "",
                        outcome: str = "", last_run: str = "", v: str = ""):
    """The one schedule, for the extension to obey - and the one place it reports back.

    The extension used to run its own alarm on its own interval, which knew
    nothing about Settings: scans fired at times the user could not see and had
    no way to change. There is one schedule now and it is the one on the
    Settings page - no times set means nothing scans automatically.

    The query string is the extension describing itself. It pauses itself after
    a run of empty scans, and that state lived only in Chrome: the app kept
    looking healthy while nothing arrived for days, with no way to tell a paused
    extension from a quiet job market. This is the only request it makes on its
    own, so it is the only channel available."""
    from scheduler import jobs as sched

    state = "stopped" if stopped == "1" else "paused" if paused == "1" else "ok"
    await queries.record_extension_state(
        state, error=error or (outcome if outcome not in ("ok", "") else ""),
        version=v, run_at=last_run)

    app_settings = await queries.get_app_settings()
    times = sched.parse_scan_times(app_settings.get("scan_times", ""))
    days = sched.parse_scan_days(app_settings.get("scan_days", ""))
    return JSONResponse({
        "times": [[h, m] for h, m in times],
        "days": [d for d in days.split(",") if d] if days else [],
        "timezone": local_timezone_name(),
    })


@app.post("/api/ingest")
async def ingest(payload: IngestPayload, background_tasks: BackgroundTasks):
    """Receive jobs from the extension, dedup by (source, job_id) and by content
    fingerprint, store new ones, then score + notify in the background."""
    received = len(payload.jobs)
    stored = 0
    for item in payload.jobs:
        # Fold the work arrangement into the location, which is where the rest
        # of the app already looks for it (matcher.detect_workplace).
        location = item.location
        wp = (item.workplace or "").strip()
        if wp and wp.lower() not in (location or "").lower():
            location = f"{location} ({wp})" if location else wp
        listing = JobListing(
            source=item.source,
            external_id=item.job_id,
            title=item.title,
            company=item.company,
            location=location,
            description=item.description,
            url=item.url,
            posted_at=item.posted_at,
        )
        if not listing.is_valid():
            continue
        if await queries.job_exists(listing.source, listing.external_id):
            continue
        if await queries.insert_job(listing, fetched_via="extension") is not None:
            stored += 1

    logger.info("Ingest: received=%d stored=%d", received, stored)

    if stored:
        # Reliable background execution (BackgroundTasks holds a reference and
        # logs exceptions - a fire-and-forget create_task can be GC'd before it
        # finishes, silently leaving jobs unscored).
        background_tasks.add_task(process_new_jobs)

    return JSONResponse({"received": received, "stored": stored})


# --------------------------------------------------------------------------- #
# Job listing + detail                                                        #
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    view: str = "strong",   # first tab: To review
    stage: str = "",        # Applied tab only: narrow to one pipeline stage
):
    """Four tabs, firehose-first: 'all' (every not-yet-applied job), 'strong'
    (the "To review" list: score >= threshold or pinned), 'saved' (favorites),
    and 'applied'. Every list is NEWEST FIRST - the score is a LABEL on each
    row, not the sort key."""
    app_settings = await queries.get_app_settings()
    threshold = int(app_settings.get("copilot_min_score", 60))
    view = view if view in ("all", "strong", "saved", "applied") else "strong"

    # Onboarding is handled by the empty state on this page (never redirect away
    # - the Jobs page must always be reachable).
    searches = await queries.list_searches()
    profile = await queries.get_profile()
    has_instructions = bool((profile.get("instructions") or "").strip())
    has_searches = bool(searches)

    # Cap rows so huge lists (1000+) render instantly - newest ones win the cap.
    LIST_LIMIT = 150
    stage = stage if pipeline.is_valid(stage) else ""
    # No sort control on the page: each tab has the one order that suits it.
    # queries.SORTS still holds the others, so bringing the menu back is a
    # template change rather than a rewrite.
    sort = "applied" if view == "applied" else "recent"
    if view == "applied":
        jobs = await queries.list_applied(stage or None, limit=LIST_LIMIT, order=sort)
    elif view == "saved":
        jobs = await queries.list_favorites(order=sort)
    elif view == "strong":
        jobs = await queries.list_strong_jobs(threshold, limit=LIST_LIMIT, order=sort)
    else:  # 'all' - the firehose: every not-yet-applied job
        jobs = await queries.list_jobs(applied=0, limit=LIST_LIMIT, order=sort)

    # Weekly/daily applied stats for the dashboard band. Week starts Monday.
    now_local = datetime.now().astimezone()
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    counts = {
        "all": await queries.count_jobs(applied=0),
        "strong": await queries.count_strong_jobs(threshold),
        "saved": len(await queries.list_favorites()),
        "applied": await queries.count_jobs(applied=1),
        "applied_week": await queries.count_applied_since(
            week_start.astimezone(timezone.utc).isoformat()),
        "applied_today": await queries.count_applied_since(
            day_start.astimezone(timezone.utc).isoformat()),
        "goal": int(app_settings.get("daily_apply_goal", DAILY_APPLY_GOAL) or DAILY_APPLY_GOAL),
        "match80": await queries.count_jobs(min_score=80, scored_only=True, applied=0),
    }
    # Stage counts drive the filter chips. Built for every stage, including the
    # empty ones, so the row does not reshuffle as applications move.
    by_stage = await queries.count_by_stage()
    stage_counts = [(s, pipeline.LABELS[s], by_stage.get(s, 0)) for s in pipeline.STAGES]

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "active": "jobs",
            "jobs": jobs,
            "view": view,
            "stage": stage,
            "stage_counts": stage_counts,
            "stage_labels": pipeline.LABELS,
            "threshold": threshold,
            "counts": counts,
            "list_limit": LIST_LIMIT,
            "has_instructions": has_instructions,
            "has_searches": has_searches,
            "scanning": is_scanning(),
            "scan_progress": get_scan_progress(),
            # None unless the extension has something wrong to report. It
            # supplies 56% of all jobs and used to fail in complete silence.
            "extension": extension_health.status(await queries.get_app_settings()),
            # Human names for the per-row source badge.
            "source_labels": {v: n for v, n, _ in SEARCH_SOURCE_OPTIONS},
        },
    )


def _clean_description(text: str | None) -> str:
    """Tidy scraped descriptions for display:
    - join a bare label line with the ': value' line that follows it
      ("Role\\n: Project Manager" -> "Role: Project Manager");
    - drop leftover "Show more"/"Show less" UI text from the source;
    - collapse runs of blank lines.
    """
    if not text:
        return ""
    raw = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    lines: list[str] = []
    for ln in raw:
        s = ln.strip()
        if s.lower() in ("show more", "show less", "see more", "see less"):
            continue
        # "Label" then ": value" on the next line -> merge.
        if s.startswith(":") and lines and lines[-1].strip() and not lines[-1].rstrip().endswith(":"):
            lines[-1] = lines[-1].rstrip() + s
            continue
        lines.append(ln)
    out: list[str] = []
    for ln in lines:
        if not ln.strip() and out and not out[-1].strip():
            continue  # collapse consecutive blanks
        out.append(ln)
    return "\n".join(out).strip()


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: int):
    job = await queries.get_job(job_id)
    if job is None:
        return HTMLResponse("Job not found", status_code=404)
    job["description"] = _clean_description(job.get("description"))
    if job.get("status") in ("scored", "notified"):
        await queries.set_job_status(job_id, "viewed")
        job["status"] = "viewed"

    app_settings = await queries.get_app_settings()
    threshold = int(app_settings.get("copilot_min_score", 60))
    score = job.get("score") or 0
    raw_assets = await queries.get_assets(job_id)
    contacts = await queries.list_contacts(job_id)
    notes_spent = await queries.notes_spent_this_month()
    # Markdown assets → HTML; cv_adjustment is structured JSON (per-bullet edits).
    import json as _json
    assets: dict[str, str] = {}
    cv_edits = None
    cl_brief = raw_assets.pop(CL_BRIEF_KIND, "")
    cl_address = raw_assets.pop(CL_ADDRESS_KIND, "")
    # The advert's demands, marked where they sit in it. Rebuilt from the stored
    # quotes rather than the stored offsets, so a description that was
    # re-fetched since the check cannot shift the colours onto the wrong words.
    marks = coverage.Coverage()
    stored = raw_assets.pop(COVERAGE_KIND, "")
    if stored:
        try:
            marks = coverage.build(job.get("description") or "",
                                   _json.loads(stored),
                                   [c.get("title") or "" for c in await queries.list_cases()])
        except (ValueError, TypeError):
            logger.warning("Stored coverage for job %s is unreadable", job_id)
    for kind, content in raw_assets.items():
        if kind == "cv_adjustment":
            try:
                cv_edits = _json.loads(content)
            except (ValueError, TypeError):
                cv_edits = None
        else:
            assets[kind] = _md_to_html(content)

    # Two full CV documents for the CV-adjustment tab: the pristine "before"
    # baseline and the "after" = baseline with all suggested edits applied.
    # Only when the user has actually uploaded a CV (no built-in default).
    profile = await queries.get_profile()
    working = (profile.get("cv_html") or "").strip()
    has_cv = bool(working or (profile.get("cv_original_html") or "").strip())
    cv_original = await queries.ensure_cv_original(working) if has_cv else ""
    cv_tailored = cv_original
    if has_cv and cv_edits and cv_edits.get("edits"):
        for e in cv_edits["edits"]:
            new_html, ok = _apply_cv_edit(cv_tailored, e.get("before", ""), e.get("after", ""))
            if ok:
                cv_tailored = new_html

    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "active": "jobs",
            "job": job,
            "assets": assets,
            "cv_edits": cv_edits,
            "has_cv": has_cv,
            "cv_original_html": cv_original,
            "cv_tailored_html": cv_tailored,
            "coverage": marks,
            "description_html": coverage.render(job.get("description") or "", marks),
            "cv_doc_css": cv_content.CV_DOC_CSS,
            "cl_brief": cl_brief,
            "cl_address": cl_address,
            "asset_kinds": gen.KINDS,
            "asset_labels": gen.KIND_LABELS,
            "copilot_available": score >= threshold or bool(job.get("pinned")),
            "copilot_threshold": threshold,
            "contacts": contacts,
            "notes_left": max(0, NOTE_ALLOWANCE_PER_MONTH - notes_spent),
            "note_allowance": NOTE_ALLOWANCE_PER_MONTH,
            "audience_labels": audience.AUDIENCE_LABELS,
            "stage_labels": pipeline.LABELS,
            "route_labels": audience.ROUTE_LABELS,
        },
    )


async def _advanced_prep(job_id: int):
    """Shared gate + client for every advanced-check route.

    Returns (job, profile, matcher). `job is None` → not found; `matcher is None`
    (with job set) → refused because the score is below the threshold and the job
    was neither pinned nor already checked. Otherwise the check may run."""
    job = await queries.get_job(job_id)
    if job is None:
        return None, None, None
    app_settings = await queries.get_app_settings()
    threshold = int(app_settings.get("copilot_min_score", 60))
    # First-time check is gated by the threshold (cost control). But an explicit
    # RE-RUN on a job that was already advanced-checked must always run - even if
    # its score has since dropped below the threshold - and so must a job the user
    # pinned into Strong matches. Otherwise the button silently does nothing.
    already_checked = bool(job.get("advanced"))
    if (job.get("score") or 0) < threshold and not already_checked and not job.get("pinned"):
        return job, None, None
    profile = await queries.get_profile_full()
    # WRITE, not SCAN: this call is the one the user sits and waits for, so it
    # runs on the service chosen for latency rather than the one chosen for
    # volume. Only advanced* methods are called here, so the client is built from
    # that role alone - one client cannot speak to two endpoints.
    matcher = AIMatcher(model=_deep_model(app_settings),
                        advanced_model=_deep_model(app_settings),
                        **_ai(app_settings, providers.WRITE))
    return job, profile, matcher


async def _save_coverage(job: dict, findings: list[dict]) -> coverage.Coverage:
    """Verify each finding against the real advert and store what survives.

    Quotes that are not in the description and case stories that are not in the
    case bank do not survive `coverage.build`, so nothing is highlighted that
    the code could not anchor to the actual text."""
    cases = await queries.list_cases()
    found = coverage.build(job.get("description") or "", findings,
                           [c.get("title") or "" for c in cases])
    await queries.save_asset(job["id"], COVERAGE_KIND, json.dumps(
        [{"start": m.start, "end": m.end, "verdict": m.verdict,
          "quote": m.quote, "source": m.source, "evidence": m.evidence}
         for m in found.marks]))
    return found


@app.post("/jobs/{job_id}/advanced")
async def advanced_check(job_id: int):
    """On-demand deep analysis (resume + case bank). Only for good matches.

    The no-JS fallback: one combined call, then a redirect. The page normally
    drives the two halves separately via /advanced/verdict and /advanced/coverage
    so the verdict paints while the citations are still being fetched."""
    job, profile, matcher = await _advanced_prep(job_id)
    if job is None:
        return HTMLResponse("Job not found", status_code=404)
    if matcher is None:
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    result = await matcher.advanced(job, profile)
    # Only mark as done on success - on error leave the button so the user can retry.
    saved = bool(not result.error and (result.strengths or result.weaknesses))
    if saved:
        # Trust the deeper analysis: it has the full CV + case bank AND enforces
        # hard constraints (location/seniority/language), so it may lower the
        # score when it finds a blocker the quick pass missed - not only raise it.
        await queries.save_advanced(job_id, result.strengths, result.weaknesses,
                                    result.score if result.score is not None else None)
        await _save_coverage(job, result.coverage)
    else:
        logger.warning("Advanced check for job %s produced no result: %s",
                       job_id, result.error)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/advanced/verdict")
async def advanced_verdict(job_id: int):
    """Half one of the progressive check: score + pros/cons, no coverage.

    Returns the rendered pros/cons block so the page can drop it in the moment it
    lands, while the coverage half is still in flight."""
    job, profile, matcher = await _advanced_prep(job_id)
    if job is None:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    if matcher is None:
        return JSONResponse({"error": "This job is below the score threshold."},
                            status_code=403)

    result = await matcher.advanced_verdict(job, profile)
    if result.error or not (result.strengths or result.weaknesses):
        logger.warning("Advanced verdict for job %s produced no result: %s",
                       job_id, result.error)
        return JSONResponse({"error": result.error or "No verdict was produced."},
                            status_code=502)
    await queries.save_advanced(job_id, result.strengths, result.weaknesses,
                                result.score if result.score is not None else None)
    fresh = await queries.get_job(job_id)
    html = templates.get_template("partials/_proscons.html").render(job=fresh)
    return HTMLResponse(html)


@app.post("/jobs/{job_id}/advanced/coverage")
async def advanced_coverage(job_id: int):
    """Half two of the progressive check: the advert's demands cited one by one.

    Returns the rendered legend, highlighted description body and digest so the
    page can slot each into the Job Description card when the citations arrive."""
    job, profile, matcher = await _advanced_prep(job_id)
    if job is None:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    if matcher is None:
        return JSONResponse({"error": "This job is below the score threshold."},
                            status_code=403)

    result = await matcher.advanced_coverage(job, profile)
    if result.error:
        logger.warning("Advanced coverage for job %s failed: %s", job_id, result.error)
        return JSONResponse({"error": result.error}, status_code=502)
    found = await _save_coverage(job, result.coverage)
    description = job.get("description") or ""
    return JSONResponse({
        "legend": templates.get_template("partials/_coverage_legend.html").render(
            coverage=found),
        "body": coverage.render(description, found),
        "digest": templates.get_template("partials/_coverage_digest.html").render(
            coverage=found),
    })


# Per-job note the user writes for the cover letter ("mention my Shopify work").
# Stored in job_assets, which is already a per-job key/value store - it is NOT in
# gen.KINDS, so it never becomes a Copilot tab of its own.
CL_BRIEF_KIND = "cl_brief"
# The recipient's postal address, typed per application. Stored beside the brief
# because it is the same kind of thing: a per-job note, not a generated asset.
CL_ADDRESS_KIND = "cl_address"
# The advert's demands, marked where they sit in the text. Stored so the page
# does not re-run a paid analysis to redraw a colour.
COVERAGE_KIND = "advanced_coverage"


@app.post("/jobs/{job_id}/asset/{kind}")
async def save_asset_text(job_id: int, kind: str, content: str = Form("")):
    """Persist edits the user made in the generated text.

    The asset panels are contenteditable, but nothing used to save them: the
    text lived only in the DOM, so a reload or a PDF export silently restored
    the pre-edit version. Plain text is stored (the panel renders Markdown, and
    paragraphs survive the round trip)."""
    if kind not in gen.KINDS:
        return JSONResponse({"error": "Unknown asset type."}, status_code=400)
    body = (content or "").strip()
    if not body:
        return JSONResponse({"error": "Nothing to save."}, status_code=400)
    await queries.save_asset(job_id, kind, body)
    return JSONResponse({"saved": True})


@app.post("/jobs/{job_id}/brief")
async def save_brief(job_id: int, brief: str = Form("")):
    """Save the cover-letter brief without spending tokens on a regeneration."""
    await queries.save_asset(job_id, CL_BRIEF_KIND, brief.strip())
    return RedirectResponse(url=f"/jobs/{job_id}#cover_letter", status_code=303)


@app.post("/jobs/{job_id}/generate/{kind}")
async def generate_asset(job_id: int, kind: str, brief: str = Form("")):
    job = await queries.get_job(job_id)
    if job is None:
        return HTMLResponse("Job not found", status_code=404)

    app_settings = await queries.get_app_settings()
    threshold = int(app_settings.get("copilot_min_score", 60))
    if (job.get("score") or 0) < threshold and not job.get("pinned"):
        # Below threshold and not pinned - do not spend tokens; just bounce back.
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    # get_profile_full() already sets resume_text from the CV document.
    profile = await queries.get_profile_full()
    generator = gen.CopilotGenerator(model=_write_model(app_settings),
                                     **_ai(app_settings, providers.WRITE))
    if kind == "cv_adjustment":
        import json as _json
        result = await generator.cv_edits(job, profile)
        # Snap each 'before' to the verbatim text of its CV element, so Apply
        # always matches even if the model paraphrased slightly.
        cv_html = (profile.get("cv_html") or "").strip()
        result["edits"] = _snap_edits_to_cv(cv_html, result.get("edits", []))
        await queries.save_asset(job_id, kind, _json.dumps(result, ensure_ascii=False))
    else:
        if kind == "cover_letter":
            # Remember the brief so a later regeneration keeps the same asks.
            brief = brief.strip()
            if brief:
                await queries.save_asset(job_id, CL_BRIEF_KIND, brief)
            else:
                brief = (await queries.get_assets(job_id)).get(CL_BRIEF_KIND, "")
        content = await generator.generate(kind, job, profile, brief=brief)
        await queries.save_asset(job_id, kind, content)
    return RedirectResponse(url=f"/jobs/{job_id}#{kind}", status_code=303)


def _cv_elements(cv_html: str) -> list[str]:
    """Verbatim text of every editable CV element (bullets, paragraphs, headline)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(cv_html, "html.parser")
    els = soup.find_all(["li", "p"]) + soup.select(".cv-subtitle")
    return [" ".join(el.get_text(" ").split()) for el in els if el.get_text(strip=True)]


def _cv_similarity(a: str, b: str) -> float:
    """Similarity robust to paraphrase/shortening: max of sequence ratio and
    word-containment (share of a's words present in b)."""
    from difflib import SequenceMatcher

    a, b = a.lower(), b.lower()
    seq = SequenceMatcher(None, a, b).ratio()
    wa = {w for w in a.replace(",", " ").replace(".", " ").split() if len(w) > 2}
    wb = {w for w in b.replace(",", " ").replace(".", " ").split() if len(w) > 2}
    contain = (len(wa & wb) / len(wa)) if wa else 0.0
    return max(seq, contain)


def _snap_edits_to_cv(cv_html: str, edits: list[dict]) -> list[dict]:
    """Rewrite each edit's 'before' to the closest ACTUAL CV element text.
    Drops edits that match nothing - those would only produce
    'could not match' errors in the UI."""
    elements = _cv_elements(cv_html)
    if not elements:
        return edits
    snapped: list[dict] = []
    for e in edits:
        target = " ".join((e.get("before") or "").split())
        best_txt, best_score = None, 0.0
        for txt in elements:
            score = _cv_similarity(target, txt)
            if score > best_score:
                best_txt, best_score = txt, score
        if best_txt is not None and best_score >= 0.5:
            snapped.append({**e, "before": best_txt})
        else:
            logger.warning("CV edit dropped (no matching element, score %.2f): %.80s",
                           best_score, e.get("before", ""))
    return snapped


def _apply_cv_edit(cv_html: str, before: str, after: str) -> tuple[str, bool]:
    """Replace the best-matching CV element (bullet, summary, headline or skills
    line) with `after`. Returns (new_html, applied)."""
    from bs4 import BeautifulSoup

    def norm(s: str) -> str:
        s = " ".join((s or "").split())
        for pre in ("HEADLINE:", "headline:", "- "):
            if s.startswith(pre):
                s = s[len(pre):].strip()
        return s

    target = norm(before)
    soup = BeautifulSoup(cv_html, "html.parser")
    # Editable text elements: bullets, summary paragraph(s), headline, skills.
    candidates = soup.find_all(["li", "p"]) + soup.select(".cv-subtitle")
    best, best_score = None, 0.0
    for el in candidates:
        score = _cv_similarity(target, norm(el.get_text(" ")))
        if score > best_score:
            best, best_score = el, score
    # Require a strong match so we never clobber the wrong element.
    if best is None or best_score < 0.55:
        return cv_html, False
    best.clear()
    best.append(after)
    return str(soup), True


@app.post("/cv/apply-edit")
async def cv_apply_edit(before: str = Form(...), after: str = Form(...)):

    profile = await queries.get_profile()
    cv_html = (profile.get("cv_html") or "").strip()
    new_html, applied = _apply_cv_edit(cv_html, before, after)
    if applied:
        await queries.save_cv_html(new_html)
    return JSONResponse({"ok": True, "applied": applied})


@app.post("/jobs/{job_id}/applied")
async def toggle_applied(job_id: int, next: str = Form("/")):
    job = await queries.get_job(job_id)
    if job is not None:
        await queries.set_applied(job_id, 0 if job.get("applied") else 1)
    return RedirectResponse(url=next or "/", status_code=303)


@app.post("/jobs/{job_id}/delete")
async def delete_job_route(job_id: int, next: str = Form("/")):
    await queries.delete_job(job_id)
    return RedirectResponse(url=next or "/", status_code=303)


@app.post("/jobs/clear")
async def clear_jobs_route(view: str = Form("all")):
    # Clear only the current tab. Saved, Applied and pinned jobs are separate
    # lists the user curates by hand - clearing results never touches them.
    #   'strong' → only jobs at/above the match threshold;
    #   'all'    → the whole firehose, scored or not.
    app_settings = await queries.get_app_settings()
    threshold = int(app_settings.get("copilot_min_score", 60))
    view = "strong" if view == "strong" else "all"
    if view == "strong":
        await queries.clear_jobs(min_score=threshold)
    else:
        await queries.clear_jobs()
    return RedirectResponse(url=f"/?view={view}", status_code=303)


@app.post("/jobs/{job_id}/favorite")
async def favorite_job(job_id: int, next: str = Form("/")):
    await queries.toggle_favorite(job_id)
    # Return to wherever the user clicked from (jobs list or job detail).
    return RedirectResponse(url=next or "/", status_code=303)


@app.post("/jobs/{job_id}/pin")
async def pin_job(job_id: int, next: str = Form("/")):
    """Manually add/remove a job to 'Strong matches' regardless of its AI score.
    Pinning also unlocks the advanced check + copilot for that job."""
    await queries.toggle_pinned(job_id)
    return RedirectResponse(url=next or "/", status_code=303)


@app.get("/favorites")
async def favorites_redirect():
    # Saved is now a tab on the Jobs page.
    return RedirectResponse(url="/?view=saved", status_code=307)


# --------------------------------------------------------------------------- #
# Profile                                                                     #
# --------------------------------------------------------------------------- #

# What the letterhead is filled with when it is only being looked at. Real
# enough to judge the layout by - a letterhead is spacing and hierarchy, and
# neither is visible against "{{company}}".
_LETTER_SAMPLE = {
    "company": "Vondel Ventures",
    "company_address": "Keizersgracht 241\n1016 EA Amsterdam",
    "job_title": "Product Operations Manager",
    "body_html": ("<p>Dear Hiring Team,</p><p>This is what your letter will look "
                  "like on the page. The words change with every application; the "
                  "layout above and around them is what you are choosing here.</p>"
                  "<p>Best regards,</p>"),
}


def _letter_preview(template: str) -> str:
    """The letterhead as it prints, as a standalone document for an iframe."""
    try:
        return letter_template.document(template, **_LETTER_SAMPLE)
    except Exception as exc:  # noqa: BLE001 - a preview must never take the page down
        logger.warning("Letterhead preview failed: %s", exc)
        return ""


async def _profile_context(request: Request, tab: str = "instructions", **extra):
    profile = await queries.get_profile()
    cases = await queries.list_cases()
    # Whether the user has uploaded their own CV (vs. the built-in sample).
    has_cv = bool((profile.get("cv_original_html") or profile.get("cv_html") or "").strip())
    ctx = {
        "active": "profile", "profile": profile, "cases": cases, "tab": tab,
        "has_cv": has_cv, "cv_doc_css": cv_content.CV_DOC_CSS,
        # The letterhead, falling back to the shipped default so the editor is
        # never an empty box the user has to invent a document in.
        "letter_html": (profile.get("letter_html") or "").strip()
                       or letter_template.default_template(profile.get("cv_html")),
        "letter_placeholders": letter_template.PLACEHOLDERS,
        "letter_brief_max": letter_template.BRIEF_MAX,
        "letter_preview": _letter_preview(
            (profile.get("letter_html") or "").strip()
            or letter_template.default_template(profile.get("cv_html"))),
    }
    ctx.update(extra)
    return templates.TemplateResponse(request, "profile.html", ctx)


@app.get("/profile", response_class=HTMLResponse)
async def profile_form(request: Request, tab: str = "instructions",
                       saved: int = 0, err: str = ""):
    extra = {}
    if saved:
        extra["saved"] = True
    if err == "pdf":
        extra["error"] = "Could not read text from that PDF (it may be scanned/image-only)."
    return await _profile_context(request, tab=tab, **extra)


@app.post("/profile/instructions", response_class=HTMLResponse)
async def save_instructions(request: Request, instructions: str = Form("")):
    p = await queries.get_profile()
    await queries.save_profile(p["resume_text"], instructions, "")
    return await _profile_context(request, tab="instructions", saved=True)


@app.post("/profile/resume", response_class=HTMLResponse)
async def save_resume(request: Request, resume_text: str = Form("")):
    p = await queries.get_profile()
    await queries.save_profile(resume_text, p["instructions"], "")
    return await _profile_context(request, tab="advanced", saved=True)


def _extract_pdf_text(data: bytes) -> str:
    """Extract text from a PDF byte string. Returns '' on failure."""
    import io

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("PDF extraction failed: %s", exc)
        return ""


@app.post("/profile/upload-resume", response_class=HTMLResponse)
async def upload_resume(request: Request, resume_pdf: UploadFile = File(...)):
    """Extract text from an uploaded PDF résumé into the resume_text field."""
    p = await queries.get_profile()
    data = await resume_pdf.read()
    text = _extract_pdf_text(data)
    error = None
    if text:
        await queries.save_profile(text, p["instructions"], "")
    else:
        error = "Could not read text from that PDF (it may be scanned/image-only)."
    return await _profile_context(request, tab="advanced", saved=bool(text), error=error)


# The page adds and deletes cases in place, so these redirects are only the
# no-JS fallback. They point at the card rather than the top of the page: after
# typing a story, being returned to the CV uploader is a small insult.
@app.post("/profile/cases")
async def add_case_route(text: str = Form(""), title: str = Form("")):
    if text.strip():
        await queries.add_case(text, title)
    return RedirectResponse(url="/profile?tab=advanced#case-bank", status_code=303)


@app.post("/profile/cases/{case_id}/delete")
async def delete_case_route(case_id: int):
    await queries.delete_case(case_id)
    return RedirectResponse(url="/profile?tab=advanced#case-bank", status_code=303)


# --------------------------------------------------------------------------- #
# CV editor (edit in your own template, export to PDF via browser print)      #
# --------------------------------------------------------------------------- #

# Known résumé section headers (longest first so "TECHNICAL SKILLS & EDUCATION"
# wins over "SKILLS"/"EDUCATION"). Matched case-insensitively as whole phrases.
_CV_SECTIONS = [
    "PROFESSIONAL SUMMARY", "PROFESSIONAL EXPERIENCE", "WORK EXPERIENCE",
    "TECHNICAL SKILLS & EDUCATION", "TECHNICAL SKILLS", "SKILLS & EDUCATION",
    "SELECTED PROJECTS", "KEY PROJECTS", "CORE COMPETENCIES",
    "EXPERIENCE", "EMPLOYMENT", "PROJECTS", "SKILLS", "EDUCATION",
    "CERTIFICATIONS", "LANGUAGES", "ACHIEVEMENTS", "SUMMARY", "PROFILE",
    "OBJECTIVE", "CONTACT", "ABOUT",
]


def _pdf_to_cv_html(data: bytes) -> str:
    """Heuristic fallback: rebuild structured .cv-doc HTML from extracted CV text
    even when the PDF extracts as one long blob (no line breaks). Splits on known
    section headers and '•' bullets. Not perfect, but far better than flat text.
    (The AI structurer is the primary path; this is the no-key fallback.)"""
    import html as _html
    import re

    text = _extract_pdf_text(data)
    if not text:
        return ""
    # Normalise to a single spaced string (extraction often loses newlines).
    blob = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    if not blob:
        return ""

    def esc(s: str) -> str:
        return _html.escape(s.strip(" |·").strip())

    # Locate section headers.
    pat = re.compile(r"\b(" + "|".join(re.escape(h) for h in _CV_SECTIONS) + r")\b")
    matches = list(pat.finditer(blob))
    header_region = blob[: matches[0].start()] if matches else blob

    parts: list[str] = []

    # --- Header: name / subtitle / contact -------------------------------- #
    m = re.match(r"\s*([A-Z][A-Z'’.\-]+(?:\s+[A-Z][A-Z'’.\-]+){0,3})", header_region)
    name = m.group(1).strip() if m else ""
    rest = header_region[m.end():] if m else header_region
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", rest)
    phone = re.search(r"\+?\d[\d ()\-]{7,}\d", rest)
    contact_bits = [x.group(0).strip() for x in (email, phone) if x]
    # Subtitle = header text minus name/contact, cleaned of stray glyphs.
    sub = rest
    for x in (email, phone):
        if x:
            sub = sub.replace(x.group(0), " ")
    sub = re.sub(r"\s+", " ", sub).strip(" |·").strip()
    sub = re.sub(r"^(fc|in|\W)\s+", "", sub).strip()  # drop leading icon glyphs
    if name:
        parts.append(f'<div class="cv-name">{esc(name)}</div>')
    if sub:
        parts.append(f'<div class="cv-subtitle"><strong>{esc(sub)}</strong></div>')
    if contact_bits:
        parts.append(f'<div class="cv-contact">{esc(" | ".join(contact_bits))}</div>')

    # --- Sections --------------------------------------------------------- #
    def render_body(title: str, body: str):
        body = body.strip()
        if not body:
            return
        segs = [s.strip() for s in body.split("•")]
        lead = segs[0].strip()
        bullets = [s for s in segs[1:] if s.strip()]
        low = title.lower()
        is_summaryish = any(w in low for w in ("summary", "profile", "objective", "about"))
        if is_summaryish and not bullets:
            parts.append(f"<p>{esc(body)}</p>")
            return
        # A lead containing "|" or a date looks like a role/entry line.
        if lead and ("|" in lead or re.search(r"\b(19|20)\d\d\b", lead)):
            parts.append(f'<div class="entry"><div class="entry-head">'
                         f'<span><strong>{esc(lead)}</strong></span></div>')
            if bullets:
                parts.append("<ul>" + "".join(f"<li>{esc(b)}</li>" for b in bullets) + "</ul>")
            parts.append("</div>")
        else:
            if lead:
                parts.append(f"<p>{esc(lead)}</p>")
            if bullets:
                parts.append("<ul>" + "".join(f"<li>{esc(b)}</li>" for b in bullets) + "</ul>")

    if not matches:
        render_body("", blob[len(header_region):] if header_region != blob else "")
    else:
        for i, mt in enumerate(matches):
            title = mt.group(1).title()
            start = mt.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(blob)
            parts.append(f"<h2>{esc(title)}</h2>")
            render_body(title, blob[start:end])

    return "\n".join(parts)


@app.post("/cv/upload")
async def cv_upload(cv_pdf: UploadFile = File(...)):
    """Import a CV PDF into the CV document, rebuilding its layout.

    PDF text extraction loses the résumé's structure, so we first ask the AI to
    reconstruct it into our .cv-doc format (name, sections, entries, bullets,
    links). If the AI is unavailable, fall back to a structured heuristic.
    """
    data = await cv_pdf.read()
    raw_text = _extract_pdf_text(data)
    if not raw_text:
        return RedirectResponse(url="/profile?tab=advanced&err=pdf", status_code=303)

    app_settings = await queries.get_app_settings()
    html_body = await gen.structure_cv_html(
        raw_text,
        model=_write_model(app_settings),
        **_writer_client(app_settings),
    )
    if not html_body:
        html_body = _pdf_to_cv_html(data)  # heuristic fallback

    if html_body:
        # Uploading replaces the whole CV → this becomes the new baseline too.
        await queries.save_cv_html(html_body, set_original=True)
        return RedirectResponse(url="/profile?tab=advanced&saved=1", status_code=303)
    return RedirectResponse(url="/profile?tab=advanced&err=pdf", status_code=303)


# The largest font the CV is tried at. The PDF steps down from here until the
# document fits one page; the preview shows this size, which is what prints
# unless the CV is long enough to need shrinking.
_CV_BASE_PT = 12.5


def _cv_document(cv_html: str, base: float = _CV_BASE_PT) -> str:
    """The printable CV as a standalone HTML document.

    One builder, used by the PDF and by the preview. Two would drift, and a
    preview of a document nobody prints is worse than no preview."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "@page { size: A4; margin: 12mm; } body { margin: 0; } "
        + cv_content.build_cv_css(base) +
        "</style></head><body><div class='cv-doc'>" + cv_html + "</div></body></html>"
    )


def _render_cv_pdf(cv_html: str) -> bytes:
    """Render the CV to PDF, auto-shrinking the font a little at a time until it
    fits on ONE page (down to a readable floor). Raises on engine errors."""
    import io

    from weasyprint import HTML

    def render(base: float) -> bytes:
        return HTML(string=_cv_document(cv_html, base)).write_pdf()

    def pages(pdf_bytes: bytes) -> int:
        try:
            from pypdf import PdfReader

            return len(PdfReader(io.BytesIO(pdf_bytes)).pages)
        except Exception:  # noqa: BLE001
            return 1

    last = None
    for base in (12.5, 12, 11.5, 11, 10.5, 10, 9.5, 9):
        last = render(base)
        if pages(last) <= 1:
            return last
    return last  # smallest tried - best effort


def _render_letter_pdf(body_html: str, template: str, *, company: str = "",
                       company_address: str = "", job_title: str = "") -> bytes:
    """Render the cover letter onto the user's letterhead as an A4 PDF."""
    from weasyprint import HTML

    doc = letter_template.document(
        template, body_html=body_html, company=company,
        company_address=company_address, job_title=job_title)
    return HTML(string=doc).write_pdf()


def _safe_filename(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', " ", s or "").strip()
    return re.sub(r"\s+", " ", s)[:80] or "Company"


@app.post("/jobs/{job_id}/cover-letter.pdf")
async def cover_letter_pdf(job_id: int, text: str = Form("")):
    """Save the cover letter as a PDF into Downloads.

    `text` carries what is on screen right now, so edits made seconds ago end up
    in the PDF instead of the stored version."""
    job = await queries.get_job(job_id)
    if job is None:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    assets = await queries.get_assets(job_id)
    md = (text or "").strip() or assets.get("cover_letter")
    if not md:
        return JSONResponse({"error": "Generate the cover letter first."}, status_code=400)
    company = _safe_filename(job.get("company") or "Company")
    template = await queries.get_letter_template()
    address = assets.get(CL_ADDRESS_KIND, "")
    try:
        pdf = _render_letter_pdf(
            _md_to_html(md), template,
            company=job.get("company") or "", company_address=address,
            job_title=job.get("title") or "")
        path = _save_pdf_to_downloads(pdf, f"Cover Letter to {company}.pdf")
    except ImportError:
        return JSONResponse({"error": "PDF engine not installed (macOS: brew install pango)."},
                            status_code=501)
    except Exception as exc:  # noqa: BLE001
        logger.error("Cover letter PDF failed: %s", exc)
        return JSONResponse({"error": f"PDF render failed: {exc}"}, status_code=500)
    return JSONResponse({"saved": True, "path": path})


@app.post("/jobs/{job_id}/cover-letter/preview")
async def cover_letter_preview(job_id: int, text: str = Form("")):
    """The exact page the PDF will print, as HTML, before anything is written.

    Same document builder the PDF uses - a preview assembled by a different
    path is a picture of something else. Only the renderer differs, and the
    layout is CSS either way."""
    job = await queries.get_job(job_id)
    if job is None:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    assets = await queries.get_assets(job_id)
    md = (text or "").strip() or assets.get("cover_letter")
    if not md:
        return JSONResponse({"error": "Generate the cover letter first."}, status_code=400)
    doc = letter_template.document(
        await queries.get_letter_template(),
        body_html=_md_to_html(md),
        company=job.get("company") or "",
        company_address=assets.get(CL_ADDRESS_KIND, ""),
        job_title=job.get("title") or "")
    return JSONResponse({"ok": True, "html": doc})


@app.post("/cv/preview")
async def cv_preview(html: str = Form("")):
    """The CV as the PDF will print it. `html` carries the on-screen document,
    so the tailored column previews its own edits; without it the stored CV is
    used, which is what the untouched column prints."""
    cv_html = (html or "").strip() or (await queries.get_profile()).get("cv_html") or ""
    if not cv_html.strip():
        return JSONResponse({"error": "No CV yet. Add one in Profile → Advanced."},
                            status_code=400)
    return JSONResponse({"ok": True, "html": _cv_document(cv_html)})


@app.get("/cv.pdf")
async def cv_pdf():
    """Render the CV document to a real, downloadable PDF (server-side)."""
    profile = await queries.get_profile()
    cv_html = (profile.get("cv_html") or "").strip()
    if not cv_html:
        return JSONResponse({"error": "No CV uploaded yet. Add one in Profile → Advanced."},
                            status_code=400)
    try:
        pdf = _render_cv_pdf(cv_html)
    except ImportError:
        return JSONResponse(
            {"error": "PDF engine not installed. Run: pip install weasyprint "
                      "(macOS also needs: brew install pango)."},
            status_code=501,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("CV PDF render failed: %s", exc)
        return JSONResponse({"error": f"PDF render failed: {exc}"}, status_code=500)

    from fastapi.responses import Response

    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="CV.pdf"'},
    )


async def _tailored_cv_html(job_id: int) -> str:
    """The original CV with this job's suggested edits applied (the 'after' doc)."""
    profile = await queries.get_profile()
    working = (profile.get("cv_html") or "").strip()
    if not working and not (profile.get("cv_original_html") or "").strip():
        return ""
    original = await queries.ensure_cv_original(working)
    raw = await queries.get_assets(job_id)
    tailored = original
    import json as _json
    try:
        edits = _json.loads(raw.get("cv_adjustment", "") or "{}").get("edits", [])
    except (ValueError, TypeError):
        edits = []
    for e in edits:
        new_html, ok = _apply_cv_edit(tailored, e.get("before", ""), e.get("after", ""))
        if ok:
            tailored = new_html
    return tailored


def _save_pdf_to_downloads(pdf: bytes, filename: str) -> str:
    """Write a PDF to the user's Downloads folder; return the saved path.
    Reliable in the desktop app where in-browser blob downloads don't work."""
    downloads = Path.home() / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    dest = downloads / filename
    # Avoid clobbering: append (1), (2)… if it exists.
    stem, suffix = dest.stem, dest.suffix
    i = 1
    while dest.exists():
        dest = downloads / f"{stem} ({i}){suffix}"
        i += 1
    dest.write_bytes(pdf)
    return str(dest)


@app.post("/cv/save-pdf")
async def cv_save_pdf():
    profile = await queries.get_profile()
    cv_html = (profile.get("cv_html") or "").strip()
    if not cv_html:
        return JSONResponse({"error": "No CV uploaded yet. Add one in Profile → Advanced."},
                            status_code=400)
    try:
        pdf = _render_cv_pdf(cv_html)
        path = _save_pdf_to_downloads(pdf, "Job Finder CV.pdf")
    except ImportError:
        return JSONResponse({"error": "PDF engine not installed (macOS: brew install pango)."},
                            status_code=501)
    except Exception as exc:  # noqa: BLE001
        logger.error("CV PDF save failed: %s", exc)
        return JSONResponse({"error": f"PDF render failed: {exc}"}, status_code=500)
    return JSONResponse({"saved": True, "path": path})


@app.post("/cv/chat")
async def cv_chat(instruction: str = Form(...), html: str = Form("")):
    """Rewrite the CV from a plain-language instruction, streamed.

    Returns the new document as a raw text stream rather than one JSON blob: a
    full rewrite takes several seconds, and the editor renders each chunk as it
    lands so the user watches it happen instead of staring at a frozen page.
    Nothing is persisted here - the user reviews the result and presses Save."""
    from fastapi.responses import StreamingResponse

    text = (instruction or "").strip()
    if not text:
        return JSONResponse({"error": "Tell me what to change."}, status_code=400)
    profile = await queries.get_profile_full()
    cv_html = (html or "").strip() or (profile.get("cv_html") or "").strip()
    if not cv_html:
        return JSONResponse({"error": "No CV yet. Upload one in Profile."}, status_code=400)

    app_settings = await queries.get_app_settings()
    stream = gen.stream_cv_edit(
        text, cv_html, profile,
        model=_write_model(app_settings),
        **_writer_client(app_settings),
    )
    return StreamingResponse(stream, media_type="text/plain; charset=utf-8",
                             headers={"X-Accel-Buffering": "no",
                                      "Cache-Control": "no-store"})


@app.post("/jobs/{job_id}/letter/chat")
async def letter_chat(job_id: int, instruction: str = Form(...), text: str = Form("")):
    """Rewrite the cover letter from a plain-language instruction, streamed.
    Nothing is persisted - the user reviews, then presses Save edits."""
    from fastapi.responses import StreamingResponse

    said = (instruction or "").strip()
    if not said:
        return JSONResponse({"error": "Tell me what to change."}, status_code=400)
    job = await queries.get_job(job_id)
    if job is None:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    body = (text or "").strip() or (await queries.get_assets(job_id)).get("cover_letter", "")
    if not body:
        return JSONResponse({"error": "Generate the cover letter first."}, status_code=400)

    profile = await queries.get_profile_full()
    app_settings = await queries.get_app_settings()
    stream = gen.stream_letter_edit(
        said, body, job, profile,
        model=_write_model(app_settings),
        **_writer_client(app_settings),
    )
    return StreamingResponse(stream, media_type="text/plain; charset=utf-8",
                             headers={"X-Accel-Buffering": "no",
                                      "Cache-Control": "no-store"})


@app.post("/cv/save")
async def cv_save(html: str = Form(...)):
    """Save CV edits made inline in the CV-adjustment tab.

    Only the working copy is touched - `cv_original_html` stays as the pristine
    baseline, so the before/after comparison keeps a stable left-hand side."""
    body = (html or "").strip()
    if not body:
        return JSONResponse({"error": "Nothing to save."}, status_code=400)
    await queries.save_cv_html(body)
    return JSONResponse({"saved": True})


@app.post("/jobs/{job_id}/cv/save-pdf")
async def tailored_cv_save_pdf(job_id: int, html: str = Form("")):
    # `html` carries what is on screen right now, including inline edits the
    # user just made; without it the PDF would silently ignore them.
    cv_html = (html or "").strip() or await _tailored_cv_html(job_id)
    if not cv_html:
        return JSONResponse({"error": "No CV uploaded yet. Add one in Profile → Advanced."},
                            status_code=400)
    try:
        pdf = _render_cv_pdf(cv_html)
        path = _save_pdf_to_downloads(pdf, "Job Finder CV - tailored.pdf")
    except ImportError:
        return JSONResponse({"error": "PDF engine not installed (macOS: brew install pango)."},
                            status_code=501)
    except Exception as exc:  # noqa: BLE001
        logger.error("Tailored CV PDF save failed: %s", exc)
        return JSONResponse({"error": f"PDF render failed: {exc}"}, status_code=500)
    return JSONResponse({"saved": True, "path": path})


@app.get("/jobs/{job_id}/cv.pdf")
async def tailored_cv_pdf(job_id: int):
    """Download the role-tailored CV (original + suggested edits) as a PDF."""
    cv_html = await _tailored_cv_html(job_id)
    if not cv_html:
        return JSONResponse({"error": "No CV uploaded yet. Add one in Profile → Advanced."},
                            status_code=400)
    try:
        pdf = _render_cv_pdf(cv_html)
    except ImportError:
        return JSONResponse(
            {"error": "PDF engine not installed. Run: pip install weasyprint "
                      "(macOS also needs: brew install pango)."}, status_code=501)
    except Exception as exc:  # noqa: BLE001
        logger.error("Tailored CV PDF render failed: %s", exc)
        return JSONResponse({"error": f"PDF render failed: {exc}"}, status_code=500)
    from fastapi.responses import Response
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="CV-tailored.pdf"'},
    )


# --------------------------------------------------------------------------- #
# Searches - multiple named search profiles                                   #
# --------------------------------------------------------------------------- #

def _csv_from_form(values: list[str]) -> str:
    return ",".join(v for v in values if v)


# Empty-config shape used when rendering the "new search" form.
_EMPTY_SEARCH = {
    "id": None, "name": "", "keywords": "", "location": "",
    "tpr_seconds": 86400, "experience": "", "job_types": "", "remote": "",
    "source": "linkedin", "max_jobs": 50,
    "years_experience": None, "years_margin": 1,
}


def _years_from_form(raw: str) -> int | None:
    """A whole number of years from a form field, or None for "not stated".

    Taken as text: an emptied number box posts "", which an int Form() field
    answers with a 422 and the whole search is lost with it."""
    try:
        years = int(str(raw).strip())
    except ValueError:
        return None
    return years if 0 <= years <= 50 else None

# Sources offered in the search form (all wired up). A search can target any
# combination - the scanner runs each in turn and de-duplicates.
SEARCH_SOURCE_OPTIONS = [
    ("linkedin", "LinkedIn", True),
    ("remotive", "Remotive (remote)", True),
    ("remoteok", "RemoteOK (remote)", True),
    ("arbeitnow", "Arbeitnow (EU)", True),
    ("jobicy", "Jobicy (EU, remote)", True),
    ("himalayas", "Himalayas (remote)", True),
    ("workable", "Workable — company career pages (any country)", True),
    ("weworkremotely", "WeWorkRemotely (remote)", True),
    ("eu_boards", "European company boards (Greenhouse, Lever, Ashby, Personio…)", True),
    ("arbeitsagentur", "Arbeitsagentur — German state board (mostly German)", True),
    ("platsbanken", "Platsbanken — Swedish state board (mostly Swedish)", True),
]


@app.get("/search", response_class=HTMLResponse)
async def search_list(request: Request):
    searches = await queries.list_searches()
    source_labels = {val: name for val, name, _ in SEARCH_SOURCE_OPTIONS}
    return templates.TemplateResponse(
        request, "search_list.html",
        {"active": "search", "searches": searches, "source_labels": source_labels},
    )


@app.get("/search/new", response_class=HTMLResponse)
async def search_new(request: Request):
    # The Settings figure is the DEFAULT for a new search, which is all it has
    # ever claimed to be - but the form ignored it and hardcoded 50, so changing
    # it in Settings did nothing at all.
    app_settings = await queries.get_app_settings()
    cfg = {**_EMPTY_SEARCH,
           "max_jobs": int(app_settings.get("default_max_jobs") or 50)}
    return templates.TemplateResponse(
        request,
        "search_form.html",
        {"active": "search", "cfg": cfg, "sources": SEARCH_SOURCE_OPTIONS},
    )


@app.get("/search/{search_id}", response_class=HTMLResponse)
async def search_edit(request: Request, search_id: int):
    cfg = await queries.get_search(search_id)
    if cfg is None:
        return RedirectResponse(url="/search", status_code=303)
    return templates.TemplateResponse(
        request,
        "search_form.html",
        {"active": "search", "cfg": cfg, "sources": SEARCH_SOURCE_OPTIONS},
    )


@app.post("/search/new")
async def search_create(
    name: str = Form(""),
    keywords: str = Form(""),
    location: str = Form(""),
    tpr_seconds: int = Form(86400),
    experience: list[str] = Form(default=[]),
    job_types: list[str] = Form(default=[]),
    remote: list[str] = Form(default=[]),
    source: list[str] = Form(default=[]),
    max_jobs: int = Form(50),
    years_experience: str = Form(""),
    years_margin: str = Form("1"),
):
    margin = _years_from_form(years_margin)
    await queries.create_search(
        name=name.strip() or "Untitled search",
        keywords=keywords,
        location=location,
        tpr_seconds=tpr_seconds,
        experience=_csv_from_form(experience),
        job_types=_csv_from_form(job_types),
        remote=_csv_from_form(remote),
        source=_csv_from_form(source) or "linkedin",
        max_jobs=max_jobs,
        years_experience=_years_from_form(years_experience),
        years_margin=1 if margin is None else margin,
    )
    return RedirectResponse(url="/search", status_code=303)


@app.post("/search/{search_id}")
async def search_update(
    search_id: int,
    name: str = Form(""),
    keywords: str = Form(""),
    location: str = Form(""),
    tpr_seconds: int = Form(86400),
    experience: list[str] = Form(default=[]),
    job_types: list[str] = Form(default=[]),
    remote: list[str] = Form(default=[]),
    source: list[str] = Form(default=[]),
    max_jobs: int = Form(50),
    years_experience: str = Form(""),
    years_margin: str = Form("1"),
):
    margin = _years_from_form(years_margin)
    await queries.update_search(
        search_id=search_id,
        name=name.strip() or "Untitled search",
        keywords=keywords,
        location=location,
        tpr_seconds=tpr_seconds,
        experience=_csv_from_form(experience),
        job_types=_csv_from_form(job_types),
        remote=_csv_from_form(remote),
        source=_csv_from_form(source) or "linkedin",
        max_jobs=max_jobs,
        years_experience=_years_from_form(years_experience),
        years_margin=1 if margin is None else margin,
    )
    return RedirectResponse(url="/search", status_code=303)


@app.post("/search/{search_id}/delete")
async def search_delete(search_id: int):
    await queries.delete_search(search_id)
    return RedirectResponse(url="/search", status_code=303)


@app.post("/jobs/{job_id}/letter-address")
async def save_letter_address(job_id: int, address: str = Form("")):
    """The recipient's postal address for this application's letterhead."""
    await queries.save_asset(job_id, CL_ADDRESS_KIND, address.strip())
    return JSONResponse({"ok": True})


@app.post("/profile/letter-template")
async def save_letter_template_route(letter_html: str = Form("")):
    await queries.save_letter_template(letter_html)
    return RedirectResponse("/profile?tab=letter", status_code=303)


@app.post("/profile/letter-template/describe")
async def describe_letter_template(brief: str = Form(""), current: str = Form("")):
    """Redraw the letterhead from one line of plain English.

    The letterhead is the only thing in the app a user was expected to edit as
    raw HTML, which is a fair thing to ask of nobody. The description is capped
    at 200 characters on purpose: a letterhead is a shape, and a longer brief
    turns into instructions about the letter, which this does not write.

    Nothing is saved here. The result lands in the editor and the existing Save
    button is still what commits it - a layout you have not seen yet should not
    replace the one you are printing on."""
    app_settings = await queries.get_app_settings()
    profile = await queries.get_profile()
    sender = letter_template._sender_block(profile.get("cv_html")) or ""
    html, error = await gen.letterhead_from_brief(
        brief,
        current=current or await queries.get_letter_template(),
        sender_block=sender,
        model=providers.resolve_model(app_settings, "write"),
        **_ai(app_settings, providers.WRITE),
    )
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    return JSONResponse({"ok": True, "html": html,
                         "preview": _letter_preview(html),
                         "dropped": letter_template.missing_tokens(html)})


@app.post("/jobs/{job_id}/stage")
async def set_stage_route(request: Request, job_id: int,
                          stage: str = Form(...), next: str = Form("")):
    """Move an application along the pipeline."""
    if not pipeline.is_valid(stage):
        return JSONResponse({"error": "unknown stage"}, status_code=400)
    if await queries.get_job(job_id) is None:
        return JSONResponse({"error": "job not found"}, status_code=404)
    await queries.set_stage(job_id, stage)
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True, "stage": stage, "label": pipeline.label(stage)})
    return RedirectResponse(next or f"/jobs/{job_id}", status_code=303)


# --------------------------------------------------------------------------- #
# Referral outreach                                                            #
# --------------------------------------------------------------------------- #

class IngestPerson(BaseModel):
    name: str
    headline: str | None = None
    profile_url: str | None = None
    # 1 / 2 / 3, or null when the badge could not be read. It decides the whole
    # route: direct message, invite first, or out of reach entirely.
    degree: int | None = None


class ContactsPayload(BaseModel):
    job_id: int
    people: list[IngestPerson]


@app.post("/jobs/{job_id}/people/start")
async def people_start(job_id: int):
    """Arm the extension for this job and hand back the page to open.

    The extension cannot read the app's page, so the target is parked in the
    database and the extension asks for it over the HTTP channel it already
    uses for ingest."""
    job = await queries.get_job(job_id)
    if job is None:
        return JSONResponse({"error": "job not found"}, status_code=404)
    await queries.set_people_target(job_id)
    company = (job.get("company") or "").strip()
    # A plain LinkedIn people search: the company's own /people/ tab needs a
    # slug we do not store, and searching by name lands on the same faces.
    url = ("https://www.linkedin.com/search/results/people/?keywords="
           + quote(company)) if company else "https://www.linkedin.com/search/results/people/"

    # Opened here rather than by the page. The page tried `window.open` after an
    # await, which a native WKWebView ignores - so pressing the button did
    # nothing at all, three times in a row. And it could never have worked:
    # anything opened inside the app's own window has no Chrome extension in it,
    # and the extension is the whole point of this button. It has to be the
    # user's real browser.
    opened = False
    try:
        import webbrowser

        opened = webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not open the browser for people search: %s", exc)
    return JSONResponse({"ok": True, "company": company, "url": url, "opened": bool(opened)})


@app.get("/api/contacts/target")
async def contacts_target():
    """Which job the extension should attach the next batch of people to."""
    target = await queries.get_people_target()
    return JSONResponse(target or {})


@app.post("/api/contacts")
async def ingest_contacts(payload: ContactsPayload):
    """Receive people read off a LinkedIn page. Classification is free: audience
    and rank come from the headline, never from a model call."""
    job = await queries.get_job(payload.job_id)
    if job is None:
        return JSONResponse({"error": "job not found"}, status_code=404)
    stored = 0
    for person in payload.people:
        group = audience.classify(person.headline)
        rank = audience.relevance(group, person.headline, job.get("title"))
        contact_id = await queries.upsert_contact(
            job_id=payload.job_id,
            name=person.name.strip(),
            headline=(person.headline or "").strip() or None,
            profile_url=(person.profile_url or "").strip() or None,
            degree=person.degree,
            audience=group,
            rank=rank,
        )
        if contact_id is not None:
            stored += 1
    logger.info("Contacts: job=%s received=%d stored=%d",
                payload.job_id, len(payload.people), stored)
    return JSONResponse({"received": len(payload.people), "stored": stored})


@app.post("/contacts/{contact_id}/draft")
async def contact_draft(contact_id: int, kind: str = Form("message"),
                        brief: str = Form("")):
    """Generate one outreach draft. The single model call in this whole feature."""
    contact = await queries.get_contact(contact_id)
    if contact is None:
        return JSONResponse({"error": "contact not found"}, status_code=404)
    job = await queries.get_job(contact["job_id"])
    profile = await queries.get_profile()
    app_settings = await queries.get_app_settings()
    generator = gen.CopilotGenerator(model=_write_model(app_settings),
                                     **_ai(app_settings, providers.WRITE))
    kind = "note" if kind == "note" else "message"
    text = await generator.outreach(contact, job, profile, kind, brief)
    await queries.save_contact_draft(contact_id, kind, text)
    return JSONResponse({"ok": True, "kind": kind, "text": text, "chars": len(text)})


@app.post("/contacts/{contact_id}/status")
async def contact_status(request: Request, contact_id: int,
                         status: str = Form(...), spent_note: str = Form("")):
    valid = ("found", "invited", "accepted", "messaged", "replied", "referred", "archived")
    if status not in valid:
        return JSONResponse({"error": "bad status"}, status_code=400)
    await queries.set_contact_status(contact_id, status, spent_note=bool(spent_note))
    contact = await queries.get_contact(contact_id)
    return RedirectResponse(f"/jobs/{contact['job_id']}#people" if contact else "/",
                            status_code=303)


@app.post("/contacts/{contact_id}/delete")
async def contact_delete(contact_id: int):
    contact = await queries.get_contact(contact_id)
    job_id = contact["job_id"] if contact else None
    await queries.delete_contact(contact_id)
    return RedirectResponse(f"/jobs/{job_id}#people" if job_id else "/", status_code=303)


# --------------------------------------------------------------------------- #
# Statistic                                                                    #
# --------------------------------------------------------------------------- #

@app.get("/statistic", response_class=HTMLResponse)
async def statistic_page(request: Request):
    app_settings = await queries.get_app_settings()
    threshold = int(app_settings.get("copilot_min_score") or 60)
    stats = await queries.get_statistics(threshold)
    # The funnel, in process order, with the count that reached each stage.
    # "Reached" and not "sits at": someone now at Offer did pass the HR round,
    # and a funnel that forgets that reads as though nobody ever gets anywhere.
    by_stage = stats.get("by_stage", {})
    reached, running = [], 0
    for name in reversed(pipeline.FUNNEL):
        running += by_stage.get(name, 0)
        reached.append((name, pipeline.LABELS[name], running))
    reached.reverse()
    # What the models actually charged for, as they reported it. Empty until a
    # scan runs on this build, because nothing recorded it before.
    spend = await usage.totals(30)
    return templates.TemplateResponse(
        request, "statistic.html",
        {"active": "statistic", "stats": stats, "threshold": threshold,
         "spend": spend, "spend_prices": {k: v.price for k, v in providers.PROVIDERS.items()},
         "funnel": reached,
         "rejected": stats.get("rejected", 0),
         "stage_labels": pipeline.LABELS},
    )


# --------------------------------------------------------------------------- #
# Settings                                                                     #
# --------------------------------------------------------------------------- #

def _settings_context(app_settings: dict) -> dict:
    """Everything the Settings page needs to render both services' worlds.

    All of it, both services, in one render - the page switches model lists in
    the browser. It used to re-render with one role's choice in the query
    string, which is how choosing a writer silently reverted the scanner."""
    row = dict(app_settings)
    provider, key = providers.credentials(row)
    return {
        "settings": app_settings,
        "timezone": local_timezone_name(),
        "timezones": available_timezones(),
        # Every service's model list, so changing the selector swaps the models
        # in place. It used to reload the page with the choice in the query
        # string, which threw away the OTHER role's unsaved choice - picking a
        # writer reverted the scanner, and the two looked like a forbidden
        # combination - and jumped the reader back to the top of the page.
        "provider_data": json.dumps({
            p.key: {"label": p.label, "models": list(p.models),
                    "console": p.console, "leaves_eu": p.leaves_eu}
            for p in providers.PROVIDERS.values()
        }),
        "provider": provider,
        "provider_key": key,
        "provider_list": list(providers.PROVIDERS.values()),
        "model_fast": providers.resolve_model(row, "fast"),
        "model_deep": providers.resolve_model(row, "deep"),
        "writer": providers.provider_for(row, providers.WRITE),
        "writer_key": (row.get(providers.key_column(
            providers.provider_for(row, providers.WRITE))) or "").strip(),
        "model_write": providers.resolve_model(row, "write"),
    }


@app.get("/settings", response_class=HTMLResponse)
async def settings_form(request: Request):
    app_settings = await queries.get_app_settings()
    return templates.TemplateResponse(
        request, "settings.html",
        {"active": "settings", "saved": False, **_settings_context(app_settings)},
    )


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(
    request: Request,
    openai_model_fast: str = Form("gpt-4o-mini"),
    openai_model_advanced: str = Form("gpt-4.1"),
    copilot_min_score: int = Form(60),
    scan_times: str = Form(""),
    scan_days: list[str] = Form(default=[]),
    scan_timezone: str = Form(""),
    daily_apply_goal: int = Form(5),
    openai_api_key: str = Form(""),
    ai_provider: str = Form("openai"),
    ai_api_key: str = Form(""),
    writer_provider: str = Form("openai"),
    writer_model: str = Form(""),
    writer_api_key: str = Form(""),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    telegram_enabled: str = Form(""),
):
    copilot_min_score = max(0, min(100, copilot_min_score))
    daily_apply_goal = max(1, min(50, daily_apply_goal))
    # Normalise the times string to clean "HH:MM, HH:MM" and the weekday list.
    from scheduler import jobs as _sched
    times = _sched.parse_scan_times(scan_times)
    scan_times_norm = ", ".join(f"{h:02d}:{m:02d}" for h, m in times)
    scan_days_norm = _sched.parse_scan_days(",".join(scan_days))
    # `default_max_jobs` left the Settings UI - the per-search cap is set on the
    # Searches page. Preserve whatever is stored so a settings save does not
    # silently reset it.
    current = await queries.get_app_settings()
    await queries.save_app_settings(
        # Fixed-interval scanning was removed from the UI - scheduled scans are
        # driven only by the times below, so the legacy column is zeroed out
        # rather than left as an invisible trigger the user can't see or change.
        scan_interval_minutes=0,
        default_max_jobs=int(current.get("default_max_jobs") or 50),
        openai_model=openai_model_fast.strip() or "gpt-4o-mini",  # legacy col = fast
        copilot_min_score=copilot_min_score,
        scan_times=scan_times_norm,
        scan_days=scan_days_norm,
        daily_apply_goal=daily_apply_goal,
        # An empty string means "this computer's clock". A name the machine
        # cannot resolve is not stored at all, so a typo cannot quietly move
        # every scheduled scan to another hour.
        scan_timezone=(scan_timezone.strip()
                       if resolve_timezone(scan_timezone) is not None else ""),
    )
    await queries.save_ai_models(
        fast=openai_model_fast.strip() or "gpt-4o-mini",
        advanced=openai_model_advanced.strip() or "gpt-4.1",
    )
    # Only overwrite a key when a new value is provided (blank = keep).
    if openai_api_key.strip():
        await queries.save_openai_api_key(openai_api_key)
    await queries.save_ai_provider(ai_provider, ai_api_key)
    await queries.save_writer(writer_provider, writer_model, writer_api_key)
    await queries.save_telegram_settings(
        token=telegram_bot_token,          # blank keeps the stored token
        chat_id=telegram_chat_id,
        enabled=bool(telegram_enabled),
    )
    # Apply the new schedule to the running scheduler immediately.
    app_settings = await queries.get_app_settings()
    configure_scan_jobs(getattr(app.state, "scheduler", None), app_settings)
    return templates.TemplateResponse(
        request, "settings.html",
        {"active": "settings", "saved": True, **_settings_context(app_settings)},
    )


@app.post("/api/telegram/test")
async def telegram_test(token: str = Form(""), chat_id: str = Form("")):
    """Probe the Telegram credentials typed into the Settings form.

    The values are saved first (blank token = keep the stored one) so the test
    exercises exactly what the app will use later."""
    from telegram_bot import send_test

    current = await queries.get_app_settings()
    await queries.save_telegram_settings(
        token=token,
        chat_id=chat_id or str(current.get("telegram_chat_id") or ""),
        enabled=bool(current.get("telegram_enabled")),
    )
    ok, message = await send_test()
    return JSONResponse({"ok": ok, "message": message})


# --------------------------------------------------------------------------- #
# Manual re-score / notify trigger                                            #
# --------------------------------------------------------------------------- #

@app.post("/run")
async def run_now(background_tasks: BackgroundTasks):
    # Streaming cycle: fetch + score jobs one at a time so they appear
    # progressively. Guarded against overlapping runs inside run_scan_cycle.
    background_tasks.add_task(run_scan_cycle)
    return RedirectResponse(url="/", status_code=303)


@app.post("/stop")
async def stop_scan():
    # Graceful stop - halts after the current job; already-found jobs are kept.
    request_stop()
    return RedirectResponse(url="/", status_code=303)




# --------------------------------------------------------------------------- #
# Diagnostics - tells you exactly why scoring fails                           #
# --------------------------------------------------------------------------- #

@app.get("/api/scan-status")
async def scan_status():
    """Live scan state for the header 'Scan now' button (polled by the UI)."""
    done, total = get_scan_progress()
    return JSONResponse({
        "scanning": is_scanning(), "done": done, "total": total,
        "last": get_last_scan(),
    })


@app.get("/api/version")
async def version():
    """Current app version + update availability (used by the desktop shell)."""
    ver = "dev"
    try:
        from desktop.version import __version__ as ver  # type: ignore
    except Exception:
        pass
    info = {"version": ver, "update": None, "repo": ""}
    try:
        from desktop import updater
        info["repo"] = updater.GITHUB_REPO
        latest = updater.update_available()
        if latest:
            info["update"] = {"version": latest["version"]}
    except Exception:
        pass
    return JSONResponse(info)


@app.get("/api/diag")
async def diag():
    """Live check of the OpenAI setup. Open http://127.0.0.1:8000/api/diag
    to see whether the key is loaded and a real scoring call succeeds."""
    app_settings = await queries.get_app_settings()
    provider, db_key = providers.credentials(app_settings)
    info: dict[str, object] = {
        "provider": provider.label,
        "endpoint": provider.base_url or "api.openai.com (default)",
        "openai_key_loaded": bool(db_key or settings.openai_api_key),
        "openai_key_source": "settings UI" if db_key else ("env" if settings.openai_api_key else "none"),
        "model_fast": _fast_model(app_settings),
        "model_advanced": _deep_model(app_settings),
        "advanced_runs_on": providers.provider_for(app_settings, providers.WRITE).label,
    }
    # Diagnoses the SCANNING endpoint, which is the one a scan depends on.
    matcher = AIMatcher(model=_fast_model(app_settings),
                        **_ai(app_settings))
    if matcher.init_error:
        info["ok"] = False
        info["error"] = matcher.init_error
        return JSONResponse(info)

    # Minimal real quick-score call to surface any API/network/model error.
    result = await matcher.quick_score(
        {
            "id": 0,
            "title": "Test Engineer",
            "company": "Diag",
            "location": "Remote",
            "source": "diag",
            "description": "Python, FastAPI.",
        },
        "Looking for a remote Python role.",
    )
    ok = "scoring_error" not in result.red_flags and "failed" not in result.summary.lower()
    info["ok"] = ok
    info["sample_score"] = result.score
    info["sample_summary"] = result.summary
    if not ok:
        info["error"] = result.summary
    return JSONResponse(info)


if __name__ == "__main__":
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)

"""SQLite schema and initialization.

Schema is intentionally simple: two tables - `jobs` (scraped + scored vacancies)
and `profile` (a single-row user profile edited via the web UI).

Status lifecycle for a job:
    new -> scored -> notified -> viewed -> archived
"""

from __future__ import annotations

import logging


from config import settings
from db import conn

logger = logging.getLogger(__name__)

# Allowed values for jobs.status. Kept here so other modules import the source
# of truth instead of hardcoding strings (no magic values).
JOB_STATUSES: tuple[str, ...] = (
    "new",
    "scored",
    "notified",
    "viewed",
    "archived",
)

# Single-row profile always uses this id.
PROFILE_ID: int = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT    NOT NULL,
    external_id   TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    company       TEXT,
    location      TEXT,
    description   TEXT,
    url           TEXT,
    posted_at     TEXT,
    scraped_at    TEXT    NOT NULL,
    score         INTEGER,
    match_summary TEXT,
    red_flags     TEXT,
    status        TEXT    NOT NULL DEFAULT 'new',
    UNIQUE(source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score  ON jobs(score);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);

CREATE TABLE IF NOT EXISTS profile (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    resume_text  TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    case_bank    TEXT NOT NULL DEFAULT '',
    updated_at   TEXT
);

-- Named search profiles edited via the /search UI. Each row is one saved
-- search (e.g. "Product Operations", "Project Management"); the scheduler runs
-- every enabled search each cycle. Mirrors the LinkedIn job-search filters.
CREATE TABLE IF NOT EXISTS searches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL DEFAULT 'Untitled search',
    keywords      TEXT    NOT NULL DEFAULT '',
    location      TEXT    NOT NULL DEFAULT '',
    tpr_seconds   INTEGER NOT NULL DEFAULT 86400,   -- date posted (f_TPR)
    experience    TEXT    NOT NULL DEFAULT '',       -- csv f_E: 1..6
    job_types     TEXT    NOT NULL DEFAULT '',       -- csv f_JT: F,P,C,T,I,V,O
    remote        TEXT    NOT NULL DEFAULT '',       -- csv f_WT: 1,2,3
    source        TEXT    NOT NULL DEFAULT 'linkedin', -- data source
    max_jobs      INTEGER NOT NULL DEFAULT 50,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT,
    updated_at    TEXT
);

-- Single-row application settings, edited via the /settings UI.
CREATE TABLE IF NOT EXISTS app_settings (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    scan_interval_minutes INTEGER NOT NULL DEFAULT 30,
    default_max_jobs      INTEGER NOT NULL DEFAULT 50,
    openai_model          TEXT    NOT NULL DEFAULT 'gpt-4o-mini',
    copilot_min_score     INTEGER NOT NULL DEFAULT 60,  -- "good match" threshold
    updated_at            TEXT
);

-- Case bank as individual stories (added one by one via the Profile UI).
-- Used only for the on-demand advanced check + copilot, never for quick scoring.
CREATE TABLE IF NOT EXISTS cases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    created_at TEXT
);

-- People worth contacting about a specific job, read from the company's own
-- LinkedIn people page by the extension. `degree` (1/2/3) decides the whole
-- route: 1st can be messaged directly, 2nd needs an invite first, 3rd cannot be
-- reached at all without Premium. `note` is the 200-character invite text (a
-- scarce resource — free accounts get ~5 a month) and `message` is the full
-- text sent once they accept. Nothing here is ever sent by the app.
CREATE TABLE IF NOT EXISTS contacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER NOT NULL,
    name         TEXT    NOT NULL,
    headline     TEXT,
    profile_url  TEXT,
    degree       INTEGER,                       -- 1, 2, 3; NULL = unknown
    audience     TEXT    NOT NULL DEFAULT 'other',
    rank         INTEGER NOT NULL DEFAULT 0,    -- 3 = closest to the decision
    status       TEXT    NOT NULL DEFAULT 'found',
    note         TEXT,                          -- 200-char invite note
    message      TEXT,                          -- full message
    note_sent_at TEXT,                          -- set when a note is spent
    created_at   TEXT,
    updated_at   TEXT,
    UNIQUE(job_id, profile_url)
);

CREATE INDEX IF NOT EXISTS idx_contacts_job ON contacts(job_id);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);

-- Generated career-copilot assets per job (cover letter, HR questions,
-- interview prep). One row per (job, kind); regenerating replaces it.
CREATE TABLE IF NOT EXISTS job_assets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at TEXT,
    UNIQUE(job_id, kind)
);
"""

# Columns added after the initial release - applied via lightweight migration
# so existing databases pick them up (SQLite lacks ADD COLUMN IF NOT EXISTS).
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("jobs", "favorite", "INTEGER NOT NULL DEFAULT 0"),
    ("jobs", "strengths", "TEXT"),
    ("jobs", "weaknesses", "TEXT"),
    ("searches", "source", "TEXT NOT NULL DEFAULT 'linkedin'"),
    # Whether the on-demand deep analysis (CV + case bank) has been run.
    ("jobs", "advanced", "INTEGER NOT NULL DEFAULT 0"),
    # When the AI last checked (quick-scored) this job.
    ("jobs", "scored_at", "TEXT"),
    # Whether the user marked this job as applied (+ when, for weekly/daily stats).
    ("jobs", "applied", "INTEGER NOT NULL DEFAULT 0"),
    ("jobs", "applied_at", "TEXT"),
    # Manual override: user pinned this job into "Strong matches" regardless of
    # its AI score. Unlocks advanced check + copilot like a real strong match.
    ("jobs", "pinned", "INTEGER NOT NULL DEFAULT 0"),
    # Content fingerprint (normalised company + title) used to reject re-posts
    # and per-city clones of the same opening, which arrive with different ids.
    ("jobs", "dedup_key", "TEXT"),
    # How this job reached us: 'public' (server guest fetch) or 'extension'
    # (Chrome extension reading the logged-in session). Drives the card badge.
    ("jobs", "fetched_via", "TEXT NOT NULL DEFAULT 'public'"),
    # Optional daily scan times (comma-separated HH:MM). Empty = use interval.
    ("app_settings", "scan_times", "TEXT NOT NULL DEFAULT ''"),
    # Optional days of week for the scheduled times (csv mon..sun). Empty = every day.
    ("app_settings", "scan_days", "TEXT NOT NULL DEFAULT ''"),
    # Which zone the scan times are read in. Empty = this computer's clock,
    # which is right until the laptop travels or the user wants the scan to
    # follow the market they are applying to rather than where they are sitting.
    ("app_settings", "scan_timezone", "TEXT NOT NULL DEFAULT ''"),
    # The last time a digest could not be delivered, and why. A notification
    # that fails is worse than one that never existed: the scan ran, the jobs
    # are there, and the only sign was one line in a 21 MB log.
    ("app_settings", "telegram_error", "TEXT NOT NULL DEFAULT ''"),
    ("app_settings", "telegram_error_at", "TEXT"),
    ("app_settings", "telegram_sent_at", "TEXT"),
    # Editable CV document (HTML) rendered in the user's own template.
    ("profile", "cv_html", "TEXT"),
    # Pristine snapshot of the CV as uploaded/seeded - the "before" baseline for
    # tailored comparisons. Never mutated by per-bullet edits.
    ("profile", "cv_original_html", "TEXT"),
    # Target number of applications per day for the dashboard's Daily goal bar.
    ("app_settings", "daily_apply_goal", "INTEGER NOT NULL DEFAULT 5"),
    # Optional short title for each case bank story.
    ("cases", "title", "TEXT NOT NULL DEFAULT ''"),
    # OpenAI API key entered via the Settings UI (overrides the .env value).
    ("app_settings", "openai_api_key", "TEXT NOT NULL DEFAULT ''"),
    # Two-model setup: a cheap FAST model for scanning (title screen + quick
    # score) and a smarter DEEP model for advanced check + copilot generation.
    ("app_settings", "openai_model_fast", "TEXT NOT NULL DEFAULT 'gpt-4o-mini'"),
    ("app_settings", "openai_model_advanced", "TEXT NOT NULL DEFAULT 'gpt-4.1'"),
    # Telegram digest. Configured in the UI so the desktop app needs no .env.
    ("app_settings", "telegram_bot_token", "TEXT NOT NULL DEFAULT ''"),
    ("app_settings", "telegram_chat_id", "TEXT NOT NULL DEFAULT ''"),
    ("app_settings", "telegram_enabled", "INTEGER NOT NULL DEFAULT 0"),
    # Which job the extension should attach the next batch of people to. The
    # extension cannot read the app's page, so the app parks the target here and
    # the extension asks for it over the HTTP channel it already uses.
    ("app_settings", "people_target_job_id", "INTEGER NOT NULL DEFAULT 0"),
    # How far an application actually got. Counting applications and nothing
    # after them left the only question that matters - does the filtering
    # produce more interviews - with no answer. See pipeline.py for the stages.
    ("jobs", "stage", "TEXT NOT NULL DEFAULT 'applied'"),
    ("jobs", "stage_at", "TEXT"),
    # The furthest stage this application ever reached. `stage` holds what is
    # true NOW, which after a rejection is "rejected" - and a funnel built on
    # that forgets every interview that ended in one, under-reporting the only
    # ratio worth measuring. This one only ever moves forward.
    ("jobs", "stage_best", "TEXT NOT NULL DEFAULT 'applied'"),
    # Letterhead the cover letter is printed on. HTML the user owns; see
    # letter_template.py for the placeholders it may use.
    ("profile", "letter_html", "TEXT"),
    # Which service the models are called on. Everything speaks the OpenAI wire
    # format, so this is a base URL and a key - see providers.py.
    ("app_settings", "ai_provider", "TEXT NOT NULL DEFAULT 'openai'"),
    # Each provider keeps its own key, so switching back does not mean pasting
    # the previous one again.
    ("app_settings", "deepseek_api_key", "TEXT NOT NULL DEFAULT ''"),
    # Writing is a separate choice from scanning. Scanning is judgement at
    # volume and belongs on whatever endpoint bills a cached token cheapest;
    # cover letters, CV edits and HR answers are read by a person and sent to an
    # employer, and cost nothing at that volume. Defaults to OpenAI, so an
    # existing install that moves scanning to DeepSeek keeps writing where it is.
    ("app_settings", "writer_provider", "TEXT NOT NULL DEFAULT 'openai'"),
    ("app_settings", "writer_model", "TEXT NOT NULL DEFAULT ''"),
    # Full-time / Contract / Internship, straight from the source's own field.
    ("jobs", "employment_type", "TEXT"),
    # What the extension last told us about itself. The app cannot see inside
    # Chrome, so an extension that pauses itself after a run of empty scans just
    # goes quiet - the Jobs page looks normal and nothing arrives for days. The
    # extension reports its state on the schedule poll it already makes; this is
    # where that lands. Persisted rather than kept in memory because the app
    # restarts far more often than the extension changes its mind.
    ("app_settings", "extension_state", "TEXT NOT NULL DEFAULT ''"),
    ("app_settings", "extension_error", "TEXT NOT NULL DEFAULT ''"),
    ("app_settings", "extension_seen_at", "TEXT"),
    ("app_settings", "extension_run_at", "TEXT"),
    ("app_settings", "extension_version", "TEXT NOT NULL DEFAULT ''"),
    # Google Gemini, the third provider. Its own key column like DeepSeek's, so
    # switching between services never means pasting a key again.
    ("app_settings", "gemini_api_key", "TEXT NOT NULL DEFAULT ''"),
    # Years of experience the candidate has for the kind of role a search looks
    # for, and how many years short of an advert's requirement still counts as a
    # fit. Per search, because one person can have eight years in one field and
    # two in the next. NULL = not stated, and then years change nothing.
    ("searches", "years_experience", "INTEGER"),
    ("searches", "years_margin", "INTEGER NOT NULL DEFAULT 1"),
    # The search that found the job, so scoring can read that search's years.
    # NULL for jobs from the extension, which scans its own URLs, and for every
    # job stored before this column existed. A job found again by a second
    # search is a duplicate and keeps its first finder.
    ("jobs", "search_id", "INTEGER"),
)


async def _ensure_column(db, table: str, column: str, decl: str) -> None:
    cur = await db.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in await cur.fetchall()}
    if column not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        logger.info("Migration: added %s.%s", table, column)


async def init_db() -> None:
    """Create tables/indexes if they do not exist and seed an empty profile row."""
    db_path = settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with conn.connect(db_path) as db:
        # First, and once: WAL is a property of the file, so setting it here
        # covers every connection the app opens afterwards.
        await conn.apply_pragmas(db)
        await db.executescript(_SCHEMA)
        from db.usage import SCHEMA as _USAGE_SCHEMA

        await db.executescript(_USAGE_SCHEMA)
        for table, column, decl in _MIGRATIONS:
            await _ensure_column(db, table, column, decl)
        # Indexed after the migration above adds the column.
        await db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_dedup ON jobs(dedup_key)")
        # Ensure the single profile row exists so the UI can always UPDATE it.
        await db.execute(
            "INSERT OR IGNORE INTO profile (id, resume_text, instructions, case_bank) "
            "VALUES (?, '', '', '')",
            (PROFILE_ID,),
        )
        # Seed app settings from .env defaults on first run.
        await db.execute(
            """INSERT OR IGNORE INTO app_settings
                   (id, scan_interval_minutes, default_max_jobs, openai_model)
               VALUES (?, ?, ?, ?)""",
            (
                PROFILE_ID,
                settings.scrape_interval_minutes,
                settings.max_jobs_per_cycle,
                settings.openai_model,
            ),
        )
        # Note: `searches` starts empty on purpose - the UI shows a
        # "Craft your first search" empty state until the user adds one.
        await db.commit()

    logger.info("Database initialized at %s", db_path)

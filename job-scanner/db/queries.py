"""All database operations live here - no raw SQL anywhere else.

Every function opens its own short-lived connection via aiosqlite. For a local
single-user tool this is simple and safe; connection pooling is unnecessary.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from db import conn
from db.models import PROFILE_ID
from scrapers.base import JobListing

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    data = dict(row)
    # JSON-encoded list columns; decode for convenience.
    for col in ("red_flags", "strengths", "weaknesses"):
        raw = data.get(col)
        if raw:
            try:
                data[col] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                data[col] = []
        else:
            data[col] = []
    return data


# A job as scoring reads it: the row plus the years its search states. Every
# path that hands a job to the matcher loads it through here, so the years gate
# cannot hold in one path and be skipped in another. A LEFT JOIN, so a job with
# no search - or whose search was deleted - reads as "years not stated".
_JOB_WITH_SEARCH = (
    "SELECT jobs.*, s.years_experience AS search_years, "
    "s.years_margin AS search_years_margin "
    "FROM jobs LEFT JOIN searches s ON s.id = jobs.search_id"
)


# --------------------------------------------------------------------------- #
# Jobs                                                                         #
# --------------------------------------------------------------------------- #

async def job_exists(source: str, external_id: str) -> bool:
    """Dedup check by (source, external_id)."""
    async with conn.connect() as db:
        cur = await db.execute(
            "SELECT 1 FROM jobs WHERE source = ? AND external_id = ? LIMIT 1",
            (source, external_id),
        )
        return await cur.fetchone() is not None


# Noise stripped from titles before fingerprinting: gender markers, seniority
# decorations that vary between reposts, and bracketed asides.
_TITLE_NOISE = re.compile(
    r"\((?:[^()]*)\)|\[[^\]]*\]|\bm/w/d\b|\bm/f/d\b|\bw/m/d\b|\bh/f\b|\bf/m/x\b",
    re.IGNORECASE,
)
_NON_WORD = re.compile(r"[^a-z0-9]+")


def _norm(text: str | None) -> str:
    """Lowercase, de-accent and strip punctuation so cosmetic differences
    ('Sr.' vs 'Sr', 'Müller' vs 'Muller') don't defeat the comparison."""
    s = unicodedata.normalize("NFKD", (text or "")).encode("ascii", "ignore").decode()
    return _NON_WORD.sub(" ", s.lower()).strip()


def dedup_key(title: str | None, company: str | None) -> str:
    """Content fingerprint for a vacancy: normalised company + title.

    The same opening is routinely posted many times - once per city, re-posted
    weekly, or mirrored across boards - each time with a *different* external
    id, so UNIQUE(source, external_id) does not catch it. Location is
    deliberately NOT part of the key: 'HR Generalist @ MOD Education' in six
    cities is one job to apply to, not six.

    Returns '' when either half is missing - without a company name, an
    identical title says nothing about it being the same job."""
    company_norm = _norm(company)
    title_norm = _norm(_TITLE_NOISE.sub(" ", title or ""))
    if not company_norm or not title_norm:
        return ""
    return f"{company_norm}|{title_norm}"


async def duplicate_exists(title: str | None, company: str | None) -> bool:
    """Whether a job with the same content fingerprint is already stored.
    Checked before scoring so duplicates never cost OpenAI tokens."""
    key = dedup_key(title, company)
    if not key:
        return False
    async with conn.connect() as db:
        cur = await db.execute("SELECT 1 FROM jobs WHERE dedup_key = ? LIMIT 1", (key,))
        return await cur.fetchone() is not None


async def insert_job(job: JobListing, fetched_via: str = "public",
                     search_id: int | None = None) -> int | None:
    """Insert a scraped job. Returns the new row id, or None if it was a duplicate.
    `fetched_via` records the origin ('public' server fetch vs 'extension');
    `search_id` the saved search that found it, whose years scoring reads."""
    key = dedup_key(job.title, job.company)
    async with conn.connect() as db:
        # Content-level dedup (re-posts / per-city clones of the same opening).
        if key:
            cur = await db.execute(
                "SELECT 1 FROM jobs WHERE dedup_key = ? LIMIT 1", (key,)
            )
            if await cur.fetchone() is not None:
                return None
        try:
            cur = await db.execute(
                """
                INSERT INTO jobs
                    (source, external_id, title, company, location,
                     description, url, posted_at, scraped_at, status, fetched_via,
                     dedup_key, employment_type, search_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)
                """,
                (
                    job.source,
                    job.external_id,
                    job.title,
                    job.company,
                    job.location,
                    job.description,
                    job.url,
                    job.posted_at,
                    _utc_now_iso(),
                    fetched_via,
                    key,
                    getattr(job, "employment_type", None),
                    search_id,
                ),
            )
            await db.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            # UNIQUE(source, external_id) violated -> duplicate, skip silently.
            return None


async def backfill_dedup_keys() -> int:
    """Fill dedup_key for rows stored before content dedup existed."""
    async with conn.connect() as db:
        # NULL only: rows that legitimately have no fingerprint are stored as ''
        # so this is a one-time pass, not work repeated on every startup.
        cur = await db.execute("SELECT id, title, company FROM jobs WHERE dedup_key IS NULL")
        rows = await cur.fetchall()
        for job_id, title, company in rows:
            await db.execute(
                "UPDATE jobs SET dedup_key = ? WHERE id = ?",
                (dedup_key(title, company), job_id),
            )
        await db.commit()
        return len(rows)


async def repair_doubled_titles() -> int:
    """Fix titles a stale extension stored twice over. Returns rows repaired.

    The fingerprint is derived from the title, so it is recomputed here - a
    repaired row must land in the same dedup group as a correctly-read copy of
    the same job."""
    from scrapers.base import clean_title

    async with conn.connect() as db:
        cur = await db.execute("SELECT id, title, company FROM jobs")
        fixed = 0
        for job_id, title, company in await cur.fetchall():
            clean = clean_title(title)
            if clean and clean != (title or ""):
                await db.execute(
                    "UPDATE jobs SET title = ?, dedup_key = ? WHERE id = ?",
                    (clean, dedup_key(clean, company), job_id),
                )
                fixed += 1
        await db.commit()
        return fixed


async def purge_duplicates() -> int:
    """Collapse existing duplicate groups to a single row. Returns rows removed.

    The keeper is the most valuable copy: anything applied/saved/pinned wins,
    then the highest score, then the oldest row. Copies that are themselves
    applied/saved/pinned are never deleted."""
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, dedup_key, COALESCE(score, -1) AS score,
                   COALESCE(applied, 0) + COALESCE(favorite, 0) + COALESCE(pinned, 0) AS marked
              FROM jobs
             WHERE dedup_key IS NOT NULL AND dedup_key != ''
             ORDER BY dedup_key, marked DESC, score DESC, id ASC
            """
        )
        rows = await cur.fetchall()
        doomed: list[int] = []
        seen: set[str] = set()
        for row in rows:
            key = row["dedup_key"]
            if key not in seen:
                seen.add(key)          # first row per key = the keeper
                continue
            if row["marked"]:
                continue               # never drop a job the user acted on
            doomed.append(row["id"])
        if not doomed:
            return 0
        marks = ",".join("?" * len(doomed))
        await db.execute(f"DELETE FROM job_assets WHERE job_id IN ({marks})", doomed)
        await db.execute(f"DELETE FROM jobs WHERE id IN ({marks})", doomed)
        await db.commit()
        return len(doomed)


async def get_new_jobs() -> list[dict[str, Any]]:
    """Jobs that have been scraped but not yet scored."""
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"{_JOB_WITH_SEARCH} WHERE jobs.status = 'new' ORDER BY jobs.scraped_at DESC"
        )
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def save_quick_score(
    job_id: int,
    score: int,
    match_summary: str,
    red_flags: list[str],
) -> None:
    """Stage-1 result: instructions-only quick score. Moves job to 'scored'."""
    async with conn.connect() as db:
        await db.execute(
            """
            UPDATE jobs
               SET score = ?, match_summary = ?, red_flags = ?,
                   scored_at = ?, status = 'scored'
             WHERE id = ?
            """,
            (
                score,
                match_summary,
                json.dumps(red_flags, ensure_ascii=False),
                _utc_now_iso(),
                job_id,
            ),
        )
        await db.commit()


async def update_job_description(job_id: int, description: str) -> None:
    async with conn.connect() as db:
        await db.execute(
            "UPDATE jobs SET description = ? WHERE id = ?", (description, job_id)
        )
        await db.commit()


async def save_advanced(
    job_id: int,
    strengths: list[str],
    weaknesses: list[str],
    score: int | None = None,
) -> None:
    """Stage-2 result: deep analysis (CV + case bank). Sets advanced = 1 and,
    if a refined score is given, updates the job score too."""
    async with conn.connect() as db:
        if score is None:
            await db.execute(
                "UPDATE jobs SET strengths = ?, weaknesses = ?, advanced = 1 WHERE id = ?",
                (
                    json.dumps(strengths, ensure_ascii=False),
                    json.dumps(weaknesses, ensure_ascii=False),
                    job_id,
                ),
            )
        else:
            await db.execute(
                "UPDATE jobs SET strengths = ?, weaknesses = ?, advanced = 1, "
                "score = ?, scored_at = ? WHERE id = ?",
                (
                    json.dumps(strengths, ensure_ascii=False),
                    json.dumps(weaknesses, ensure_ascii=False),
                    score,
                    _utc_now_iso(),
                    job_id,
                ),
            )
        await db.commit()


async def mark_notified(job_ids: list[int]) -> None:
    if not job_ids:
        return
    placeholders = ",".join("?" * len(job_ids))
    async with conn.connect() as db:
        await db.execute(
            f"UPDATE jobs SET status = 'notified' WHERE id IN ({placeholders})",
            job_ids,
        )
        await db.commit()


def _job_filters(
    min_score: int | None,
    max_score: int | None,
    scored_only: bool,
    applied: int | None,
    below_or_unscored: int | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if scored_only:
        clauses.append("score IS NOT NULL")
    if min_score is not None:
        clauses.append("score >= ?")
        params.append(min_score)
    if max_score is not None:
        clauses.append("score < ?")
        params.append(max_score)
    if below_or_unscored is not None:
        # Everything not in "review": below threshold OR not yet scored, so
        # nothing ever becomes invisible.
        clauses.append("(score IS NULL OR score < ?)")
        params.append(below_or_unscored)
    if applied is not None:
        clauses.append("COALESCE(applied, 0) = ?")
        params.append(applied)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


# Newest first, everywhere. The score is shown on each row as a label, so the
# list reads as a feed of what just came in; `id DESC` breaks ties within the
# same scan (scraped_at has second resolution). NOTE: this also decides WHICH
# rows survive `limit` - the most recent ones, not the highest scoring.
_RECENT_FIRST = "ORDER BY scraped_at DESC, id DESC"
# Applied tab: most recently applied first (older rows may predate applied_at).
_APPLIED_FIRST = "ORDER BY COALESCE(applied_at, scraped_at) DESC, id DESC"

# Sort orders offered in the UI. Every one ends in a tie-breaker, or rows with
# equal scores shuffle between page loads and the list looks unstable.
#
# `score IS NULL` first is redundant on SQLite, which already sorts NULL last
# under DESC - it is spelled out so the intent survives someone flipping the
# direction or moving the query to an engine that orders NULLs the other way.
SORTS: dict[str, str] = {
    "recent": _RECENT_FIRST,
    "applied": _APPLIED_FIRST,
    "score": "ORDER BY score IS NULL, score DESC, scraped_at DESC, id DESC",
    "title": "ORDER BY title COLLATE NOCASE ASC, id DESC",
    "company": "ORDER BY company COLLATE NOCASE ASC, title COLLATE NOCASE ASC, id DESC",
}


def _order_by(sort: str | None, default: str = "recent") -> str:
    return SORTS.get(sort or "", SORTS[default])


async def list_jobs(
    min_score: int | None = None,
    max_score: int | None = None,
    scored_only: bool = False,
    applied: int | None = None,
    below_or_unscored: int | None = None,
    limit: int | None = None,
    order: str = "recent",
) -> list[dict[str, Any]]:
    """Filterable listing for the web UI. `limit` caps rows (keeps big lists
    fast to render). `order` is a key of SORTS."""
    where, params = _job_filters(min_score, max_score, scored_only, applied, below_or_unscored)
    order_by = _order_by(order)
    query = f"SELECT * FROM jobs {where} {order_by}"
    if limit is not None:
        query += " LIMIT ?"
        params = [*params, int(limit)]
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def count_jobs(
    min_score: int | None = None,
    max_score: int | None = None,
    scored_only: bool = False,
    applied: int | None = None,
    below_or_unscored: int | None = None,
) -> int:
    where, params = _job_filters(min_score, max_score, scored_only, applied, below_or_unscored)
    async with conn.connect() as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM jobs {where}", params)
        return int((await cur.fetchone())[0])


# "Strong matches" = scored at/above the threshold OR manually pinned by the user.
_STRONG_WHERE = (
    "WHERE COALESCE(applied, 0) = 0 "
    "AND (score >= ? OR COALESCE(pinned, 0) = 1)"
)


async def list_strong_jobs(threshold: int, limit: int | None = None,
                           order: str = "recent") -> list[dict[str, Any]]:
    """Jobs at/above the match threshold OR pinned in by hand."""
    query = f"SELECT * FROM jobs {_STRONG_WHERE} {_order_by(order)}"
    params: list[Any] = [int(threshold)]
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def count_strong_jobs(threshold: int) -> int:
    async with conn.connect() as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM jobs {_STRONG_WHERE}", [int(threshold)])
        return int((await cur.fetchone())[0])


async def get_job(job_id: int) -> dict[str, Any] | None:
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(f"{_JOB_WITH_SEARCH} WHERE jobs.id = ?", (job_id,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def set_job_status(job_id: int, status: str) -> None:
    async with conn.connect() as db:
        await db.execute(
            "UPDATE jobs SET status = ? WHERE id = ?", (status, job_id)
        )
        await db.commit()


async def delete_job(job_id: int) -> None:
    """Permanently remove a job and its generated assets."""
    async with conn.connect() as db:
        await db.execute("DELETE FROM job_assets WHERE job_id = ?", (job_id,))
        await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await db.commit()


async def clear_jobs(
    min_score: int | None = None,
    max_score: int | None = None,
    keep_favorites: bool = True,
    keep_applied: bool = True,
    keep_pinned: bool = True,
    scored_only: bool = False,
) -> int:
    """Delete jobs within a score range (min inclusive, max exclusive).

    Clears ONE tab at a time: 'All jobs' or 'Strong matches'. The tabs the user
    curates by hand are independent of it - Saved, Applied and pinned jobs are
    never touched, because clearing a results list must not erase application
    history. Returns the number removed."""
    clauses: list[str] = []
    params: list[Any] = []
    if scored_only:
        clauses.append("score IS NOT NULL")
    if keep_favorites:
        clauses.append("COALESCE(favorite, 0) = 0")
    if keep_applied:
        clauses.append("COALESCE(applied, 0) = 0")
    if keep_pinned:
        clauses.append("COALESCE(pinned, 0) = 0")
    if min_score is not None:
        clauses.append("score >= ?")
        params.append(min_score)
    if max_score is not None:
        clauses.append("score < ?")
        params.append(max_score)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    async with conn.connect() as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM jobs {where}", params)
        n = int((await cur.fetchone())[0])
        await db.execute(
            f"DELETE FROM job_assets WHERE job_id IN (SELECT id FROM jobs {where})",
            params,
        )
        await db.execute(f"DELETE FROM jobs {where}", params)
        await db.commit()
        return n


async def toggle_favorite(job_id: int) -> int:
    """Flip a job's favorite flag. Returns the new value (0/1)."""
    async with conn.connect() as db:
        await db.execute(
            "UPDATE jobs SET favorite = 1 - COALESCE(favorite, 0) WHERE id = ?",
            (job_id,),
        )
        await db.commit()
        cur = await db.execute("SELECT favorite FROM jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def toggle_pinned(job_id: int) -> int:
    """Flip a job's 'pinned to Strong matches' flag. Returns the new value (0/1)."""
    async with conn.connect() as db:
        await db.execute(
            "UPDATE jobs SET pinned = 1 - COALESCE(pinned, 0) WHERE id = ?",
            (job_id,),
        )
        await db.commit()
        cur = await db.execute("SELECT pinned FROM jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def set_applied(job_id: int, applied: int) -> None:
    async with conn.connect() as db:
        await db.execute(
            "UPDATE jobs SET applied = ?, applied_at = ? WHERE id = ?",
            (applied, datetime.now(timezone.utc).isoformat() if applied else None, job_id),
        )
        await db.commit()


async def count_applied_since(iso_ts: str) -> int:
    """Jobs marked applied at/after the given ISO timestamp (UTC)."""
    async with conn.connect() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM jobs WHERE COALESCE(applied, 0) = 1 "
            "AND applied_at IS NOT NULL AND applied_at >= ?",
            (iso_ts,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def list_favorites(order: str = "recent") -> list[dict[str, Any]]:
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT * FROM jobs WHERE favorite = 1 {_order_by(order)}"
        )
        return [_row_to_dict(r) for r in await cur.fetchall()]


# --------------------------------------------------------------------------- #
# Profile                                                                      #
# --------------------------------------------------------------------------- #

async def get_profile() -> dict[str, Any]:
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM profile WHERE id = ?", (PROFILE_ID,)
        )
        row = await cur.fetchone()
        if row is None:
            return {"resume_text": "", "instructions": "", "case_bank": ""}
        return dict(row)


async def list_cases() -> list[dict[str, Any]]:
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM cases ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


async def add_case(text: str, title: str = "") -> None:
    async with conn.connect() as db:
        await db.execute(
            "INSERT INTO cases (text, title, created_at) VALUES (?, ?, ?)",
            (text.strip(), title.strip(), _utc_now_iso()),
        )
        await db.commit()


async def delete_case(case_id: int) -> None:
    async with conn.connect() as db:
        await db.execute("DELETE FROM cases WHERE id = ?", (case_id,))
        await db.commit()


async def save_cv_html(cv_html: str, set_original: bool = False) -> None:
    """Save the working CV. If `set_original`, also store it as the pristine
    baseline (used when uploading/replacing the whole CV)."""
    async with conn.connect() as db:
        if set_original:
            await db.execute(
                "UPDATE profile SET cv_html = ?, cv_original_html = ?, updated_at = ? WHERE id = ?",
                (cv_html, cv_html, _utc_now_iso(), PROFILE_ID),
            )
        else:
            await db.execute(
                "UPDATE profile SET cv_html = ?, updated_at = ? WHERE id = ?",
                (cv_html, _utc_now_iso(), PROFILE_ID),
            )
        await db.commit()


async def ensure_cv_original(fallback_html: str) -> str:
    """Return the pristine CV baseline, seeding it from `fallback_html` the first
    time (so existing users get a baseline captured from their current CV)."""
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT cv_original_html FROM profile WHERE id = ?", (PROFILE_ID,)
        )
        row = await cur.fetchone()
        current = (row["cv_original_html"] if row else None) or ""
        if current.strip():
            return current
        await db.execute(
            "UPDATE profile SET cv_original_html = ? WHERE id = ?",
            (fallback_html, PROFILE_ID),
        )
        await db.commit()
        return fallback_html


async def get_profile_full() -> dict[str, Any]:
    """Profile for the advanced check and copilot. The CV document is the single
    source of truth for the résumé, so resume_text is derived from it; case bank
    is assembled from the individual case stories."""
    from cv_content import cv_to_text

    profile = await get_profile()
    cv_html = (profile.get("cv_html") or "").strip()
    cv_text = cv_to_text(cv_html)
    if cv_text:
        profile["resume_text"] = cv_text
    cases = await list_cases()
    profile["case_bank"] = "\n".join(
        f"- {c['title'] + ': ' if c.get('title') else ''}{c['text']}" for c in cases
    )
    return profile


async def save_profile(
    resume_text: str, instructions: str, case_bank: str
) -> None:
    async with conn.connect() as db:
        await db.execute(
            """
            UPDATE profile
               SET resume_text = ?, instructions = ?, case_bank = ?, updated_at = ?
             WHERE id = ?
            """,
            (resume_text, instructions, case_bank, _utc_now_iso(), PROFILE_ID),
        )
        await db.commit()


# --------------------------------------------------------------------------- #
# Searches - multiple named search profiles (LinkedIn-style filters)          #
# --------------------------------------------------------------------------- #

async def list_searches() -> list[dict[str, Any]]:
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM searches ORDER BY id DESC")
        return [dict(r) for r in await cur.fetchall()]


async def list_enabled_searches() -> list[dict[str, Any]]:
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM searches WHERE enabled = 1 ORDER BY id"
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_search(search_id: int) -> dict[str, Any] | None:
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM searches WHERE id = ?", (search_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def create_search(
    name: str,
    keywords: str,
    location: str,
    tpr_seconds: int,
    experience: str,
    job_types: str,
    remote: str,
    max_jobs: int,
    source: str = "linkedin",
    years_experience: int | None = None,
    years_margin: int = 1,
) -> int:
    async with conn.connect() as db:
        cur = await db.execute(
            """
            INSERT INTO searches
                (name, keywords, location, tpr_seconds, experience, job_types,
                 remote, source, max_jobs, years_experience, years_margin,
                 enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                name,
                keywords,
                location,
                tpr_seconds,
                experience,
                job_types,
                remote,
                source,
                max_jobs,
                years_experience,
                years_margin,
                _utc_now_iso(),
                _utc_now_iso(),
            ),
        )
        await db.commit()
        return cur.lastrowid


async def update_search(
    search_id: int,
    name: str,
    keywords: str,
    location: str,
    tpr_seconds: int,
    experience: str,
    job_types: str,
    remote: str,
    max_jobs: int,
    source: str = "linkedin",
    enabled: int = 1,
    years_experience: int | None = None,
    years_margin: int = 1,
) -> None:
    async with conn.connect() as db:
        await db.execute(
            """
            UPDATE searches
               SET name = ?, keywords = ?, location = ?, tpr_seconds = ?,
                   experience = ?, job_types = ?, remote = ?, source = ?,
                   max_jobs = ?, years_experience = ?, years_margin = ?,
                   enabled = ?, updated_at = ?
             WHERE id = ?
            """,
            (
                name,
                keywords,
                location,
                tpr_seconds,
                experience,
                job_types,
                remote,
                source,
                max_jobs,
                years_experience,
                years_margin,
                enabled,
                _utc_now_iso(),
                search_id,
            ),
        )
        await db.commit()


async def delete_search(search_id: int) -> None:
    async with conn.connect() as db:
        await db.execute("DELETE FROM searches WHERE id = ?", (search_id,))
        await db.commit()


# --------------------------------------------------------------------------- #
# App settings                                                                 #
# --------------------------------------------------------------------------- #

_SETTINGS_DEFAULTS = {
    "scan_interval_minutes": 30,
    "default_max_jobs": 50,
    "scan_timezone": "",
    "openai_model": "gpt-4o-mini",
    "copilot_min_score": 60,
    "scan_times": "",
    "scan_days": "",
    "daily_apply_goal": 5,
    "openai_api_key": "",
    "openai_model_fast": "gpt-4o-mini",
    "openai_model_advanced": "gpt-4.1",
}


async def save_ai_models(fast: str, advanced: str) -> None:
    async with conn.connect() as db:
        await db.execute(
            "UPDATE app_settings SET openai_model_fast = ?, openai_model_advanced = ?, "
            "updated_at = ? WHERE id = ?",
            (fast.strip(), advanced.strip(), _utc_now_iso(), PROFILE_ID),
        )
        await db.commit()


async def save_openai_api_key(api_key: str) -> None:
    async with conn.connect() as db:
        await db.execute(
            "UPDATE app_settings SET openai_api_key = ?, updated_at = ? WHERE id = ?",
            (api_key.strip(), _utc_now_iso(), PROFILE_ID),
        )
        await db.commit()


async def record_telegram_result(error: str = "") -> None:
    """Remember whether the last digest arrived, so the app can say so.

    Both outcomes are stored: an error with nothing after it looks permanent,
    and a user who fixed their network deserves to see it clear itself."""
    now = _utc_now_iso()
    async with conn.connect() as db:
        if error:
            await db.execute(
                "UPDATE app_settings SET telegram_error = ?, telegram_error_at = ? "
                "WHERE id = 1", (error[:300], now))
        else:
            await db.execute(
                "UPDATE app_settings SET telegram_error = '', telegram_error_at = NULL, "
                "telegram_sent_at = ? WHERE id = 1", (now,))
        await db.commit()


async def get_app_settings() -> dict[str, Any]:
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM app_settings WHERE id = ?", (PROFILE_ID,)
        )
        row = await cur.fetchone()
        return dict(row) if row else dict(_SETTINGS_DEFAULTS)


async def save_app_settings(
    scan_interval_minutes: int,
    default_max_jobs: int,
    openai_model: str,
    copilot_min_score: int,
    scan_times: str = "",
    scan_days: str = "",
    daily_apply_goal: int = 5,
    scan_timezone: str = "",
) -> None:
    async with conn.connect() as db:
        await db.execute(
            """
            UPDATE app_settings
               SET scan_interval_minutes = ?, default_max_jobs = ?,
                   openai_model = ?, copilot_min_score = ?, scan_times = ?,
                   scan_days = ?, daily_apply_goal = ?, scan_timezone = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (
                scan_interval_minutes,
                default_max_jobs,
                openai_model,
                copilot_min_score,
                scan_times,
                scan_days,
                daily_apply_goal,
                scan_timezone,
                _utc_now_iso(),
                PROFILE_ID,
            ),
        )
        await db.commit()


async def count_new_jobs() -> int:
    async with conn.connect() as db:
        cur = await db.execute("SELECT COUNT(*) FROM jobs WHERE status = 'new'")
        row = await cur.fetchone()
        return int(row[0]) if row else 0


# --------------------------------------------------------------------------- #
# Job assets (career copilot: cover letter / HR questions / interview prep)   #
# --------------------------------------------------------------------------- #

async def get_assets(job_id: int) -> dict[str, str]:
    """All generated assets for a job as {kind: content}."""
    async with conn.connect() as db:
        cur = await db.execute(
            "SELECT kind, content FROM job_assets WHERE job_id = ?", (job_id,)
        )
        return {kind: content for kind, content in await cur.fetchall()}


async def save_asset(job_id: int, kind: str, content: str) -> None:
    """Insert or replace a generated asset (regenerating overwrites)."""
    async with conn.connect() as db:
        await db.execute(
            """
            INSERT INTO job_assets (job_id, kind, content, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(job_id, kind) DO UPDATE SET content = excluded.content,
                                                    created_at = excluded.created_at
            """,
            (job_id, kind, content, _utc_now_iso()),
        )
        await db.commit()


# --------------------------------------------------------------------------- #
# Statistics                                                                  #
# --------------------------------------------------------------------------- #

async def get_statistics(threshold: int) -> dict[str, Any]:
    """Everything the Statistic tab shows, in one pass over the DB.

    `scanned` counts what is stored NOW; jobs deleted by hand or collapsed as
    duplicates are gone, so this is the working set rather than a lifetime
    total - the tab says so."""
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row

        async def one(sql: str, params: tuple = ()) -> int:
            cur = await db.execute(sql, params)
            row = await cur.fetchone()
            return int(row[0] or 0)

        stats: dict[str, Any] = {
            "scanned": await one("SELECT COUNT(*) FROM jobs"),
            "scored": await one("SELECT COUNT(*) FROM jobs WHERE score IS NOT NULL"),
            "matches": await one(
                "SELECT COUNT(*) FROM jobs WHERE score >= ?", (threshold,)),
            "match80": await one("SELECT COUNT(*) FROM jobs WHERE score >= 80"),
            "applied": await one("SELECT COUNT(*) FROM jobs WHERE COALESCE(applied,0) = 1"),
            "saved": await one("SELECT COUNT(*) FROM jobs WHERE COALESCE(favorite,0) = 1"),
            "advanced": await one("SELECT COUNT(*) FROM jobs WHERE COALESCE(advanced,0) = 1"),
            "assets": await one("SELECT COUNT(*) FROM job_assets"),
            "searches": await one("SELECT COUNT(*) FROM searches WHERE enabled = 1"),
        }

        cur = await db.execute(
            "SELECT source, COUNT(*) n, SUM(CASE WHEN score >= ? THEN 1 ELSE 0 END) good "
            "FROM jobs GROUP BY source ORDER BY n DESC", (threshold,))
        stats["by_source"] = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            """SELECT CASE WHEN score >= 80 THEN '80-100'
                           WHEN score >= 60 THEN '60-79'
                           WHEN score >= 40 THEN '40-59'
                           WHEN score >= 20 THEN '20-39'
                           ELSE '0-19' END AS band, COUNT(*) n
                 FROM jobs WHERE score IS NOT NULL GROUP BY band ORDER BY band DESC""")
        stats["bands"] = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            "SELECT substr(applied_at, 1, 10) AS day, COUNT(*) n FROM jobs "
            "WHERE applied_at IS NOT NULL GROUP BY day ORDER BY day DESC LIMIT 14")
        stats["applied_by_day"] = [dict(r) for r in await cur.fetchall()]

        # The funnel counts how far each application GOT, not where it sits
        # today: a rejection after a tech interview is still a tech interview.
        cur = await db.execute(
            "SELECT COALESCE(stage_best, 'applied') AS stage, COUNT(*) n FROM jobs "
            "WHERE COALESCE(applied, 0) = 1 GROUP BY stage")
        stats["by_stage"] = {r["stage"]: int(r["n"]) for r in await cur.fetchall()}
        cur = await db.execute(
            "SELECT COUNT(*) FROM jobs WHERE COALESCE(applied, 0) = 1 AND stage = 'rejected'")
        stats["rejected"] = int((await cur.fetchone())[0])

        cur = await db.execute(
            "SELECT MIN(scraped_at) first, MAX(scraped_at) last FROM jobs")
        row = await cur.fetchone()
        stats["first_seen"], stats["last_seen"] = (row["first"], row["last"]) if row else (None, None)
        return stats


async def save_telegram_settings(token: str, chat_id: str, enabled: bool) -> None:
    """Store Telegram credentials. A blank token keeps the existing one, so the
    Settings form never wipes it just because the field renders empty."""
    async with conn.connect() as db:
        if token.strip():
            await db.execute(
                "UPDATE app_settings SET telegram_bot_token = ? WHERE id = ?",
                (token.strip(), PROFILE_ID))
        await db.execute(
            "UPDATE app_settings SET telegram_chat_id = ?, telegram_enabled = ?, "
            "updated_at = ? WHERE id = ?",
            (chat_id.strip(), 1 if enabled else 0, _utc_now_iso(), PROFILE_ID))
        await db.commit()


async def get_jobs_to_notify(min_score: int = 0) -> list[dict[str, Any]]:
    """Scored jobs at/above `min_score` that have not been sent yet."""
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM jobs WHERE status = 'scored' AND COALESCE(score, 0) >= ? "
            "ORDER BY score DESC", (int(min_score),))
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def mark_all_scored_notified() -> None:
    """Drain the notify queue.

    Jobs below the threshold are never sent, but they must not sit in 'scored'
    forever - otherwise every future digest re-considers them."""
    async with conn.connect() as db:
        await db.execute("UPDATE jobs SET status = 'notified' WHERE status = 'scored'")
        await db.commit()


# --------------------------------------------------------------------------- #
# Contacts (referral outreach)                                                #
# --------------------------------------------------------------------------- #

# A contact stops being interesting long before it stops existing. Anything
# still sitting at "invited" after this many days is archived automatically.
STALE_CONTACT_DAYS = 60


async def upsert_contact(job_id: int, name: str, headline: str | None,
                         profile_url: str | None, degree: int | None,
                         audience: str, rank: int) -> int | None:
    """Store a person read off a company page. Returns the row id, or None.

    Re-reading the same page must not reset work already done, so an existing
    row keeps its status and both drafts; only the facts are refreshed."""
    if not (name or "").strip() or not (profile_url or "").strip():
        return None
    now = _utc_now_iso()
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id FROM contacts WHERE job_id = ? AND profile_url = ?",
            (job_id, profile_url))
        row = await cur.fetchone()
        if row:
            await db.execute(
                "UPDATE contacts SET name = ?, headline = ?, degree = ?, "
                "audience = ?, rank = ?, updated_at = ? WHERE id = ?",
                (name, headline, degree, audience, rank, now, row["id"]))
            await db.commit()
            return int(row["id"])
        cur = await db.execute(
            "INSERT INTO contacts (job_id, name, headline, profile_url, degree, "
            "audience, rank, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'found', ?, ?)",
            (job_id, name, headline, profile_url, degree, audience, rank, now, now))
        await db.commit()
        return cur.lastrowid


async def list_contacts(job_id: int) -> list[dict[str, Any]]:
    """Contacts for a job, closest to the decision first."""
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM contacts WHERE job_id = ? AND status != 'archived' "
            "ORDER BY rank DESC, degree ASC, name ASC", (job_id,))
        return [dict(r) for r in await cur.fetchall()]


async def get_contact(contact_id: int) -> dict[str, Any] | None:
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def save_contact_draft(contact_id: int, kind: str, text: str) -> None:
    """Store a generated draft. `kind` is 'note' or 'message'."""
    column = "note" if kind == "note" else "message"
    async with conn.connect() as db:
        await db.execute(
            f"UPDATE contacts SET {column} = ?, updated_at = ? WHERE id = ?",
            (text, _utc_now_iso(), contact_id))
        await db.commit()


async def set_contact_status(contact_id: int, status: str,
                             spent_note: bool = False) -> None:
    """Move a contact along the pipeline.

    `spent_note` records that one of the month's personalised invite notes went
    with it - that allowance is small enough that the count has to be real."""
    now = _utc_now_iso()
    async with conn.connect() as db:
        if spent_note:
            await db.execute(
                "UPDATE contacts SET status = ?, note_sent_at = ?, updated_at = ? "
                "WHERE id = ?", (status, now, now, contact_id))
        else:
            await db.execute(
                "UPDATE contacts SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, contact_id))
        await db.commit()


async def delete_contact(contact_id: int) -> None:
    async with conn.connect() as db:
        await db.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        await db.commit()


async def notes_spent_this_month() -> int:
    """Personalised invite notes sent since the 1st.

    LinkedIn allows free accounts about five a month; without a counter the
    only way to discover the limit is to hit it."""
    first = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    async with conn.connect() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM contacts WHERE note_sent_at >= ?", (first,))
        row = await cur.fetchone()
        return int(row[0] or 0)


async def archive_stale_contacts(days: int = STALE_CONTACT_DAYS) -> int:
    """Archive contacts invited long ago that never accepted. Returns the count."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with conn.connect() as db:
        cur = await db.execute(
            "UPDATE contacts SET status = 'archived', updated_at = ? "
            "WHERE status = 'invited' AND updated_at < ?",
            (_utc_now_iso(), cutoff))
        await db.commit()
        return cur.rowcount or 0


async def set_people_target(job_id: int) -> None:
    """Park the job the extension should attach the next people batch to."""
    async with conn.connect() as db:
        await db.execute(
            "UPDATE app_settings SET people_target_job_id = ?, updated_at = ? WHERE id = ?",
            (int(job_id), _utc_now_iso(), PROFILE_ID))
        await db.commit()


async def get_people_target() -> dict[str, Any] | None:
    """The parked job, resolved to something the extension can display."""
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT people_target_job_id FROM app_settings WHERE id = ?", (PROFILE_ID,))
        row = await cur.fetchone()
        job_id = int(row["people_target_job_id"] or 0) if row else 0
        if not job_id:
            return None
        cur = await db.execute("SELECT id, title, company FROM jobs WHERE id = ?", (job_id,))
        job = await cur.fetchone()
        return dict(job) if job else None


async def mark_below_threshold_notified(min_score: int) -> int:
    """Drain the notify queue of everything that will never be sent.

    Jobs above the threshold stay in 'scored' so they can accumulate until the
    digest is worth sending; the rest must leave, or every later digest
    re-considers them forever."""
    async with conn.connect() as db:
        cur = await db.execute(
            "UPDATE jobs SET status = 'notified' "
            "WHERE status = 'scored' AND COALESCE(score, 0) < ?", (int(min_score),))
        await db.commit()
        return cur.rowcount or 0


# --------------------------------------------------------------------------- #
# Application pipeline                                                        #
# --------------------------------------------------------------------------- #

async def set_stage(job_id: int, stage: str) -> None:
    """Move an application to a stage.

    Setting any stage implies the job was applied to: reaching an HR interview
    without having applied is not a state that exists, and requiring two clicks
    to say so would just produce rows that disagree with themselves."""
    import pipeline

    now = _utc_now_iso()
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT stage_best FROM jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
        best = pipeline.furthest(row["stage_best"] if row else None, stage)
        await db.execute(
            "UPDATE jobs SET stage = ?, stage_best = ?, stage_at = ?, applied = 1, "
            "applied_at = COALESCE(applied_at, ?) WHERE id = ?",
            (stage, best, now, now, job_id))
        await db.commit()


async def list_applied(stage: str | None = None, limit: int | None = None,
                       order: str = "applied") -> list[dict[str, Any]]:
    """Applications, most recently applied first, optionally one stage only."""
    query = "SELECT * FROM jobs WHERE COALESCE(applied, 0) = 1"
    params: list[Any] = []
    if stage:
        query += " AND COALESCE(stage, 'applied') = ?"
        params.append(stage)
    query += f" {_order_by(order, default='applied')}"
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    async with conn.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def count_by_stage() -> dict[str, int]:
    """How many applications sit at each stage. Missing stages are absent, not
    zero - the caller knows the full list and fills the gaps."""
    async with conn.connect() as db:
        cur = await db.execute(
            "SELECT COALESCE(stage, 'applied') AS stage, COUNT(*) n FROM jobs "
            "WHERE COALESCE(applied, 0) = 1 GROUP BY stage")
        return {row[0]: int(row[1]) for row in await cur.fetchall()}


async def save_letter_template(html_text: str) -> None:
    """Store the letterhead. Empty resets to the shipped default."""
    async with conn.connect() as db:
        await db.execute(
            "UPDATE profile SET letter_html = ?, updated_at = ? WHERE id = ?",
            ((html_text or "").strip() or None, _utc_now_iso(), PROFILE_ID))
        await db.commit()


async def get_letter_template() -> str:
    """The user's letterhead, or the shipped default when they have not made one."""
    import letter_template

    profile = await get_profile()
    # The fallback is built from the CV, which already holds the name, city,
    # phone, email and LinkedIn a letterhead needs. Only someone with no CV at
    # all sees the "Your Name" markers.
    return ((profile.get("letter_html") or "").strip()
            or letter_template.default_template(profile.get("cv_html")))


async def save_ai_provider(provider: str, api_key: str) -> None:
    """Store the chosen provider and, when given, its key.

    A blank key keeps the stored one: the Settings form renders the field empty
    for safety, and saving the page must not wipe a working key."""
    import providers as _providers

    chosen = _providers.get(provider)
    column = _providers.key_column(chosen)
    async with conn.connect() as db:
        await db.execute(
            "UPDATE app_settings SET ai_provider = ?, updated_at = ? WHERE id = ?",
            (chosen.key, _utc_now_iso(), PROFILE_ID))
        if api_key.strip():
            await db.execute(
                f"UPDATE app_settings SET {column} = ? WHERE id = ?",
                (api_key.strip(), PROFILE_ID))
        await db.commit()


async def save_writer(provider: str, model: str, api_key: str = "") -> None:
    """Store the provider and model used for text a person reads.

    Kept apart from `save_ai_provider` because the two choices are independent:
    moving scanning to a cheaper endpoint must not silently move cover letters
    there too. The key is shared when both roles land on the same provider, so a
    blank one here is normal rather than an omission."""
    import providers as _providers

    chosen = _providers.get(provider)
    name = (model or "").strip()
    if name not in chosen.models:
        name = chosen.default_deep
    async with conn.connect() as db:
        await db.execute(
            "UPDATE app_settings SET writer_provider = ?, writer_model = ?, "
            "updated_at = ? WHERE id = ?",
            (chosen.key, name, _utc_now_iso(), PROFILE_ID))
        if api_key.strip():
            await db.execute(
                f"UPDATE app_settings SET {_providers.key_column(chosen)} = ? WHERE id = ?",
                (api_key.strip(), PROFILE_ID))
        await db.commit()


async def record_extension_state(state: str, error: str = "", version: str = "",
                                 run_at: str = "") -> None:
    """Store what the extension said about itself on its last schedule poll.

    `state` is one of 'ok', 'paused', 'stopped'. The timestamp is ours, not the
    browser's: it answers "when did we last hear anything", which is the
    question a silent extension makes worth asking."""
    async with conn.connect() as db:
        await db.execute(
            "UPDATE app_settings SET extension_state = ?, extension_error = ?, "
            "extension_version = ?, extension_seen_at = ?, "
            "extension_run_at = COALESCE(NULLIF(?, ''), extension_run_at) "
            "WHERE id = ?",
            (state, (error or "")[:200], version or "", _utc_now_iso(),
             run_at or "", PROFILE_ID))
        await db.commit()

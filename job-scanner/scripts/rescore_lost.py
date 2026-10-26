"""Re-score the jobs that were buried at zero by a failure, not by a verdict.

An earlier version stored 0 whenever the scorer returned an empty answer (a
reasoning budget too small to leave room for one), as if it were a judgement.
Those jobs left the queue carrying a number no model produced. They sit below
every threshold, so they appear in no list and nothing looks at them again.

The bug is fixed: an answer with no score in it is no longer written. This
repairs the damage it already did.

    .venv/bin/python scripts/rescore_lost.py            # count them, cost it, change nothing
    .venv/bin/python scripts/rescore_lost.py --apply    # re-score them

Stop the app first, or at least do not run a scan at the same time.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import providers  # noqa: E402
from db import conn, queries  # noqa: E402
from matcher import AIMatcher  # noqa: E402

# The signature of the failure: a zero with no reason and no flags. A genuine
# zero from the model carries a summary, because the prompt demands one.
LOST = """
    SELECT id FROM jobs
     WHERE score = 0
       AND (match_summary IS NULL OR match_summary IN ('', 'No summary.'))
       AND (red_flags IS NULL OR red_flags IN ('', '[]'))
"""

# Measured on the real prompts, off-peak DeepSeek Flash: 2 594 cached + 768 of
# job + 32 out. Roughly $0.00022 a job either way.
COST_PER_JOB = 0.00022


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually re-score")
    ap.add_argument("--limit", type=int, default=0, help="stop after N jobs")
    args = ap.parse_args()

    async with conn.connect() as db:
        ids = [r[0] for r in await (await db.execute(LOST)).fetchall()]
    if args.limit:
        ids = ids[:args.limit]

    print(f"{len(ids)} jobs carry a zero nobody decided on.")
    print(f"Re-scoring them costs about ${len(ids) * COST_PER_JOB:.2f}.")
    if not ids:
        return 0
    if not args.apply:
        print("\nNothing was changed. Add --apply to do it.")
        return 0

    settings_row = await queries.get_app_settings()
    profile = await queries.get_profile()
    provider, key = providers.credentials(settings_row, providers.SCAN)
    if not key:
        print(f"No API key for {provider.label} — set one in Settings first.")
        return 1
    matcher = AIMatcher(
        model=providers.resolve_model(settings_row, "fast"),
        api_key=key, base_url=provider.base_url,
        reasoning_reserve=provider.reasoning_reserve,
        timeout=provider.timeout, no_reasoning=providers.no_reasoning(settings_row),
    )

    scored = failed = 0
    for n, job_id in enumerate(ids, 1):
        job = await queries.get_job(job_id)
        if job is None:
            continue
        result = await matcher.quick_score(job, profile.get("instructions") or "")
        if result.failed:
            failed += 1
            print(f"  [{n}/{len(ids)}] {job_id} still no answer: {result.summary}")
            continue
        await queries.save_quick_score(job_id, result.score, result.summary, result.red_flags)
        scored += 1
        if result.score >= 60:
            print(f"  [{n}/{len(ids)}] {result.score:3}  {job.get('title')} — {job.get('company')}")
        elif n % 50 == 0:
            print(f"  [{n}/{len(ids)}] …")

    print(f"\n{scored} re-scored, {failed} still unanswered (left unscored, not zeroed).")
    print("Anything at 60 or above is now in Strong matches.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

# Job Finder — Project Notebook

> Architecture reference. Read this instead of scanning the tree; open a file only when you change it.
> Last updated **2026-08-19** (code state: 642 tests, released as 1.3.0).
> Rules that outlive the code live in `../CLAUDE.md`.

**Read the section you need, not the whole file** (`grep -n "^## " PROJECT_NOTEBOOK.md`, then Read with
`offset`/`limit`). Sections 1–3 are the map; the rest are independent.

| § | Read it before touching |
|---|---|
| 1–2 | anything — what the app is and where code lives |
| 3 | the database, a new column, anything that deletes rows |
| 4 | the scan loop, `/api/ingest`, scheduling, the digest |
| 5 | scoring, prompts, a new gate — **the most re-broken area** |
| 6 | models, API keys, cost, anything that calls a provider |
| 7 | job boards, a new source, "source X returns nothing" |
| 8 | routes, tabs, the Jobs list, application stages |
| 9 | templates, CSS, the AJAX refresh |
| 10 | cover letters, CV editing, PDFs |
| 11 | contacts and outreach |
| 12 | the Chrome extension |
| 13 | tests |
| 14 | packaging, releases, anything Windows |
| 15–16 | conventions, what is deliberately not built |

---

## 1. What it is

Local desktop app on the user's Mac (`127.0.0.1`, single user, no job-board login). Scans job boards on a
schedule, scores every posting against the user's profile cheapest-stage-first, then helps prepare the
application: cover letter, CV adjustments, HR answers, outreach to people at the company.

**Stack:** FastAPI + Jinja2 (server-rendered, no SPA) · aiosqlite · APScheduler · `AsyncOpenAI` (also used
for DeepSeek — same wire format) · httpx · WeasyPrint for PDF (`pydyf==0.11.0`, needs `brew install pango`) ·
vanilla JS. Entry `main.py`; `run-dev.sh` / `run-prod.sh`. Chrome MV3 extension in `../job-scanner-extension/`.

---

## 2. Layout

```
main.py 2064     FastAPI app, all 63 routes           providers.py      endpoints/models per role
config.py        frozen dataclass, built at IMPORT    pipeline.py       application stages
cv_content.py    CV css + cv_to_text()                letter_template.py letterhead + placeholders
paths.py         app_root() / user_data_dir()         extension_health.py what the extension reports
origins.py       who may talk to a server with no password on it
db/models.py     _SCHEMA, _MIGRATIONS, init_db()      db/queries.py 1386  ALL sql lives here
db/conn.py       the ONE way to open the database     db/usage.py       what each model call cost
matcher/  matcher.py (AIMatcher) generate.py (copilot) coverage.py (evidence) prompts/*.txt
          language.py seniority.py residency.py workplace.py audience.py   ← deterministic gates
scheduler/ jobs.py (scan loop, scheduler, digest)  screening.py (what is worth a model call)
scrapers/  base.py (JobListing + strip_html) api_sources.py linkedin_public.py ats.py national.py
           registry/eu_boards.json
scripts/   rescore_lost.py (repairs jobs buried at 0 by a failure)
templates/ base.html (ALL css + shared js) index job_detail profile search_form search_list
           settings statistic
telegram_bot/    digest notifier
```

`config.settings` is a **frozen dataclass built at import time** — set `DB_PATH` env var *before* importing
anything (that is what `tests/conftest.py` does).

---

## 3. Data model (SQLite)

Tables: `jobs · profile · searches · app_settings · cases · contacts · job_assets · model_usage · chats ·
chat_messages`.
**Columns are in `db/models.py`, each with a comment saying why it exists** — read them there rather than from a
copy that goes stale. What follows is only what the column names do not tell you.

**Two dedup layers.** `UNIQUE(source, external_id)` catches the same posting id. `dedup_key` catches the same
job re-posted under a new id — the common case (one opening advertised per city; the real DB had one job 17×).
`queries.dedup_key(title, company)` = de-accented, lowercased, punctuation-stripped `company|title` with
`(m/w/d)` asides removed. **Location is deliberately not in the key.** Returns `''` if either half is missing —
never dedup on title alone. Enforced in `insert_job` (covers `/api/ingest`) and pre-checked by
`duplicate_exists()` in the scan loop so a duplicate costs no tokens. `backfill_dedup_keys()` +
`purge_duplicates()` run once from `lifespan`; purge keeps applied/saved/pinned first, then highest score,
then oldest, and **never deletes a row the user acted on**.

**`profile`** and **`app_settings`** hold exactly one row each. In `profile`, `cv_html` is the single source of
truth and `resume_text` is derived from it via `cv_to_text()`; `cv_original_html` is the pristine baseline for
the compare view, never touched by per-bullet edits. **No default CV** — without an upload, CV features show an
"upload your CV" state. In `app_settings`, `copilot_min_score` (60) is the "To review" threshold, and
`scan_interval_minutes`/`openai_model` are legacy (always 0 / the fast model).

`searches.source` is a **CSV**, so one search can hit several boards. `job_assets` is a per-job key/value store
for generated text — anything not in `gen.KINDS` lives there without becoming a UI tab.

**`model_usage`** (`db/usage.py`) is the only record of what the models charged: four numbers per call —
stage, model, prompt, cached, completion — written after the answer arrives and never allowed to raise. Read by
`/statistic`. Everything anyone said about cost before 18 Aug 2026 was arithmetic over measured prompts.

**Open the database through `db/conn.py`, never `aiosqlite.connect` directly.** It carries the 15-second busy
timeout, and `apply_pragmas()` puts the file in **WAL** at startup — a scan writing while the page polls
`/api/scan-status` is the normal state of this app, and under the default journal one of them loses. That cost
a whole LinkedIn search on 18 Aug 2026. `tests/test_db_locking.py` fails if a raw connect reappears.

**Migrations:** append `(table, column, decl)` to `_MIGRATIONS` in `db/models.py`; `_ensure_column` ALTERs on
startup. **Never rewrite an existing table.**

---

## 4. Scan pipeline — `scheduler/jobs.py` + `scheduler/screening.py`

Streaming: per enabled search, cards are fetched one at a time and stored immediately so results appear
progressively. Two roads lead in — the scheduled scan of public sources, and the extension posting what it read
in the user's own LinkedIn session (`/api/ingest`).

**`scheduler/screening.py` owns the policy for both roads; each caller keeps its own plumbing.** They used to
carry separate copies of this decision and drifted: the free gates and the batched title screen were added to
the scan only, and the extension path kept paying full price for 56% of jobs. What differs (a `JobListing` vs a
stored row) is real; what must never differ is which jobs are screened out.

Per card:
1. **Dedup** — id, then fingerprint. Never spend tokens on a known job.
2. **`free_reject()`** — seniority / office-abroad / residency, all in code, **zero cost**. These gates used to
   run at the *end* of scoring: on a real DB, 1273 of 5886 jobs reached a verdict the code already held.
3. **`title_screen_batch`** — 10 titles per call (`matcher.BATCH_SIZE`). Not plausible ⇒ stored with score ≤39,
   "Screened out on title", no description fetch.
4. **Description** (inline for API sources, `fetch_job_detail` for LinkedIn) + language gate.
5. **`quick_score`** — instructions + description truncated to `_QUICK_DESC_CHARS=4500`. Sets `scored_at`.
6. **`advanced`** — on demand only, adds CV + case bank → strengths/weaknesses, may move the score. The page
   runs it as two parallel halves so the verdict paints first: `POST /jobs/{id}/advanced/verdict` (returns the
   rendered `partials/_proscons.html`) and `POST /jobs/{id}/advanced/coverage` (returns JSON: legend,
   highlighted body, digest). `POST /jobs/{id}/advanced` is the combined **no-JS fallback**. See §6.

`_REQUEST_TIMEOUT=18.0`, `_MAX_RETRIES=4`. **Every stage degrades gracefully** — an API/parse error returns a
safe result and the job is still saved (score 0 + "Scoring failed — check OpenAI at /api/diag") rather than
vanishing. `_require_content()` raises a *named* error when a reasoning model spends its whole budget thinking
and writes nothing, instead of looking like an empty answer.

**Scan state** (module globals): `is_scanning()`, `get_scan_progress() -> (done, total)`.
`request_stop()` sets the event **and cancels the task**, so a hung network call stops instantly.

**Scheduling:** `parse_scan_times` / `parse_scan_days` → one CronTrigger per time.
**Fixed-interval scanning was removed** — a schedule with no UI is invisible background behaviour.
**No times set ⇒ no automatic scans**, only "Scan now". The extension obeys the same schedule via
`GET /api/scan-schedule`. `scan_timezone` (empty = the machine's clock) is resolved by
`resolve_timezone()`, which **falls back rather than raising** — an unknown zone must cost a surprising
hour, never the whole schedule. A name that will not resolve is not stored in the first place.

**Telegram digest:** `DIGEST_MIN_SCORE=80`, `MIN_DIGEST_JOBS=5`. The extension posts a batch every few seconds
and each batch runs the pipeline, so without that floor the phone gets one message per job.

---

## 5. Deterministic gates — "the model judges, the code decides"

The model is asked for a score *as if every hard constraint were satisfied*; `blocker` + `blocker_reason` come
back separately and `_apply_blocker()` caps at 25. Only **location/workplace, language, seniority, residency**
are hard. Role type, industry, tools, salary, work authorization are graded — they subtract points.

Each gate reads the user's **own instructions** and blocks nothing when the rule is unstated. **Nothing personal
belongs in a prompt or in code** — no thresholds, no country lists tied to this user.

| Module | Blocks on | The trap it is built around |
|---|---|---|
| `language.py` | **two halves.** `violates`: posting WRITTEN in a language the user did not list (stopword detection, 18 langs + Cyrillic; ≥40 words, ≥5 hits, 1.5× edge over English, else `None`). `required_language_missing`: English posting that DEMANDS a working language they lack ("fluent Hungarian required") — capitalised proper-noun match + a requirement cue, minus a "plus" cue. Both short-circuit **before** the quick API call (score 15) and cap the deep verdict at 25. |
| `seniority.py` | title claims a level under the user's **Reject** heading | parenthetical asides stripped ("PM (Senior Stakeholders)" is not senior); "Lead Generation" is not a level. `required_years` must sit within 40 chars of "experience" and exclude company claims ("7 years on the market") — ⚠️ keep that exclusion list NARROW, an earlier one ate "We need 5+ years of experience". |
| `residency.py` | remote job that still demands you *live* somewhere | "a company based in Switzerland" ≠ "a candidate based in Switzerland" — patterns anchor on the person hired. A region is not a residency: "remote within Europe" does not make you a Swiss resident. Soft cap 55. |
| `workplace.py` | office requirement buried in the body | "United Kingdom (Remote)" with a hybrid model 3000 chars down. Of nine live matches for "hybrid", **seven** were methodology ("Agile, Waterfall, or hybrid"), travel, a gym, or an option the candidate may decline — hence the veto regexes and the ±130-char context window. |
| `screening.office_abroad` | Hybrid/on-site in a country the user does not live in | the card's chip and its location were never compared. Returns None on anything unknown. |

Each gate also writes a line into `_vacancy_block` (`Description language:`, `Title seniority:`,
`Experience required:`, `Office requirement in the body:`) so the model's own summary agrees with the code.

⚠️ **A required language is not the same as the advert's language — the model will NOT catch it** (26 Aug
2026). Because the score is "as if every constraint were satisfied" and the model's only language blocker is a
posting *written* in a foreign tongue, an English advert that requires Hungarian sailed through the model at
**85%**: `blocker=false` (the text is English), so `_apply_blocker` never capped it, and `violates` saw
English. Both levers were blind to the *requirement*. The fix is deterministic, not a prompt tweak:
`language.required_language_missing`, wired into `quick_score` and `_verdict_from_data`. If you ever find a
blocker the model "should have caught", assume it will not and gate it in code — that is the whole point of
this section.

⚠️ **Do not re-add a code-side seniority *verifier*.** One was built and removed the same day; it rested on the
belief the model confabulated a "ceiling of 4 years" — the user's instructions literally say that. The cure is a
capable model reading tiered instructions, not a second dumber opinion in Python.

⚠️ **`gpt-4o-mini` cannot do graded scoring — measured.** It zeroes anything it flags, so the softening is wasted
and four prompt rewrites did not fix it. Same jobs, same prompt: a mid-level PM asking 3 years scored 0 on
4o-mini and 50 on gpt-4.1-mini. The tell-tale signature was a live DB with 2180 jobs at 0–29, six above 60, and
**nothing in 30–59**. The fast model must be gpt-4.1-mini or better.

---

## 6. Providers — `providers.py`

Everything speaks the OpenAI wire format, so a provider is a base URL, a key and a model list. **Two roles, not
two models:**

- **SCAN** (`ai_provider`) — title screen, quick score. Thousands of calls; the answer is a
  number. Priced on cached input tokens.
- **WRITE** (`writer_provider`, defaults to **openai**) — cover letters, CV rewrites, HR answers, outreach,
  **and the advanced check** (`_ROLE_OF_STAGE["deep"] == WRITE`). A handful of calls a person waits for; the
  advanced check moved here when DeepSeek's reasoning ate its whole budget and it belongs where latency
  counts, not price.

Chosen independently in Settings, **service first and its models underneath** — a model list means nothing
until you know whose it is, and the page once read backwards. `Provider.leaves_eu` carries the warning, so it
is a property of the service and not of the template; both roles warn separately, because scanning sends
descriptions and writing sends the CV. One key when both roles share a provider.

**Three services** (28 Sep 2026): OpenAI (SDK default endpoint), DeepSeek, and **Google Gemini** through
Google's OpenAI-compatible endpoint (`generativelanguage.googleapis.com/v1beta/openai/`, key column
`gemini_api_key`, `leaves_eu=True` — only Vertex AI is EU-resident). Adding a service is one `Provider` entry
plus one `_MIGRATIONS` key column; storage, Settings and the model switcher are generic.

⚠️ **Only offer models that can scan with thinking down.** The scanning stages send `no_reasoning`
(`reasoning_effort: none`) on every job, and the model lists are shared by both roles. Gemini 2.5 **Pro** cannot
stop thinking and answers that with a 400 — offered as a scan model it would fail a whole day's scan. That is
why Gemini lists Flash-Lite and Flash only; `tests/test_providers.py` fails if a "pro" model is added.
Gemini prices checked against the live list 28 Sep 2026 (cache hit = a tenth of input; the from-memory draft
said a quarter).

**Per-model thinking floor** (26 Oct 2026). `Provider.least_reasoning` maps a model that cannot stop thinking to
the least it accepts; `providers.no_reasoning(settings, role)` resolves it against the role's model. The three
places a scanning client is built (`main._ai`, `scheduler.jobs._ai`, `scripts/rescore_lost.py`) all call it —
never `provider.no_reasoning` directly, or 3.5 Flash-Lite 400s in that path; a test guards all three. Measured
live with a fresh key: `3.5-flash-lite` still 400s on `none`, accepts `minimal` (also its default), and spent
**0 thinking tokens** on the real quick-score prompt (2043 in, 61 out). Offered, not default — $0.30 / $0.03 /
$2.50 against 3.1 Flash-Lite's $0.25 / $0.025 / $1.50. Same day, `3.1-flash-lite` returned 503 (high demand) and
took up to 80 s for a 63-token answer; the advanced verdict took 114 s with zero thinking — Google queueing, not
the app, but it sits close to the 180 s `write_timeout`.

⚠️ **The price list is not the availability list — test with a FRESH key** (12 Oct 2026). `gemini-2.5-flash-lite`
was still priced, yet answers new keys with 404 "no longer available to new users"; it was the default scan model,
so every new Gemini user scored nothing. Google's suggested replacement `3.5-flash-lite` rejects
`reasoning_effort: none` (400). Measured live: `3.1-flash-lite` (now default scan) and `2.5-flash` accept it — 2.5
Flash drops from 372 hidden thinking tokens to 0. The docs claim no 3.x model can stop thinking; 3.1 Flash-Lite and
3.5–3.8 Flash accepted `none` anyway, so **measure, don't trust the docs**. 3.8 Flash was returning 503 (high demand).
Google keys (`AIza…`) are masked in the log by `desktop/launcher._SecretsFilter`.

⚠️ **The Settings page switches services in the browser, never by re-rendering.** It used to navigate to
`/settings?provider=…` and `/settings?writer=…` — one parameter at a time, so each choice threw away the
other role's unsaved one. Picking a writer reverted the scanner, which read as "that combination is not
allowed", and every change jumped the reader to the top of the page. All services' model lists ship with the
page in `#service-data`. `resolve_model(settings, stage)`
maps `fast|deep|write` → role → column, falling back to the provider's own default if the stored name is not
one it offers. `reserve(settings, role)` gives the reasoning headroom.

Eval set of 32 jobs sampled evenly across all four score bands, reference = gpt-4.1-mini. Repriced against
the published lists on 17 Aug 2026 (the table this file shipped with was ~3x under on DeepSeek and 2x over
on OpenAI's cached rate):

| | $/job | $ per 1300/day | hours per 1300 | output tokens |
|---|---|---|---|---|
| OpenAI gpt-4.1-mini | 0.00050 | 0.65 | 0.6 | 2 106 |
| DeepSeek + reasoning, off-peak | 0.00086 | 1.12 | 4.6 | 33 058 |
| DeepSeek, reasoning off, off-peak | 0.00022 | 0.29 | 0.5 | 2 094 |
| DeepSeek, reasoning off, peak | 0.00045 | 0.59 | 0.5 | 2 094 |

**Validated against the live bill, 17 Aug 2026.** Measured on the real prompts: a quick score is 2 594 tokens
of cached prefix + 768 of job + 32 out; the title batch is 1 301 + 271 + 250 per ten. Real use is ~500 scores
and ~1.94M tokens per working day (scans run `mon-fri` only), $0.14/day, ~$3.03/month. DeepSeek's dashboard
for the same day: 3 363 tokens/request against 3 394 modelled, $0.15 against $0.140 — so **prefix caching is
confirmed working**, which is worth 3.5x. Nothing in the app records `usage`, so this is the only check there
is. DeepSeek bills 2x inside 01:00-04:00 and 06:00-10:00 UTC (from 16 Aug 2026): the 10:00 local scan sits in
it, which is +13% on a working day.

Reasoning is disabled on `title_screen`, `title_screen_batch` and `quick_score` via `extra_body` (the SDK
rejects it as a kwarg) and deliberately **left on for the advanced calls**.

**The advanced check is TWO calls, run in parallel** (25 Aug 2026). `advanced_verdict()` returns score +
pros/cons (`advanced_verdict.txt`, `_VERDICT_MAX_TOKENS`); `advanced_coverage()` returns the 6-12 cited
findings (`advanced_coverage.txt`, `_COVERAGE_MAX_TOKENS`). The verdict is small so it lands first and fills
the pros/cons region while the citations are still coming — the wait reads as progress, not dead air. Both
share `_advanced_user()` byte-for-byte so the provider caches the prefix and the second call pays little for
it. `_verdict_from_data()` applies the deterministic gates (blocker/language/residency/office) identically
for both the split verdict and the combined `advanced()`. The old combined `advanced()` (`advanced.txt`,
`_ADVANCED_MAX_TOKENS`) stays as the **no-JS fallback** for the plain form POST.

⚠️ **Any advanced budget must fit its answer on its own, not lean on the reserve** (fixed 25 Aug 2026). The
budget is `max_tokens + self.reserve` (added centrally in `_advanced_call`), and on OpenAI `reserve == 0`, so
the cap IS the whole budget. When the coverage block landed (19 Aug) the combined cap was 800: the JSON
truncated mid-array, `json.loads` raised, and every advanced check silently "produced no result" — the button
did nothing. A cap is a ceiling, not a charge; unused tokens are not billed. `tests/test_providers.py`
guards that the reserve is ADDED to every budget, combined and split.

⚠️ **Two latent bugs this split exposed — do not reintroduce.** Writing used to resolve its model from the
*scanning* provider, and the three streaming helpers in `generate.py` built their own `AsyncOpenAI` with **no
`base_url`**, so they always reached api.openai.com and would have been handed a DeepSeek model name.
`tests/test_providers.py` parses `main.py` with `ast` and **fails when a new writing call site takes the
scanning model** — that test is the guard, keep it passing rather than deleting it.

---

## 7. Sources — `scrapers/`

`SOURCE_FETCHERS` in `jobs.py` maps a source key to an async generator; `SOURCE_LABELS` +
`SEARCH_SOURCE_OPTIONS` (main.py) drive the UI. To add one: write `iter_*`, register in both maps.

`linkedin` (public guest endpoint, remote via JSON-LD `TELECOMMUTE`) · `remotive` · `remoteok` · `arbeitnow` ·
`jobicy` · `himalayas` · `workable` (the only probed ATS with a **global** search, honours a real location) ·
`weworkremotely` (RSS, 100 newest, **keywords filtered on TITLE only** — matching the description returned a
Head of Marketing for "product") · `eu_boards` · `arbeitsagentur` · `platsbanken`.

**`eu_boards` (`ats.py` + `registry/eu_boards.json`)** — Greenhouse/Lever/Ashby/Personio/Teamtailor/Recruitee/
SmartRecruiters expose one key-free JSON per **company**, so coverage is a list of verified slugs, not a query.
The list call carries the full description (except SmartRecruiters, which needs one fetch per posting and is
capped), a structured workplace flag folded into the location string so `detect_workplace` reads it like
LinkedIn's chip, and a real posting date. Fetched once per scan cycle and cached.

**`national.py`** — Arbeitsagentur (DE, description costs one extra request) and Platsbanken (SE, inline).
**Expect the language gate to reject most of these** — that is the gate working. NAV (NO) was left out: its
search returns no posting body.

**Probed and rejected — do not re-attempt without new information:** Wellfound (403 Cloudflare) · Otta /
Welcome to the Jungle (postings need an authenticated session) · InfoJobs (OAuth) · Adzuna (needs a free key —
easy to add if the user registers) · CV.ee (no public JSON) · Domestika (HTML only).

⚠️ **Recency floor.** LinkedIn is filtered server-side by `f_TPR`; API sources are filtered locally by
`_too_old`. With `tpr_seconds=86400` one real scan dropped **1014 of 1014** remotive/jobicy/remoteok cards as
"too old" — those boards publish a handful of matching roles per *week*. `_API_MIN_WINDOW = 7*86400` floors the
window for non-LinkedIn sources. After: remotive 0→4, jobicy 6→50, remoteok 0→60.

⚠️ **Two databases — do not debug the wrong one.** `data/jobs.db` is DEV; the desktop app writes
`~/Library/Application Support/Job Finder/jobs.db` (+ `jobfinder.log`), the REAL one.
`grep "title-screened" jobfinder.log` gives the per-source kept/too-old/workplace breakdown.

⚠️ **The sandbox cannot reach LinkedIn, OpenAI or these APIs.** Those paths only work on the user's Mac. For
"0 jobs" / "not scoring", send the user to `/api/diag`.

---

## 8. Routes & pages (`main.py`, ~63 routes)

⚠️ **No route is authenticated, and that is deliberate — but `origins.py` decides who may ask.** A middleware
refuses any request carrying an Origin that is not the extension or this machine's own pages, *before* the
route runs: CORS only asks a browser not to read the answer, while a cross-site form POST is delivered and
acted on regardless. A request with no Origin at all (the desktop window, curl, the scheduler) is not
cross-site and passes. The old `allow_origins=["*"]` told every website in the browser it was welcome to read
the CV.

**Jobs `/`** — four tabs: `strong` (default, labelled **"To review"**: score ≥ `copilot_min_score` or pinned),
`all` (every not-yet-applied job), `saved`, `applied` (filterable by pipeline stage). The internal key
`strong` and `view=strong` were left alone when the label changed — only labels moved.
**Everything is newest first** (`_RECENT_FIRST = scraped_at DESC, id DESC`; Applied uses `applied_at`). The
score is a per-row label, not the sort key. ⚠️ This also decides which rows survive `LIST_LIMIT=150`.

Other pages: `/jobs/{id}` (detail + copilot tabs), `/profile` (tabs `instructions | advanced | letter` — **the
CV editor lives here now**, `#cv-mine`; `templates/cv.html` is gone), `/search*`, `/statistic` (the funnel),
`/settings`. `/favorites` is a 307 to `/?view=saved`. The full list is
`grep -n '^@app\.' main.py` — read it there, it moves.

The personal assistant tab (`/chat`, `assistant/`, `/api/transcribe`) was removed when the code was opened
(19 Oct 2026). It drove the local Claude CLI and never had a page; `/cv/chat` and `/jobs/{id}/letter/chat` are
unrelated — they rewrite the CV and the letter from a typed instruction.

**`/jobs/clear` clears ONE tab and nothing else.** `clear_jobs(keep_favorites/keep_applied/keep_pinned=True)` —
the tabs the user curates by hand are independent of the results list. It once deleted every scored row and
wiped **Applied** (the application history). Still missing: partial delete, undo, preview count.

**Pipeline stages** (`pipeline.py`): applied → assignment → hr_interview → tech_interview → final_interview →
offer, with rejected as an *outcome* reachable from anywhere. `stage` is what is true now; **`stage_best` only
moves forward**, because a funnel built on `stage` forgets every interview that ended in a rejection.
One field, not an event log — it answers "how far did this get", which is the whole question.

---

## 9. Frontend rules (easy to break)

**All CSS is in `templates/base.html`** inside one `<style>` block, tokens in `:root`. No stylesheet file.

- `.metrics` = full-bleed **dark dashboard band** (`100vw` + `margin-left: calc(50% - 50vw)`; body has
  `overflow-x:hidden`). **It must never be inside the AJAX-swapped node.**
- `#jobs-tabs` and `#list-tools` live **outside** the swapped region; `#jobs-list` is the only thing re-mounted.
- `loadArea` syncs `.metrics__inner`, tab labels/active class in place and `#list-tools`, then swaps the list —
  surgical, to avoid a white flash.
- **Rows never re-animate on scan refresh.** Only tab switches animate: opacity cross-fade (no translate → no
  layout jump) plus the sliding pill.
- `window.tabInk(root)` positions the `.tabs__ink` pill; call it after changing the active tab or the DOM.
- The **Stop** button is a plain form POST to `/stop` — no JS dependency, guaranteed to work.
- **Advanced check is progressive but degrades.** `job_detail.html` intercepts `form[data-advanced]`,
  fires the verdict + coverage halves in parallel and swaps `#proscons-area`, `#cov-legend`, `#desc-body`,
  `#cov-digest` in place (server-rendered `partials/`). The plain form POST to `/jobs/{id}/advanced` is the
  fallback, so the button still works with JS off. The base.html generation-spinner handler **skips
  `data-advanced`** — those forms own their own spinner.
- **No confirm on single-job delete** (the user removes jobs constantly); the row fades out with a toast.
  Bulk "Clear results" keeps its confirm.
- Design tokens: `--bg:#FAFAF8 --surface:#FFF --text:#1D1D1D --accent:#16A34A`, dark
  `--header-bg:#141313 --header-text:#F5F4F3 --header-line:#2A2A2A`. Score badges are soft tinted blocks, not
  solid. Topbar 64px, brand left in M PLUS 1; tabs are a segmented control with a white pill.
- Verify a template change with a quick Jinja render smoke test before claiming it works.

---

## 10. Writing: copilot, CV, letters

**Cover-letter brief** — a per-job note ("mention my Shopify work"), stored in `job_assets` under
`cl_brief`; not in `gen.KINDS`, so it never becomes a tab. Prepended to `_context()` as highest priority,
**before** the resume, with an explicit "do not invent evidence" clause so "say I led a 20-person team" cannot
manufacture experience. An empty brief on regenerate reuses the stored one.

**Conversational CV editing** — `POST /cv/chat` → `gen.stream_cv_edit()` returns a raw-text
`StreamingResponse`; the JS reads it with `response.body.getReader()` and assigns `innerHTML` per chunk, so the
document types itself (verified over real HTTP: 1223 chunks, 0.9s → 13.5s). Wired anywhere via
`.cv-chat[data-target]` + `window.initCvChat`. Nothing is persisted until Save; one-step Undo lives in JS.
⚠️ **`TestClient` buffers streaming responses** into one chunk — never conclude "streaming is broken" from it.

**CV adjustment** — structured whole-CV edits with Before→After + a short "Why" (red left border = before,
green = after, no strikethrough). `_snap_edits_to_cv` snaps `before` to a verbatim element at generation;
`_cv_similarity` (SequenceMatcher or word-containment, ~0.5) matches on apply.
`POST /jobs/{id}/cv/save-pdf` takes the live DOM in an `html` field, or edits typed seconds earlier are lost.
PDF via WeasyPrint with an **auto-fit loop** (12.5→9px until ≤1 page).

**Letterhead** (`letter_template.py`) — the cover-letter PDF was a bare body: a note, not a letter. The template
is HTML the user owns (Profile → Letter template) with `{{date}} {{company}} {{company_address}} {{job_title}}
{{body}}`. Everything but the body is escaped ("Smith & Sons"); an unknown token is left visible rather than
blanked.

---

## 11. Outreach — `matcher/audience.py`, `contacts`

Both answers are mechanical, so classifying ten contacts costs **zero model calls**: the audience comes from
the person's headline (`recruiter | leadership | hiring_manager | peer`), the route from the LinkedIn degree.

The route matters more than the tone: 1st = direct message, no limit; 2nd = invite first, note capped at 200
chars and free accounts get ~5 personalised ones per **month**; 3rd = nothing without Premium. So a draft
written without the degree is unsendable. **The recommended play for 2nd degree is an invite with no note**,
then the full message once accepted — it spends none of the monthly five and writes better than 200 chars can.

The app parks `people_target_job_id` in settings and the extension asks for it over the HTTP channel it already
uses, because the extension cannot read the app's page.

---

## 12. Chrome extension — `../job-scanner-extension/`

MV3, reads LinkedIn in the user's own logged-in session and posts to `/api/ingest`. Obeys the app's schedule
via `/api/scan-schedule`; also collects people for the outreach flow.

⚠️ **It pauses itself after two empty runs, and that state lives in `chrome.storage` where the app cannot see
it.** From outside it looks like a perfectly healthy Jobs page with nothing arriving for days. Two fixes, both
load-bearing:
- The state travels on the schedule poll and lands in `app_settings`. `extension_health.status()` renders one
  line on the Jobs page **only when something is wrong**; silence for 30 min (six missed heartbeats) outranks a
  stale "ok", and someone who never installed the extension is told nothing.
- The pause check used to sit **above** the poll, so pausing silenced the only channel that could report the
  pause. Poll first, then decide whether to run.

⚠️ **Popup width.** Chrome sizes the popup window to the widest element while `body` keeps its declared width,
so one unbreakable string (a LinkedIn search URL) opens a white gutter. Fixed by pinning `width` on **`html`**
as well as `body`, plus `overflow-x:hidden` and `overflow-wrap:anywhere`.

---

## 13. Tests — `.venv/bin/python -m pytest tests -q`

**~640 tests, ~6s, no network and no API calls.** `pytest.ini` sets `asyncio_mode = auto`, so async tests need
no marker. Deps in `requirements-dev.txt`. Filenames say what each file guards; the counts are in the run.

Scope is deliberately **the code that destroys data, hides data, or spends money**, not coverage. Two tests are
not what they look like: `test_providers.py` parses `main.py` with `ast` and fails on a *future* call site
(§6), and `test_scan_pipeline.py` asserts both ingest roads screen identically rather than testing either one.

`conftest.py` sets `DB_PATH` to a temp file **before importing anything** and asserts `jobfinder-tests` is in
the path, so a mis-resolved path fails loudly instead of deleting the real database. The `listing` factory gives
every job a **unique title** — the app dedups by company+title, so identical fixtures silently collapse to one row.

**Verified by mutation, not by going green.** Each guard was confirmed to fail with its bug reintroduced
(`keep_applied` removed → 1 fail; `dedup_key` returning `""` → 9; purge protection dropped → 1; score-first
ordering → 3; language gate defaulting to English → 1; the HTML fallback removed → 2; the silence banner put
back → 3). If you add a test, break the code once and check. A guard that survives its own mutation is not a
guard: `test_html_text.py` had one that patched `codecs.lookup`, which the decoder never consults, and it
passed happily with the fix taken out.

**A build is a different machine.** Two of the worst faults this suite has never seen were frozen-only: the
packaged app carried no `windows-1252` codec, so the HTML parser raised `LookupError` from inside a job
description and killed 84 whole searches over seven days. Codecs are resolved from a *string* at the moment
they are needed, so nothing imports them and PyInstaller cannot see them — `collect_submodules("encodings")`
in the spec is what ships them. When a test cannot reach the fault, pin the shipping condition instead.

---

## 14. Desktop build — `desktop/`

pywebview + PyInstaller (native WKWebView, no Chromium). `launcher.py` points `DB_PATH` at a writable user dir,
picks a free port, boots FastAPI in a thread, opens the window, checks for updates. `main.py` `BASE_DIR` and
`matcher._PROMPTS` both go through `paths.app_root()` so resources resolve when frozen.

**The full release sequence is `RELEASE.md`, and `./desktop/preflight.sh` refuses to pass unless all three
assets exist and are newer than the code.** Releases 1.2.0 to 1.2.3 all shipped macOS-only because the
instruction named one build; Windows users found out. Three assets, every time: `JobFinder-macOS.zip`,
`JobFinder.dmg`, `JobFinder-Setup.exe`.

Two things `desktop/README.md` does not say: the macOS build bundles pango/cairo via `macos_libs.py`, and the
Windows NSIS installer cross-built on a Mac is **untested by construction** — a Mac can build it but cannot
run it.

**Templates are listed one entry at a time in the spec.** A file maps to `templates/`, a subdirectory to
`templates/<name>`, so `{% include 'partials/_x.html' %}` resolves in the bundle as it does from source.

⚠️ **The macOS auto-update deletes the running bundle.** `updater.download_and_apply` writes a script that
waits ten seconds for the process to exit and then `rm -rf`s the app whether it has or not. Pages are read from
disk when first opened, so the next click after an update can be a 500 — it is in the user log four times.
Half of this was fixed on 12 Aug 2026 (module imports hoisted to startup); the templates half is open.

**Releases:** code is in `andrii424/JobFinder`; the unauthenticated updater reads the public
`andrii424/JobFinder-releases`. Bump `desktop/version.py`, rebuild, `gh release create v<x> … -R
andrii424/JobFinder-releases`. The update *swap* is macOS-only by design; on Windows the updater opens the
installer download, because an installer cannot replace the .exe running it. Both platforms are now offered
the update — `asset_for_platform()` picks the asset that belongs to the machine asking, and returning None is
a real answer ("no update for you") rather than a reason to hand back somebody else's build.

⚠️ Hard-won Windows rules, all still load-bearing:
- **`sys.stdout`/`sys.stderr` can be `None`** — the shortcut runs `pythonw.exe`. uvicorn's colour formatter did
  `sys.stdout.isatty()` and `dictConfig` re-raised it as `ValueError: Unable to configure formatter 'default'`;
  the server thread died before serving. `_ensure_std_streams()` runs before any other import, and
  `uvicorn.Config(log_config=None)`. Anything touching stdout must assume None.
- **Windows ships no tz database** — `tzdata` must stay in the wheel list and in `requirements.txt` behind
  `sys_platform == "win32"`. `create_scheduler()` wraps *construction* too, because APScheduler resolves the
  local zone itself and raised again after the first guard. A bad timezone must never take the app down.
- **A failed lifespan looks like a timeout** — uvicorn logs the traceback and exits cleanly, which is why that
  bug survived a release. `launcher._LastError` keeps the last ERROR line so the dialog names the real cause.
- Keep the 1.0.3 diagnostics: `_fatal()` → log + `MessageBoxW`, the **browser fallback** when webview fails
  (missing WebView2/pythonnet — the server thread is a daemon, so it must block the main thread), the WebView2
  registry check, and the "show errors" shortcut.
- **Don't let pywebview wipe the context menu** — its Cocoa backend calls `menu.removeAllItems()` unless debug
  is on, so right-click → Copy disappears. `launcher._enable_context_menu()` neutralises that one method.
- Unsigned: macOS needs right-click → Open once; Windows SmartScreen warns.

---

## 15. Conventions

- Keep `scheduler/__init__.py` re-exports in sync with new public functions in `jobs.py`.
- Write absolute dates in notes, never relative ones.
- Nothing personal in a prompt or in code — it belongs in the user's AI Instructions.
- The user iterates on design from reference images: match the image, don't invent.

## 16. Deferred

Employment type from Lever/Ashby · salary + seniority band from Himalayas · Greenhouse/Lever/Ashby *global*
search (needs a tracked-companies table + UI) · application autofill (fill-only, no submit) ·
splitting `main.py` (2006 lines, 61 routes) into routers.

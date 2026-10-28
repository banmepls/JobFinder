# Changes since 1.1.0

Everything below sits between the 1.1.0 release and now: **50 files changed, 21
new, 6681 lines added**. Tests went from 424 to **656**. Seventeen new database
columns and one new table, applied by migration on first launch.

Sections 1 to 9 shipped as 1.2.0 to 1.2.3. Everything from section 10 onwards
ships as **1.3.0** — including all of the audit below, which is the reason 1.3.0
exists.

---

## 1. Application pipeline

Applications were counted and then forgotten. The one question the whole
filtering strategy rests on — does showing 30 jobs instead of 3300 produce more
interviews — had no answer.

**Stages**, in process order: Applied → Test task → HR interview → Tech
interview → Final interview → Offer. Rejected sits outside that line, because it
is an outcome rather than a step and can land on any stage.

- Filter chips on the **Applied** tab, with counts. Empty stages stay visible but
  dimmed, so the row does not reshuffle every time something moves.
- A stage selector in each row and on the job page. Setting a stage also marks
  the job applied: an HR interview for a job you never applied to is not a state
  that exists, and asking for two clicks would only produce rows that disagree
  with themselves.
- `applied_at` records when you applied and never moves; `stage_at` records when
  the stage last changed.

**A defect found while building it.** One field is not enough. An application
rejected after a tech interview reads as "rejected", and a funnel built on that
forgets the interview ever happened — under-reporting the only ratio worth
measuring. A second column holds the furthest stage reached; it moves forward
only and a rejection never overwrites it.

The **Statistic** page now shows the funnel with conversion at each step, and the
interview rate against the market average of 7.6%.

## 2. Sorting

Every list can be ordered by newest, highest score, title A-Z or company A-Z;
the Applied tab adds "recently applied". Sorting keeps the stage filter and the
stage filter keeps the sort.

Every order ends in a tie-breaker. Without one, rows with equal scores swap
places between page loads and the list looks broken rather than sorted.

**A defect found while building it.** `?sort=applied` passed validation on every
tab, so the All tab sorted by a date most of its rows do not have while the menu
showed nothing selected. Orders are now validated against what the current tab
actually offers.

## 3. Cover letters

**What the letter is for.** The prompt no longer asks for a prose version of the
resume. It answers the three questions a resume cannot: why this company, why
this role, why you — with experience used as evidence for a claim rather than
listed as a record. The instruction to recite two achievements with metrics is
gone; the case bank is still the material, shaped as alignment.

**A letterhead.** The PDF was the letter body alone: no date, no addressee, no
sender block. Next to the CV, which has a document layout, it read as a note.
The template is HTML the user owns, edited under **Profile → Letter template**,
with placeholders for the date, company, company address and job title. The
recipient's address is typed per application on the job page.

## 4. Where the jobs come from

### Metadata nobody was reading

Measured across the whole database:

| Source | Jobs | Work arrangement recovered |
|---|---|---|
| LinkedIn via the extension | 2747 | **100%** |
| LinkedIn via the app | 2713 | **0%** |
| arbeitnow | 527 | 2% |
| jobicy, himalayas, remoteok, remotive | 416 | 0% |

The sources were publishing it and the app was discarding it: Remotive states
`job_type` (including freelance), Arbeitnow a `remote` boolean, Himalayas the
employment type, a salary range and a seniority band.

- Five boards list nothing but remote work; they now say so in the metadata
  rather than hoping the word appears in the prose.
- **Employment type** is a new field, stored and shown to the model. An
  internship and a freelance gig used to score like permanent roles.
- LinkedIn's own job page states four facts — seniority level, employment type,
  job function, industries. The app already downloads that page for the
  description, so two of them now cost nothing extra. The search results carry
  none of it, which is why the app recovered 0%.

### Location and workplace

Three gaps closed, each found by a real posting scoring 85%.

**An office in another country.** The card says the arrangement and the location
says where, and nothing compared the two — so "Stockholm, Sweden (Hybrid)"
passed as "somewhere in Europe, remote-ish". 165 jobs in the database.

**An office buried in the body.** The card said "United Kingdom (Remote)" and
three thousand characters down sat "fully hybrid working model, with one two-day
onsite session per month in Birmingham".

Reading the whole description for the word "hybrid" is not the fix. Of nine
matches at 60% and above, seven meant something else: Agile methodology,
occasional travel, a sports ground "on site", and offers of a choice the reader
can decline. The detector looks for phrases describing the arrangement and
throws the match away when the surrounding words show it is a methodology, a
trip, a perk or an option. 80 hits across 2388 descriptions, all genuine.

**A country stated in the description.** "This is a Germany-based role",
"UK based", "Remote (Germany)", "Location: Portugal". An employer's own address
("a Germany-based company") does not count.

A first attempt blocked on the card alone. Measured, that removed 108 of 138
jobs from the review list, most of them wrongly — companies fill the location
field with their own address and hire across Europe anyway. The rule now needs
the description to say it, and blocks 7 of 138.

**The advanced check explains itself.** A score quietly capped with no gap
explaining it reads as the model being wrong. The deep check now writes the
reason into "Gaps to watch".

## 5. Cost

Spend was $1.15 on the heaviest day. The structure of it: **94.8% of every token
sent was the same text sent again.** Of a 1247-token title screen, 17 tokens
were the job.

| | Input | Cost |
|---|---|---|
| Before | 14.34M | $7.77 |
| Free gates before the model | 11.60M | $6.35 |
| Ten titles per call | 6.71M | $4.21 |
| Cacheable prefix | 3.72M | **$3.02** |

**−61%.** Per day: $1.70 → $0.66.

**Free gates run first.** The seniority and location checks already existed and
ran at the *end* of scoring, after two model calls had been paid for. On the
real database that was 1273 of 5886 jobs reaching a verdict the code held from
the start.

**Titles are screened ten at a time.** The preamble is 1230 tokens and the title
is 17. Anything the model fails to answer for is let through, never dropped, and
verdicts are matched by number rather than position.

**The instructions moved into the system message.** OpenAI caches a prompt
prefix automatically once it reaches 1024 tokens, and only when it is identical
between calls. With the instructions in the user message the stable prefix was
364 tokens — under the threshold — so the title screen was never cached at all.

### Evals that did not ship

Three offline evals, all against a fixed set of jobs already scored by the
model in production, which is the reference. Recorded because the negative
result is the useful part.

**gpt-4o-mini.** Re-evaluated now that the code does the hard gates, on the
theory that the model's remaining task got simpler. It did not help: of jobs
scoring 60-79, half came back below 30. On the title screen its recall was 30 of
40 plausible jobs, and it never erred the cheap way. It stays off.

**A shorter scoring prompt.** Trimmed from 1709 to 605 tokens by removing
explanations of rules the code now enforces. On 22 jobs currently in the review
list it kept 17 against the full prompt's 20, one dropping from 75 to 25. The
15% saving was not worth three jobs.

**A tightened title screen.** Two prompt variants against a labelled eval set of
139 good jobs and 200 poor ones. The strict one saved 38% at a 22% recall loss on
the good jobs; the mild one saved 2.5% and still lost four. Neither shipped. The conclusion is that a title
does not carry the information — "Onboarding Coordinator" cannot be judged
without the description.

### A defect the evals exposed

A dense scan hits the 200,000 tokens-per-minute limit. Failed calls were being
written to the database as a score of 0, which is permanent: the job leaves the
queue with a number the model never produced and is never looked at again. Five
jobs in the database already. Temporary failures are now told apart from real
ones and leave the job unscored for the next cycle. Retries went from 1 to 4.

## 6. DeepSeek

A second provider. Everything speaks the OpenAI wire format, so a provider is a
base URL and a key; `providers.py` holds one entry per service and nothing else
in the app knows they exist.

| | Input | Cached input | Output |
|---|---|---|---|
| gpt-4.1-mini | $0.40 | $0.10 | $1.60 |
| DeepSeek V4 Flash, off-peak | $0.22 | **$0.007** | $0.66 |
| DeepSeek V4 Flash, peak | $0.44 | $0.014 | $1.32 |

The cached price is the one that decides the bill here, and DeepSeek's is 14
times lower off-peak. The 2575-token prefix costs very little; it does not stop
costing anything.

> **Repriced 17 August 2026.** The numbers this section shipped with were wrong
> in both directions — DeepSeek's were about a third of the published list, and
> OpenAI's cached rate was double it, since the 4.1 family caches at a quarter of
> input and not a half. Together they overstated the gap as 71x. See
> *Repricing* at the end of this file.

Each provider keeps its own key, so switching back does not mean pasting the
previous one again. The model list follows the provider — offering
`gpt-4.1-mini` to DeepSeek would earn a 400. The Settings page states plainly
that descriptions, the CV and the instructions would leave the EU.

### Measured, then split in two

The same eval that failed gpt-4o-mini, run on an eval set of 32 jobs sampled
evenly across all four score bands, scored against gpt-4.1-mini as the reference:

Token counts are as measured; the money is those counts repriced against the
published lists on 17 August 2026.

| | $ per job | $ per 1300/day | hours per 1300 | output tokens |
|---|---|---|---|---|
| gpt-4.1-mini | 0.00050 | 0.65 | 0.6 | 2 106 |
| DeepSeek, thinking on, off-peak | 0.00086 | 1.12 | 4.6 | 33 058 |
| DeepSeek, thinking off, off-peak | **0.00022** | **0.29** | 0.5 | 2 094 |
| DeepSeek, thinking off, peak | 0.00045 | 0.59 | 0.5 | 2 094 |

At the current prices, reasoning does not merely cost more than not reasoning —
it costs more than OpenAI, for the same verdicts and eight times the wall clock.
The decision to switch it off for the title screen and the quick score was made
on the old numbers and the new ones make it look conservative.

Reasoning cost 16 times the output tokens and eight times the wall clock for the
same verdicts, and it is now off for the title screen and the quick score. It
stays on for the advanced check, which is asked a genuinely hard question a few
times a day.

Input dominates the bill on both services, and the cached part of it is where
they differ: about 2 000 of the 2 800 input tokens a scored job sends are the
same prefix as the job before it.

**So the two roles were separated.** They have nothing in common but the wire
format:

- **Scanning** — the title screen, the quick score, the advanced check. Thousands
  of calls a day, each answering with a number nobody reads as prose. Judged on
  price and agreement, and DeepSeek wins both.
- **Writing** — cover letters, CV rewrites, answers to HR questions, outreach
  drafts. One or two calls per application, read by a person and sent to an
  employer. Price is irrelevant at that volume and the prose is not.

Each picks its provider and model independently in Settings, and shares a key
when both land on the same service. Defaults to scanning wherever it is
configured and writing on OpenAI, so switching the scan does not quietly move
cover letters with it.

**Two defects this exposed.** The letter and CV models were resolved from the
*scanning* provider, so choosing DeepSeek would have sent `deepseek-v4-pro` to
whichever endpoint the writer used. Worse, three of the writing helpers built
their own client with no endpoint at all — they always reached
`api.openai.com`, and would have been handed a DeepSeek model name for a
guaranteed 400 on the one call the user sits and watches. Both are covered by a
structural test that reads `main.py` and fails on a new writing call site that
takes the scanning model.

## 7. One pipeline instead of two

Jobs arrive by two roads: the scheduled scan, and the extension posting what it
read in the logged-in session. Each carried its own copy of the screening
decision, and they drifted — the free gates and the batched screen were added to
the scheduled scan while the extension path, which supplies 56% of all jobs,
kept paying full price.

`scheduler/screening.py` now holds the policy. What differs between the roads is
real and stays separate; what must never differ is which jobs get screened out
and why. Two tests fail if a second copy of the policy appears in either path.

## 8. The extension failing where you can see it

It supplies 56% of every job in the database, and it pauses itself after two
scans that come back empty — a sign-in wall, a search URL that stopped matching.
That state was written to browser storage and nowhere else.

What that looks like from the outside is nothing at all: a Jobs page that reads
as completely healthy while no new job arrives for three days. It happened. The
only trace is in the ingest log, in hindsight.

The extension polls `/api/scan-schedule` every five minutes, so it now describes
itself on the way past — paused, stopped or running, with the reason and its
version. The Jobs page shows one line when there is something wrong and nothing
at all otherwise.

Three things had to change for that to be true:

- The pause check used to sit **above** the poll, so pausing silenced the only
  channel that could have reported the pause. It now polls first and pauses
  after.
- Silence outranks a stale flag. A browser that never opens reports "ok"
  forever, because the last poll before it closed said so; not hearing anything
  for thirty minutes is its own warning, and "press Scan now" is useless advice
  when Chrome is not running.
- Nothing is said to someone who never installed the extension.

**The popup.** Chrome sizes a popup to the widest thing inside it and leaves the
body at its declared width, so one unbreakable string — a LinkedIn search URL in
the status line — bought a white gutter down the right-hand side. Long strings
now wrap. The status block also moved to the top, above the searches, which are
collapsed: it was below the fold of a popup Chrome caps at ~600px, which is a
poor place to keep the word "Paused".

## 9. Housekeeping

- Three templates nothing rendered were deleted. A test now fails on a template
  with no route, or a route with no template.
- `/chat` pointed at a file that does not exist and returned a 500. It redirects
  until the page is written. The backend behind it drives a personal Claude CLI
  subscription and must not ship in a build other people run.
- Three sources were showing in the UI as raw identifiers.

## 10. The case bank, and what it is for

Adding a story reloaded the page and dropped you at the top of Profile, in front
of the CV uploader. Deleting one did the same. Typing in five stories therefore
cost five scrolls back down to the form. Both now happen in place: only the list
is replaced, and it is re-read from the server rather than patched in the
browser, so what is on screen is what is stored.

Deleting a case is one click with no confirmation. That was survivable while it
reloaded the page — the reload was itself a receipt. Without it, the delete is
silent, so the story is now offered back: **Deleted. Undo**, which reposts it.

**Interview prep** (was "Interview instructions") was a general brief: position
yourself, achievements to emphasise, gaps. Useful, and mostly the résumé read
back to a person who wrote it. It is now capped at 250 words and built around
the case bank — it names the two to four stories to actually tell in this
interview, by their titles, with the question each one answers and the number to
lead with. Picking on evidence is stated as the rule: the story that lands on
what the posting asks for beats the more impressive one that does not.

---

## Deduplication, for the record

A scan at 15:00 does not re-score what a scan at 10:00 found. Two checks run
before any model call: the source's own id, and a content fingerprint of company
plus title. The second exists because the same opening is re-posted weekly and
cloned per city, each time with a new id.

```
extension sent      17126 cards
stored as new        4136
dropped as known    12990  (76%, at zero token cost)
```

6431 rows in the database; 6417 carry a fingerprint and all 6417 of those are distinct. The 14 without one are postings that arrived with no company name — an identical title alone
says nothing about it being the same job.

## 11. The letterhead, described instead of coded

The cover-letter template was the one thing in the app a person had to edit as
raw HTML, placeholders and inline styles included. That is a fair thing to ask
of nobody, and it meant the shipped letterhead was the letterhead almost
everyone kept.

It is now a sentence: *"no date, my name bigger and on the right, thin line
under it"*, and the writing model redraws the page. **Capped at 200 characters**,
which is the feature rather than a limit — a letterhead is a shape, and given a
paragraph people start describing the letter instead, which this does not write.
The cap is enforced on the server; `maxlength` on an input is a suggestion to
anything that is not a browser.

Two things are not left to the model. The sender block is handed to it from the
CV and it is told to reproduce it verbatim rather than retype a name and a phone
number. And `{{body}}` — the letter itself — is checked for on the way back: a
layout that lost it would print a handsome heading with nothing under it, and
nobody would find out until the PDF. That answer is refused and the editor is
left alone.

Nothing is stored until Save is pressed, so a layout you have not looked at
cannot replace the one your letters are already printing on. The HTML box is
still there, folded away under *Edit the HTML directly*.

## 12. Seeing it before it exists

**A whole class of dead buttons.** Switching a tab on the Profile page replaces
the body with `innerHTML`, and a `<script>` inserted that way is never executed
by the browser. Every handler defined inside a tab was therefore dead for anyone
who arrived by clicking the tab rather than by loading its URL: the letterhead's
character counter never counted, its Redraw button fell through to a plain GET
that put the description in the address bar, and **Save changes on the CV called
a function that did not exist**. All of it now lives at page level and is
delegated to an element the swap does not replace. A test parses the page and
fails on a script inside the swapped body.

**The letterhead has a preview.** The redraw landed in a textarea folded away
under *Edit the HTML directly*, so nothing on screen changed and the only way to
check the result was to print a PDF and open it. The page now shows the
letterhead as it prints, filled with a sample company and a sample paragraph,
and it redraws with each attempt.

**Every PDF on the job page can be looked at before it is written.** All three
downloads — the CV, the CV tailored for this role, and the cover letter — now
have *Preview PDF* beside *Save PDF*. Each opens the exact page that will print:
the real letterhead, the address typed for this application, and whatever is in
the editor right now rather than the last saved version. Each is built by the
same call the PDF renderer makes, because a preview assembled by a second code
path is a picture of something else. Nothing is written to disk.

The preview belongs where the download is. The letterhead page keeps its own,
because there you are editing the letterhead itself and there is no letter and
no company to print it against.

**Saving a PDF says so.** The confirmation was a grey line under a full page of
letter, easy to miss and easy to read as "the button did nothing". Saves,
failures and errors now raise a toast as well.

## 13. The deep check now shows its evidence

"You fit, here are three strengths" is an opinion about someone's career, which
is the one thing they cannot check. The deep check now marks the advert itself:
each demand it found is highlighted where it sits in the text — green where the
record answers it, amber where it is adjacent, red where nothing answers it —
and hovering shows what answers it. A digest under the description lists the
same findings as a short list.

**The model does not get to rewrite the advert.** It returns short quotes; the
code finds each one in the real description and marks it there. A quote that is
not present character for character is dropped, never approximated — a
highlight sitting on the neighbouring sentence is worse than no highlight,
because it looks like evidence. Whitespace is the only thing forgiven, because
adverts are hard-wrapped and a model quoting them collapses that without meaning
to. A changed word is a changed quote.

The same rule covers attribution. A case story is named by its title and the
title is checked against the case bank; one that is not there is discarded and
the finding stands without a source. From the CV it is one line saying what was
actually done, never a quality. The one place claiming to be proof is the
easiest place to invent, so it is the place with the strictest check.

Marks are rebuilt from the stored quotes on every page load rather than from
stored offsets: descriptions get re-fetched and enriched, and offsets would then
point at the wrong words. Findings are capped at 14 — beyond that the page reads
as decoration rather than as findings — and overlapping quotes resolve to the
more specific one, since nested markup would not render.

**Cost.** The findings ride on the call that was already being made, so nothing
new is sent: the description, the CV and the case bank were all in the prompt
already. Measured on the real prompts, a deep check goes from $0.0157 to $0.0201
— at ten checks a day, $4.72 to $6.04 a month.

Which is the moment to note that **the deep check costs more than all the
scanning** — around $4.30 a month against $3.03 — on eleven calls a day against
five hundred. Each one carries the résumé, the case bank and the instructions,
and that is 6 360 tokens of context in front of a 737-token vacancy.

**Those 6 360 tokens are already cached, and there is nothing to fix.** Caching
works on the prompt PREFIX, and this prompt was already built in the right
order: everything reusable first, the vacancy last. Measured on the log, 80% of
deep checks happen within five minutes of the one before, so the prefix is still
warm and they are billed at a quarter. The remaining 20% are the first call of a
burst, which can never be warm. Blended, $0.0126 a check; if every call were
warm it would be $0.0107, so the whole theoretical saving left is about 65 cents
a month.

What that property lacks was a test. A single per-job word moved above the case
bank makes the entire prefix unique, doubles the price of every deep check and
changes nothing else — nothing fails, nothing appears in the log. Four tests now
build two prompts for two different jobs at each call site and measure what
actually repeats.

## Repricing, 17 August 2026

The price table in `providers.py` was checked against both published lists for
the first time since it was written. It was wrong on both providers, and both
errors flattered the same conclusion.

| per 1M tokens | table said | published | |
|---|---|---|---|
| DeepSeek Flash, cache hit | $0.0028 | $0.007 off-peak, $0.014 peak | 2.5–5x under |
| DeepSeek Flash, input | $0.14 | $0.22 / $0.44 | 1.6–3.1x under |
| DeepSeek Flash, output | $0.28 | $0.66 / $1.32 | 2.4–4.7x under |
| gpt-4.1-mini, cache hit | $0.20 | $0.10 | 2x over |

The claimed 71x advantage on the token that matters is really **14x off-peak and
7x at peak**. Scanning on DeepSeek is still the cheaper choice and by a wide
margin; it is not the rounding error the old table implied.

**Nothing behaved differently, because nothing reads these numbers.** The field
is documented as feeding a note under the provider selector, and that note does
not exist — it went when the Settings page was cut down. The table is used by
one test and by whoever is deciding which service to point the app at, which is
the more expensive audience to mislead.

**Time-of-day billing is the operational half.** On 16 August 2026 DeepSeek
doubled its rate inside 01:00–04:00 and 06:00–10:00 UTC. Scans run on a fixed
schedule, so a scan time is now a billing decision, and the default schedule
was written when every hour cost the same. In Tallinn (UTC+3) the expensive
windows are 04:00–07:00 and **09:00–13:00** local — which is exactly where a
morning scan naturally lands. A scan moved out of a peak window costs half.

The windows are now data in `providers.py` rather than a fact in a comment.
Nothing warns about them yet.

## The audit, 18-19 August 2026

Nine findings, every one checked against the real database (13 996 jobs) and the
real log (22 MB, 184 634 lines) rather than against the code alone. All nine were
true. Three more came out of the checking, and one of those was twenty times the
size of the thing it was found beside.

### Any website in your browser could read your CV

The app listens on 127.0.0.1 and asks nobody to log in, which is right for a
tool that runs on your own laptop. What was wrong was the other half: it also
answered every website with `Access-Control-Allow-Origin: *`. While it was
running, any page open in the same browser could fetch `/profile` in the
background and read the answer — CV, instructions, case bank — and a plain form
on that page could POST `/jobs/clear`, because a form submission needs no CORS
permission at all. Sixty-three routes, no authentication on any of them.

Two callers are known and expected: the extension, which identifies itself as
`chrome-extension://…`, and the app's own pages. Everything else that announces
where it came from is refused, in the server as well as in the CORS headers —
CORS only asks the browser not to *read* the answer, while a cross-site form
POST arrives and is acted on regardless. A request with no Origin at all — the
desktop window, the scheduler, curl — is not a cross-site request and passes
untouched.

### The database locked, and a whole search went with it

SQLite's default journal blocks readers and writers against each other. This app
has one long writer — a scan storing hundreds of rows — and a steady reader: the
page polling `/api/scan-status` every few seconds while the scan runs. On 18
August at 12:36 they met: the vacancies had been fetched, the model had been
paid, and none of it was stored. Four such collisions in the log.

The database is now in WAL mode, where a reader and a writer proceed at the same
time, and every connection carries a 15-second timeout for what is left, which
is writer against writer. WAL is a property of the file, so it is set once at
startup and stays — including on the database you already have.

### 854 jobs carried a zero no model ever produced

Between 6 and 9 August the scorer returned empty answers, and the code stored
the resulting 0 as though it were a judgement. Those jobs sit below every
threshold, so they appear in no list and nothing would ever have looked at them
again: 6.1% of the database, invisible. A failure to answer is no longer written
as an answer — the job stays unscored and the next cycle picks it up — and
`scripts/rescore_lost.py` repairs the ones already buried.

### A Windows user was never told an update existed

The updater looked for a macOS zip on every platform and gave up quietly when it
did not find one, so the notification never appeared on Windows however
faithfully the installer was attached to the release. It now picks the asset
that belongs to the machine asking, and on Windows it opens the download rather
than swapping files under a running process, which is how Windows installers
work anyway.

### The digest that never arrived, and never said so

A Telegram digest failed on 13 August and again on 18 August at 12:41 — a laptop
whose network was not up yet, and a proxy rewriting TLS. Both times the only
trace was one line in a log nobody opens, and a day with no digest is
indistinguishable from a day with no matches. Delivery is now retried twice, and
the outcome — delivered when, or failed and why — is written where the Settings
page can say it out loud.

### The log grew without limit and kept your bot token in it

21 MB in a month, no rotation, and the Telegram bot token written out in full 41
times: httpx logs every request as its URL, and Telegram puts the token in the
path. Redaction landed on 12 August; rotation lands now, five files of five
megabytes. **The old file is still on your disk and the old token is still in
it** — if it has ever been shared, revoke it in Telegram's BotFather and paste a
new one into Settings.

### What the models cost was arithmetic, never a fact

Every answer carries a usage block — tokens in, tokens out, how many were served
from cache — and the app had never stored one, so every cost figure it or anyone
else produced was measured prompts multiplied by a price list. That arithmetic
was wrong twice: once about the prices (see *Repricing* above), once about
whether caching worked at all. Four numbers per call are now recorded as the
provider reports them, and the Statistic page shows them per stage. Recording
failures are swallowed on purpose: a bookkeeping error must never cost a scored
job.

### `/chat` was excluded by an accident

The rule is that the personal assistant must not be in a build somebody else
runs, because it drives a personal Claude subscription. What actually kept it
out was that `templates/chat.html` had never been written, while the build
copied the templates *folder* — so the day the file appeared it would have
shipped with everything else. The build now lists templates one by one and
excludes that one by name.

### The office check was passing blind on half the shortlist

The finding said 41% of the shortlist had no work arrangement to check. Measured
with the app's own detector over the real database it is **53% of the shortlist
and 62.6% of everything** — and the cause is worse than a gap in the data.
`_with_workplace` exists to stamp an authoritative "Workplace: Remote" onto
public LinkedIn jobs from the structured `jobLocationType` field. It has fired
**zero times in 7 896 jobs**, while `employment_type`, which comes back in the
same request, is filled in on 3 026 of them. The field simply is not on the
guest page. The body-text scanner rescues 2 of the 276 blind shortlist entries.
Nothing is fixed here yet; it is written down because "the gate is on" and "the
gate has anything to look at" turned out to be different statements.

### Found while checking: 84 searches died on a dash

`LookupError: unknown encoding: windows-1252`, thrown by the HTML parser while
it resolved `&#150;` — the en dash half the job ads in Europe are written with.
It came out of a description, travelled up through the source and out of the
scan loop, and ended the whole search: 84 of them over seven days, every one
after a request that had returned 200. Twenty times the damage of the database
locking, and invisible for the same reason — one line in the log.

No test could have caught it, because it cannot happen here: a normal Python
install carries every codec and the packaged app carried only the ones something
imports by name. The build now ships the codecs, and one HTML-to-text reader
sits between every source and the parser, so a description that cannot be parsed
costs that description and not the search around it.

### Found while checking: the update deletes the app out from under itself

The macOS updater writes a script that waits ten seconds for the app to quit and
then removes the bundle — whether it quit or not. Anything the running app had
not already read is gone at that moment, and pages are read from disk when they
are first opened, so the next click gives an internal error. It is in the log
four times, most recently on 18 August at 16:31 on `/search`. Half of this was
fixed on 12 August, for the Profile page; the templates half is still open.

### Found while checking: your network is rewriting TLS

Twenty-three `CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate
chain` in one month, and on 18 August at 12:41 three job sources and the Telegram
digest failed together, in the same second, for that reason. Retrying does not
help against it and the app never names it. Also unnamed: sources fail silently
and often — remoteok 26 times, arbeitnow 19, remotive 18.

### The banner that cried wolf

The extension-health warning (section 8) had a third state beside "paused" and
"stopped": it also complained when the extension had not checked in for half an
hour. But the extension only polls while Chrome is open, so that banner stood on
the Jobs page every evening, every night and every weekend, telling its owner to
check that an extension was enabled in a browser they had deliberately closed. A
warning that is right every evening is furniture, and furniture is what people
stop reading — including on the morning it means something. It now reports only
what the extension says about itself, which survives the browser closing and is
still true the next morning.

## The deep check answers in two parts, 25 August 2026

The deep check had quietly stopped working. On 19 August the evidence findings
were added to it — 6 to 12 quotes, each with a source and a line of proof — but
the room the answer was given to write in stayed sized for the old, shorter
answer. On OpenAI, where there is no separate thinking budget to borrow from,
the reply ran out of room mid-sentence, the half-written JSON would not parse,
and the whole check returned nothing. The button looked broken because it was:
you pressed it and the page came back unchanged. The room is now sized to the
answer it has to hold.

That was the bug. The change is what the wait feels like. The deep check is a
genuinely hard question and takes around fifteen seconds, and for all fifteen
the page used to sit blank and then fill in at once. It is now **two questions
asked at the same time**: the verdict — your score, why you fit, the gaps — and
the evidence, each demand of the advert cited against your record. The verdict
is a short answer, so it lands first and fills its panel while the evidence is
still being gathered. Nothing is slower. You simply see the first half of the
answer while the second half is still coming, instead of waiting for both.

With scripting off, the button still does the old thing: one call, one reload,
the whole answer at once. The two-part version is the enhancement, not the floor.

## A required language is now a blocker, 26 August 2026

A role advertised in English that plainly required Hungarian was scoring **85%**
against a CV with no Hungarian on it. Not a rounding slip — the deep check read
the requirement and still called it a strong match.

The reason is worth stating, because it will happen again with the next
constraint if it is not understood. The score is deliberately answered "as if
every hard constraint were met" — it measures fit to the *role*, and the
blockers are meant to be applied separately, afterward, by rules rather than by
the model's own number. The model's one language rule is about the language the
advert is *written in*: an Italian advert is blocked, an English advert is not.
A required *working* language buried in English text is neither the written
language nor anything the model was told to block, so both the model and the
existing code let it through. 85% for a job you could never be hired into.

Requiring a language you do not speak now blocks the job, checked in code like
every other hard constraint. It fires only on wording that genuinely demands the
language — "fluent Hungarian required", "must speak Hungarian" — and never on
"Hungarian is a plus", which stays a small mark against the score, not a wall.
It reads the languages you named in your own instructions and blocks nothing
until you have named at least one. The job drops to a hard miss both in the fast
scan and in the deep check, with a line saying why.

The first cut of this was too eager, and the fix is the interesting part. It
looked for a demand word anywhere near a language name — but every advert has a
"Requirements" heading, and a heading sits a few words from every noun, so "the
German market", "manage Spanish clients" and "knowledge of the German industry"
all read as language demands and would have hidden perfectly good jobs. The rule
is now adjacency, not proximity: the cue has to sit against the language name —
"fluent German", "German required" — with nothing between them, which is exactly
what separates "knowledge of German" (a language) from "knowledge of the German
market" (a place). Four tests hold that line, because a gate that hides jobs is
the kind that has to be wrong out loud, not silently.

## Settings you can read, 15 September 2026

The AI section opened with "Two different jobs, so you choose a service for
each" — in a job-hunting app, where "jobs" means vacancies, it read as if the
page were about two openings. It now says what it is: AI works in two tiers.
**Scanning** is the first pass over every vacancy, fast and cheap because it runs
on all of them; **the detailed work** is everything after — the advanced check,
cover letters, CV rewrites. The two steps below carry exactly those names, so the
sentence and the headings describe the same thing in the same words.

"Jobs per source — default for a new search" is gone: it set a starting value
you then changed per search anyway.

## The Telegram digest arrives again, 2 October 2026

1.4.0 removed the Telegram digest from Settings. That was a misreading of a
request to tidy the page, not a decision anyone made, and it is back — card,
test button, failure banner and all.

Looking at why it had to come back turned up the real fault: **no digest had
been delivered since 3 September.** Once enough strong matches piled up, the
message ran past Telegram's length limit and was cut at the limit — wherever
that fell, including the middle of a link. Telegram refuses a message with an
unclosed tag, so from 4 to 11 September all 824 attempts were rejected and
nothing arrived. The banner on the Settings page said so; the digest itself
could not. A long digest now holds as many whole jobs as fit, best first, and
ends with "…and N more in Job Finder" — always a message Telegram accepts.

## Google Gemini as a third AI service, 28 September 2026

Gemini now sits next to OpenAI and DeepSeek in both tiers — pick it for
scanning, for the detailed work, or both, with its own key that survives switching
back and forth. The Settings page says plainly that it sends your data outside
the EU, as it does for DeepSeek.

It offers Gemini 2.5 **Flash-Lite** and **Flash**, and deliberately not Pro. Pro
cannot switch off its thinking, and scanning asks every model to, on every job —
so Pro picked for scanning would fail a whole day's scan one vacancy at a time.
A key in Google's format is masked in the log file the same way the others are.

## Gemini scores again for new users, 12 October 2026

A new Gemini user's scan scored nothing: every job came back as a 404. The scan
model it defaulted to, Gemini 2.5 Flash-Lite, is still on Google's price list but
is closed to new accounts. It is replaced by **Gemini 3.1 Flash-Lite**, which
anyone can use, costs $0.25 / $1.50 per million tokens in and out, and was
measured live doing the job. Anyone who had 2.5 Flash-Lite selected is moved to
it automatically. Google's own suggested replacement was tried first and refuses
to switch its thinking off, so it cannot scan.

The Settings page also keeps **Save** in view: the button used to live at the
very bottom of a long page, and changes were easy to make and leave unsaved. It
now sits in a bar along the bottom of the window, says when something is unsaved,
and asks before you leave.

## The code is open, 19 October 2026

Job Finder's source is now public under the Apache License 2.0. The app does
what it did before; nothing about how it scores, writes or stores was changed
to open it.

What did change: the personal assistant tab is gone. It drove a Claude CLI
subscription on the machine it ran on, never had a page, and could not go into
a build anyone else installs, so it was code nobody could use. The rewrite-by-
instruction boxes for the CV and the cover letter are a different feature and
are untouched.

## Gemini 3.5 Flash-Lite can be chosen, 26 October 2026

**Gemini 3.5 Flash-Lite** is now on the Gemini model lists, next to 3.1
Flash-Lite and 2.5 Flash. It was left out on 12 October because it cannot switch
its thinking off: told to, it answers with an error, and as the scan model that
would have failed every job. The app now asks it for its lowest thinking level
instead, which it accepts. Measured live, that level spent no thinking tokens on
a job score.

3.1 Flash-Lite stays the default. 3.5 costs more: $0.30 / $2.50 per million
tokens in and out, against $0.25 / $1.50.

## Years of experience, per search, 26 October 2026

Each saved search now has **Your years of experience** and **Still a fit if
short by up to** (1 year unless you change it). When an advert asks for more
years than that allows, its score is held at 55 at most: it stays in your list,
below the strong matches, with a note saying how many years it asked for. Within
the margin nothing changes, and a search with the box left empty scores exactly
as before.

The comparison is done by the app, not the AI. Left to read years off the CV,
the AI counted eight years as short of "5+"; it is now given your own figure.

Jobs that arrive through the Chrome extension are not covered: the extension
scans its own LinkedIn URLs, which are not linked to a search in the app.

## The AI reads your whole CV, 28 October 2026

The Profile page showed the CV complete, but the AI was handed only its bullet
points: every job title, employer, location and date was left out of what the
advanced check, cover letters and CV edits read. It could not tell your most
senior role from your first, or see when you held either. The AI now gets each
job's heading above its own bullets.

Importing a CV also keeps the one-line description under a company's name,
which was dropped before. Upload your CV again to get those back; the job
headings need no re-upload.

## Every job is scored against your CV, 28 October 2026

The score every job gets used to read only your written instructions, so with
those empty it had nothing to go on: 22 real adverts all scored 80–100,
accountants included. It now also reads your CV: what kind of work you have
done, at what level, in which fields, with which tools, and for how long in
each. On the same 22 adverts, roles outside your field fell to 10–30 and the
To review list went from 22 to 13.

Your CV is evidence, not a rule. Where you live and the languages on your CV
never hide a job by themselves; a role in another city is scored lower and
stays in the list. Write your location and languages in your instructions if
they are rules for you.

## Searches suggested from your CV, 28 October 2026

**Suggest from my CV** on the Searches page reads your CV and proposes one
search for each area of it, such as engineering leadership or AI engineering,
with the job titles to search for and your years in that area, counted from the
dates of the roles that show it. You review and edit each one before anything is
saved.

## Still open

- **The office check is blind on half the shortlist** and the authoritative
  signal it was built to read does not exist on the page it reads. Either the
  arrangement gets fetched from somewhere that has it, or the app stops implying
  it checked.
- **An update still deletes the running app's files.** The swap should wait for
  the process to be gone rather than for a countdown, and the pages should be
  read into memory at startup so a bundle that vanishes cannot produce a 500.
- **Nothing says "your network is intercepting TLS"** when a dozen unrelated
  services fail in the same second with the same certificate error.
- **The Host header is not checked**, so a page that resolves its own hostname to
  127.0.0.1 can still read `/profile` — the requests carry no Origin to refuse.
- **Model usage is recorded on 4 of the 13 call sites.** Scanning is complete;
  the writing side — letters, CV edits, the letterhead — is not, and the two
  streaming calls cannot report usage at all without asking for it.
- **Nothing warns that a scan time falls in DeepSeek's peak window**, and the
  prices are not shown anywhere at all. The Schedule card knows the times, the
  provider knows the windows, and the two have never been introduced.
- **Lever and Ashby** state the employment type and it is not read; only the
  API sources were wired.
- **Salary and seniority band** from Himalayas are discarded.
- **The Clear results control** is one button and a confirmation dialog. There is
  no way to remove only the weak matches, no undo, and no way to see how many
  rows will go before pressing it.
- **`main.py` is 2006 lines and 61 routes.** Splitting it into routers is a day's
  work that pays back the moment the file stops fitting in one head — and it keeps
  growing (the progressive advanced check added three of those routes).

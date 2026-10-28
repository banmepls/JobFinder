"""Career copilot - on-demand generation of application assets for one job.

Three kinds:
  * cover_letter - a tailored cover letter written in the candidate's own voice,
    grounded in the resume + case bank, positioned for this specific role.
  * hr_questions - likely questions an HR/recruiter would ask for this role,
    plus a short angle on how to answer each using the candidate's background.
  * interview_prep - which case-bank stories to tell in THIS interview, the
    angle to take, and the gap to be ready for.

Each function returns Markdown text. Errors never raise - they return a short
message so the UI can show what went wrong. Results are cached in job_assets by
the caller, so generation only runs when the user clicks (cost control).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI

import letter_template
from config import settings
from db import usage

logger = logging.getLogger(__name__)

KINDS = ("cover_letter", "cv_adjustment", "hr_questions", "interview_prep")

KIND_LABELS = {
    "cover_letter": "Cover letter",
    "cv_adjustment": "CV adjustment",
    "hr_questions": "HR questions",
    "interview_prep": "Interview prep",
}

_TIMEOUT = 60.0
_MAX_RETRIES = 2
_MAX_DESC = 6000

_CV_STRUCTURE_SYSTEM = (
    "You convert the RAW TEXT of a résumé (extracted from a PDF, so its layout is "
    "lost) into clean HTML that reproduces a professional résumé structure. "
    "Return ONLY an HTML FRAGMENT (no <html>, <head> or <body>, no markdown, no "
    "code fences). Preserve ALL wording VERBATIM — never invent, drop, summarize "
    "or reorder content. Only add structure.\n"
    "Use EXACTLY these classes/tags:\n"
    '- The person\'s name → <div class="cv-name">NAME</div> (first line).\n'
    '- The headline/title line right after the name → '
    '<div class="cv-subtitle"><strong>…</strong> | location</div>.\n'
    '- The contact line (email / phone / links) → <div class="cv-contact">…</div>.\n'
    '- Each section header (e.g. PROFESSIONAL SUMMARY, EXPERIENCE, PROJECTS, '
    'SKILLS, EDUCATION) → <h2>Title Case</h2>.\n'
    "- A summary paragraph → <p>…</p>.\n"
    '- Each job/project entry → <div class="entry"><div class="entry-head">'
    '<span><strong>Role | Company</strong> | Location</span>'
    '<span class="date">Dates</span></div><ul><li>…</li></ul></div>. '
    "If there is no date, omit the date span.\n"
    "- A line describing the company or project, under its heading → "
    '<p class="entry-desc">…</p> right after the entry-head, before the <ul>. '
    "If one company has several roles, put it in the first of them only.\n"
    "- Bullet points → <li>…</li> inside <ul>. Skills/education lines can be "
    '<ul><li><strong>Label:</strong> …</li></ul>.\n'
    "- Any URL in the text → wrap the relevant words in <a href=\"URL\">…</a>.\n"
    "Output the fragment only."
)


async def structure_cv_html(text: str, model: str | None = None,
                            api_key: str | None = None,
                            base_url: str | None = None) -> str:
    """Rebuild a résumé's structure from raw extracted text using the AI.
    Returns an HTML fragment (our .cv-doc classes), or '' on any failure."""
    key = (api_key or "").strip() or settings.openai_api_key
    if not key or not (text or "").strip():
        return ""
    try:
        client = AsyncOpenAI(api_key=key, base_url=base_url or None,
                             timeout=_TIMEOUT, max_retries=1)
        resp = await client.chat.completions.create(
            model=model or settings.openai_model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": _CV_STRUCTURE_SYSTEM},
                {"role": "user", "content": text[:12000]},
            ],
        )
        html = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("CV structuring failed: %s", exc)
        return ""
    # Strip accidental code fences.
    if html.startswith("```"):
        html = html.split("```", 2)[1] if "```" in html[3:] else html.strip("`")
        html = html.lstrip("html").strip()
    if "<div" not in html and "<h2" not in html:
        return ""
    return html

_SYSTEM = {
    "cover_letter": (
        "You write a cover letter FOR the candidate, in the FIRST person and in "
        "their own natural voice.\n"
        "WHAT THIS LETTER IS FOR. The resume already lists what they have done. "
        "Repeating it here wastes the one page where they can say something a "
        "resume cannot. This letter answers three questions and nothing else:\n"
        "  1. Why THIS company. Something specific from the posting — what they "
        "build, how they say they work, a value or a problem they name. Not "
        "'your innovative culture'. If the posting gives you nothing to work "
        "with, say plainly what about the role itself is worth wanting.\n"
        "  2. Why THIS role, and why now. What the candidate is looking for and "
        "how this particular job matches it.\n"
        "  3. Why them. What the candidate values in how work gets done, and "
        "how that lines up with what this company says it values and how this "
        "team says it operates.\n"
        "EXPERIENCE IS THE EVIDENCE, NOT THE SUBJECT. The resume, the "
        "instructions and the case bank are your material and you should use "
        "them heavily — but shaped as alignment, not as a record.\n"
        "  * Right: 'You are looking for someone to hold delivery together "
        "across teams that disagree. That is most of what I did at X, and it is "
        "the part of the job I actually like.' The experience is real, cited, "
        "and pointed at something the posting asks for.\n"
        "  * Wrong: 'At X I cut delivery time by 30% and coordinated 12 "
        "stakeholders across 4 teams.' That is the resume, and the reader "
        "already has it.\n"
        "Draw on the case bank for what the candidate has actually done and how "
        "they work, then say what it means for THIS role. Numbers are allowed "
        "when they carry the point, but a sentence whose purpose is to deliver a "
        "metric belongs in the resume.\n"
        "Never invent experience, a motivation, or an opinion about the company. "
        "If the posting is thin, write a shorter letter rather than a padded one.\n"
        "Tone: sincere and human, direct, specific. No flattery, no 'I am excited "
        "to apply', no 'I believe I would be a great fit'. Say the real thing.\n"
        "PUNCTUATION AND STYLE - the letter must not read as machine-written:\n"
        "- NEVER use an em dash or en dash. If you want one, use a full stop and "
        "start a new sentence, or a comma. Plain hyphens inside words are fine.\n"
        "- NEVER use a semicolon. Split the sentence in two.\n"
        "- No bullet lists, no bold, no headings. Plain paragraphs only.\n"
        "- Vary sentence length and keep most sentences short. Contractions are "
        "welcome (I've, I'm). Write the way a competent person actually writes.\n"
        "- Ban this vocabulary outright: delve, leverage, spearheaded, "
        "passionate, seamless, robust, cutting-edge, dynamic, synergy, "
        "showcase, tapestry, testament, landscape, realm, moreover, furthermore, "
        "as a seasoned, I am confident that my, I am writing to express. Say the "
        "plain thing instead.\n"
        "- No sentence of the shape 'Not only X, but also Y' and no 'It is not "
        "just about X, it is about Y'.\n"
        "Length: 180-260 words in 3-4 short paragraphs. Shorter is better than "
        "padded.\n"
        "COMPLETENESS: the letter must be ready to send. Start with a greeting "
        "('Dear Hiring Team,' if no named contact) and end with a sign-off "
        "('Best regards,') followed by the candidate's REAL name taken from the "
        "resume, in normal Title Case. Output ONLY the letter — no notes, no "
        "placeholders in square brackets, no commentary.\n"
    ),
    "hr_questions": (
        "You are a senior recruiter. Produce EXACTLY 10 questions an HR screener "
        "/ hiring manager would most likely ask for THIS specific role, given the "
        "candidate's background.\n"
        "FORMAT (strict): number ONLY the questions, as a Markdown numbered list "
        "(1., 2., …). Put the QUESTION in bold. On the NEXT line under each "
        "question, add exactly one advice line that begins with '**Tip:**' and is "
        "NOT numbered — how the candidate should angle their answer using their "
        "real resume/case-bank evidence. Example:\n"
        "1. **How do you prioritise competing deadlines?**\n"
        "**Tip:** Anchor on your delivery-lead-time win and DoR/DoD process.\n"
        "Never number the tips; never merge a tip into the question line."
    ),
    "interview_prep": (
        "You are an interview coach. The candidate is walking into an interview "
        "for THIS vacancy. They already know their own history, so do not retell "
        "it back to them — decide what to say, and what to leave out.\n"
        "THE CENTRE OF THIS BRIEF IS THE CASE BANK. Those are the stories the "
        "candidate can actually tell, and picking the right two or three is the "
        "whole job. Choose on evidence: the ones whose real facts answer what "
        "this posting asks for. A weaker story that lands on the role's main "
        "requirement beats a stronger one that does not.\n"
        "Markdown, UNDER 250 WORDS, exactly three sections:\n"
        "'## Your angle' — 2–3 bullets. The line to take about why they fit, in "
        "the terms this company uses.\n"
        "'## Stories to tell' — the 2–4 best case-bank items. For each: the case "
        "TITLE exactly as written in the case bank, in bold; then one line on the "
        "question it answers, and one line on the fact or number to lead with. "
        "Nothing else.\n"
        "'## Be ready for' — 2 bullets. The gap this role will probe, and the "
        "honest answer to give.\n"
        "Never invent a case, a number or an outcome that is not in the case bank "
        "or the resume. If the case bank is empty, say so in one line and build "
        "the stories from the resume instead. No preamble, no closing pep talk."
    ),
}

# CV adjustment is generated as STRUCTURED edits (not prose) so each bullet can
# be applied to the CV document individually - the point is targeted swaps, not
# a full rewrite.
_CV_EDITS_SYSTEM = (
    "You are a CV editor. You are given the candidate's ENTIRE CV as text: a "
    "HEADLINE line, a professional summary, section headers, skills/tools lines, "
    "and experience bullets (lines starting with '-'). Suggest targeted rewrites "
    "across ANY of these — the headline, the summary, the skills/tools lines, and "
    "the bullets — so the CV mirrors THIS role's language, priorities and "
    "keywords and foregrounds the candidate's TRANSFERABLE fit.\n"
    "TRUTHFULNESS — ABSOLUTE RULE: never lie, exaggerate, or invent. Every claim, "
    "metric, tool, employer and outcome in an 'after' MUST already be present in "
    "the candidate's CV or CASE BANK. Do NOT add skills they never listed, invent "
    "numbers, or imply seniority/experience they do not have. You may only reword, "
    "re-angle, and surface REAL facts — if a role wants something the candidate "
    "lacks, do not fabricate it.\n"
    "USE THE CASE BANK: review the CASE BANK section and, where a proven project "
    "or achievement there strengthens the fit for THIS role, put it in 'additions' "
    "(and, if it clearly belongs in an existing bullet, weave the real detail in). "
    "Only use case-bank facts verbatim in meaning — never embellish them.\n"
    "Do NOT change: the candidate's name, contact details, employer names, dates, "
    "or section header titles.\n"
    "You MUST return AT LEAST 3 edits spanning different parts where useful "
    "(e.g. headline + summary + a couple of bullets). If the role differs from "
    "the candidate's background, reframe the transferable angle (never fabricate). "
    "An empty edits list is NOT acceptable.\n"
    "The 'before' MUST be copied VERBATIM from a line above — the exact text only, "
    "WITHOUT any leading '-' or 'HEADLINE:' label — so it can be matched and "
    "replaced. The 'after' keeps all metrics.\n"
    "An EDIT always replaces an EXISTING line, so 'before' can NEVER be empty. If "
    "you want to ADD something the CV does not already have (e.g. a headline or a "
    "summary the CV lacks), put that text in 'additions' — do NOT emit an edit "
    "with an empty 'before'. Each 'after' is ONE line/bullet, never several "
    "bullets joined together.\n"
    "CRITICAL — LENGTH: the 'after' MUST be roughly the SAME LENGTH as the "
    "'before' (within about ±15% of its word count). NEVER shorten a bullet into "
    "a terse fragment: rewrite it as a full, detailed bullet of comparable length. "
    "If 'before' is one long sentence, 'after' is one long sentence; match its "
    "depth and specificity. Reword and re-angle — do not compress.\n"
    "The professional SUMMARY is the most common failure: its 'after' MUST keep "
    "the SAME number of sentences and the SAME overall length as the original "
    "summary — do NOT reduce a multi-sentence summary to one short line.\n"
    "For each edit include a very short 'why' (max 8 words) explaining the reason "
    "for the change (e.g. 'mirrors the role's stakeholder-management keyword').\n"
    "Return ONLY valid JSON:\n"
    "{\n"
    '  "edits": [{"before": "<verbatim CV bullet>", "after": "<tailored bullet>", "why": "<max 8 words>"}],\n'
    '  "additions": ["<case-bank achievement worth adding for this role>"]\n'
    "}\n"
    "Return 3–6 edits. additions: 0–3 items or empty. Nothing outside the JSON."
)


def _strip_bullet(s: str) -> str:
    """Remove any leading/trailing list markers or dashes the model may add
    around a bullet, so applied CV lines never gain a stray '-'."""
    return (s or "").strip().strip("-–—•*·").strip()


def _context(job: dict[str, Any], profile: dict[str, Any], brief: str = "") -> str:
    desc = (job.get("description") or "")[:_MAX_DESC]
    # The brief is what the user typed for THIS job ("mention my Shopify work").
    # It goes first and is marked as an instruction so it outranks the generic
    # system prompt when the two disagree.
    brief_block = (
        "=== WHAT THE CANDIDATE ASKED YOU TO INCLUDE (highest priority) ===\n"
        f"{brief.strip()}\n"
        "Work every point above into the text naturally. Do not invent evidence "
        "for them — if the resume or case bank has nothing to back a point, "
        "state it plainly and briefly rather than fabricating detail.\n\n"
    ) if brief and brief.strip() else ""
    return (
        brief_block +
        "=== CANDIDATE RESUME ===\n"
        f"{(profile.get('resume_text') or '').strip() or '(empty)'}\n\n"
        "=== CANDIDATE INSTRUCTIONS / PREFERENCES ===\n"
        f"{(profile.get('instructions') or '').strip() or '(none)'}\n\n"
        "=== CASE BANK (proven projects / achievements) ===\n"
        f"{(profile.get('case_bank') or '').strip() or '(none)'}\n\n"
        "=== VACANCY ===\n"
        f"Title: {job.get('title')}\n"
        f"Company: {job.get('company')}\n"
        f"Location: {job.get('location')}\n"
        f"Description:\n{desc or '(no description)'}\n"
    )




# Referral outreach. Two genres because LinkedIn gives two channels with very
# different shapes, not one channel with a length setting.
_OUTREACH_SYSTEM = {
    "note": (
        "You write the 200-character note attached to a LinkedIn connection "
        "request, in the FIRST person, as the candidate.\n"
        "This is the hardest text in the product: 200 characters INCLUDING "
        "spaces, and its only job is to make the person accept. Do not try to "
        "sell the whole candidacy.\n"
        "Structure: name the specific role you are interested in at their "
        "company, give ONE concrete reason you are relevant, ask nothing else. "
        "No greeting line, no sign-off, no 'I hope this finds you well'.\n"
        "Free LinkedIn accounts get about five of these per MONTH, so this one "
        "has to be worth spending.\n"
        + "Style rules, the same ones the cover letter follows, because a message that reads as machine-written is worse than no message:\\n- NEVER use an em dash or en dash. Use a full stop or a comma.\\n- NEVER use a semicolon.\\n- No bullet lists, no headings, no bold.\\n- Ban this vocabulary outright: delve, leverage, spearheaded, passionate, seamless, robust, cutting-edge, dynamic, synergy, showcase, reach out, touch base, I hope this message finds you well, as a seasoned, I am confident that my. Say the plain thing instead.\\n- No 'Not only X, but also Y'.\\n- Never invent experience. Everything you claim must come from the resume or the case bank.\\n" +
        "Output ONLY the note text. No quotes around it, no commentary, no "
        "character count. Stay under 200 characters."
    ),
    "message": (
        "You write a LinkedIn message FOR the candidate, in the FIRST person, to "
        "someone who is already a connection.\n"
        "Goal: a referral or an internal introduction for the specific role "
        "below. Be direct about what you are asking for. People help when the "
        "ask is small and clear.\n"
        "Structure: one line on who you are and why you are writing to THEM "
        "specifically (their role, their team). Two or three sentences of "
        "evidence drawn from the case bank, with the numbers attached, mapped to "
        "what this role actually asks for. One clear, small ask. A short "
        "sign-off.\n"
        "Length: 90 to 140 words. Longer does not get read.\n"
        + "Style rules, the same ones the cover letter follows, because a message that reads as machine-written is worse than no message:\\n- NEVER use an em dash or en dash. Use a full stop or a comma.\\n- NEVER use a semicolon.\\n- No bullet lists, no headings, no bold.\\n- Ban this vocabulary outright: delve, leverage, spearheaded, passionate, seamless, robust, cutting-edge, dynamic, synergy, showcase, reach out, touch base, I hope this message finds you well, as a seasoned, I am confident that my. Say the plain thing instead.\\n- No 'Not only X, but also Y'.\\n- Never invent experience. Everything you claim must come from the resume or the case bank.\\n" +
        "Output ONLY the message text, ready to paste."
    ),
}


# LinkedIn counts characters, not intentions. Models overshoot a stated limit
# often enough that the cap is enforced here as well, cutting at a sentence
# boundary so the note still reads as finished.
def _fit_note(text: str, limit: int = 200) -> str:
    text = " ".join(text.split()).strip('"').strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for stop in (". ", "! ", "? "):
        idx = cut.rfind(stop)
        if idx > limit * 0.5:
            return cut[:idx + 1].strip()
    return cut[:cut.rfind(" ")].strip() if " " in cut else cut


class CopilotGenerator:
    def __init__(self, model: str | None = None, api_key: str | None = None,
                 base_url: str | None = None, reasoning_reserve: int = 0,
                 timeout: float | None = None, no_reasoning: dict | None = None) -> None:
        # Both accepted and ignored, for different reasons. `reasoning_reserve`:
        # generation is not capped by max_tokens here, so a reasoning model
        # already has all the room it needs. `no_reasoning`: this is the prose a
        # person sends to an employer, the one place worth letting a model
        # think. They are in the signature because every client is built from
        # the same helper, and one that quietly rejects an argument fails at the
        # moment of use - a 500 on the cover letter, which is exactly what
        # leaving `no_reasoning` out of this line shipped in 1.2.0.
        self.reserve = max(0, int(reasoning_reserve))
        self.timeout = float(timeout or _TIMEOUT)
        self.model = model or settings.openai_model
        self.init_error: str | None = None
        self._client: AsyncOpenAI | None = None
        key = (api_key or "").strip() or settings.openai_api_key
        if not key:
            self.init_error = "OPENAI_API_KEY not configured (set it in Settings)."
        else:
            try:
                self._client = AsyncOpenAI(
                    api_key=key,
                    base_url=base_url or None,
                    timeout=self.timeout,
                    max_retries=_MAX_RETRIES,
                )
            except Exception as exc:  # noqa: BLE001
                self.init_error = f"OpenAI client init failed: {exc}"
                logger.error(self.init_error)

    async def generate(
        self, kind: str, job: dict[str, Any], profile: dict[str, Any],
        brief: str = "",
    ) -> str:
        if kind not in KINDS:
            return f"Unknown asset type: {kind}"
        if self._client is None:
            return f"Cannot generate: {self.init_error or 'OpenAI unavailable.'}"
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                temperature=0.5,
                messages=[
                    {"role": "system", "content": _SYSTEM[kind]},
                    {"role": "user", "content": _context(job, profile, brief)},
                ],
            )
            await usage.record("write", self.model, getattr(resp, "usage", None))
            return (resp.choices[0].message.content or "").strip() or "(empty response)"
        except Exception as exc:  # noqa: BLE001
            logger.error("Copilot %s failed for job %s: %s", kind, job.get("id"), exc)
            return f"Generation failed ({kind}): {exc}"

    async def outreach(
        self, contact: dict[str, Any], job: dict[str, Any],
        profile: dict[str, Any], kind: str, brief: str = "",
    ) -> str:
        """One referral draft. `kind` is 'note' (200 chars) or 'message'.

        Two genres, not one with a length knob: a 200-character invite note has
        room for a single idea and must earn a click, while a message to someone
        who already accepted can carry a case-bank story. Writing one and
        trimming it produces a bad version of both."""
        if kind not in ("note", "message"):
            return f"Unknown outreach type: {kind}"
        if self._client is None:
            return f"Cannot generate: {self.init_error or 'OpenAI unavailable.'}"
        who = (
            "=== PERSON YOU ARE WRITING TO ===\n"
            f"Name: {contact.get('name')}\n"
            f"Headline: {contact.get('headline') or '(unknown)'}\n"
            f"Relationship to the role: {contact.get('audience') or 'other'}\n"
        )
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                temperature=0.6,
                messages=[
                    {"role": "system", "content": _OUTREACH_SYSTEM[kind]},
                    {"role": "user", "content": who + "\n" + _context(job, profile, brief)},
                ],
            )
            text = (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("Outreach %s failed for contact %s: %s",
                         kind, contact.get("id"), exc)
            return f"Generation failed ({kind}): {exc}"
        if not text:
            return "(empty response)"
        if kind == "note":
            text = _fit_note(text)
        return text

    async def cv_edits(
        self, job: dict[str, Any], profile: dict[str, Any]
    ) -> dict[str, Any]:
        """Structured, per-bullet CV suggestions ({edits:[{before,after}], additions:[]})."""
        if self._client is None:
            return {"edits": [], "additions": [],
                    "error": self.init_error or "OpenAI unavailable."}
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                temperature=0.3,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _CV_EDITS_SYSTEM},
                    {"role": "user", "content": _context(job, profile)},
                ],
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.error("CV edits failed for job %s: %s", job.get("id"), exc)
            return {"edits": [], "additions": [], "error": str(exc)}

        edits = []
        additions = [str(a).strip() for a in data.get("additions", []) if a]
        for e in data.get("edits", []):
            if not isinstance(e, dict):
                continue
            before = _strip_bullet(str(e.get("before") or ""))
            after = _strip_bullet(str(e.get("after") or ""))
            if not after:
                continue
            # An edit needs a real line to REPLACE. If there's no verbatim
            # 'before' (the model is proposing brand-new content), it's an
            # addition, not an appliable edit - move it there instead of showing
            # a broken "Could not match" card.
            if not before:
                additions.append(after)
                continue
            edits.append({"before": before, "after": after,
                          "why": str(e.get("why", "")).strip()})
        edits = await self._enforce_length(edits)
        logger.info(
            "CV edits for job %s: %d edits, %d additions (raw %d chars)",
            job.get("id"), len(edits), len(additions), len(raw),
        )
        if not edits:
            # Surface WHY nothing came back, so it's debuggable from the UI.
            logger.warning("CV edits empty. Raw model output: %.500s", raw)
        return {"edits": edits[:6], "additions": additions[:3]}

    async def _enforce_length(self, edits: list[dict]) -> list[dict]:
        """Guarantee the tailored bullets keep roughly the original length.
        Any 'after' shorter than ~75% of its 'before' (word count) is sent back
        for a single expansion pass. Best-effort: on error the edits pass through
        unchanged, each flagged so the UI can show it was left short."""
        def wc(s: str) -> int:
            return len((s or "").split())

        short = [e for e in edits
                 if wc(e["before"]) >= 8 and wc(e["after"]) < 0.85 * wc(e["before"])]
        if not short or self._client is None:
            return edits
        payload = "\n".join(
            f'{i}. TARGET_WORDS={wc(e["before"])} | BEFORE: {e["before"]} | AFTER: {e["after"]}'
            for i, e in enumerate(short)
        )
        sys = (
            "You expand CV bullets that were rewritten too short. For each item, "
            "rewrite AFTER so it has about TARGET_WORDS words (within ±10%), keeping "
            "its meaning, every metric, and the role-tailored angle — just add the "
            "detail/specificity that was lost. Never pad with fluff. Return ONLY "
            'JSON: {"fixes": [{"i": <index>, "after": "<expanded>"}]}'
        )
        try:
            resp = await self._client.chat.completions.create(
                model=self.model, temperature=0.3,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": sys},
                          {"role": "user", "content": payload}],
            )
            fixes = json.loads(resp.choices[0].message.content or "{}").get("fixes", [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("CV length-repair failed: %s", exc)
            for e in short:
                e["short"] = True
            return edits
        by_i = {int(f["i"]): str(f["after"]).strip()
                for f in fixes if isinstance(f, dict) and f.get("after")}
        for idx, e in enumerate(short):
            new_after = _strip_bullet(by_i.get(idx, ""))
            if new_after and wc(new_after) >= 0.75 * wc(e["before"]):
                e["after"] = new_after
            else:
                e["short"] = True
        return edits


# --------------------------------------------------------------------------- #
# The letterhead, described in words                                          #
# --------------------------------------------------------------------------- #

_LETTERHEAD_SYSTEM = (
    "You lay out the LETTERHEAD a cover letter is printed on, from a one-line "
    "description the candidate typed. You are a typesetter, not a writer: you "
    "never write the letter, and you never invent the candidate's details.\n"
    "You are given the letterhead as it stands and the candidate's own sender "
    "block, taken from their CV. Reproduce the sender block EXACTLY as given — "
    "same name, same address, same contacts, character for character. You may "
    "move it, align it or restyle it. You may not retype it.\n"
    "PLACEHOLDERS. These are filled in per application and must be left as "
    "written, braces included:\n"
    "  {{date}} {{company}} {{company_address}} {{job_title}} {{body}}\n"
    "{{body}} is the letter and MUST be present. Drop one of the others only if "
    "the description clearly asks for it (\'no date\', \'no addressee\'). Never "
    "invent a placeholder of your own.\n"
    "OUTPUT. An HTML fragment only: no <html>, <head>, <body>, no markdown, no "
    "code fences, no commentary. Style with inline `style=\"...\"` attributes or "
    "the existing classes (sender, sender__name, date, recipient, "
    "recipient__company, subject, body). Do not emit a <style> block — the "
    "print stylesheet is not yours to change.\n"
    "Change only what the description asks for. Everything else keeps its "
    "current markup. Output the fragment and nothing else."
)


async def letterhead_from_brief(
    brief: str, current: str, sender_block: str = "", model: str | None = None,
    api_key: str | None = None, base_url: str | None = None,
    timeout: float | None = None, **_ignored: Any,
) -> tuple[str, str]:
    """Re-lay-out the letterhead from a short description. Returns (html, error).

    Never raises and never returns a half-template: a letterhead that lost
    {{body}} prints a heading with no letter under it, and the user would not
    find out until the PDF. So the placeholder check happens here, in code,
    rather than being asked of the model and hoped for."""
    brief = " ".join((brief or "").split())
    if not brief:
        return "", "Describe the letterhead first."
    if len(brief) > letter_template.BRIEF_MAX:
        return "", (f"Keep it under {letter_template.BRIEF_MAX} characters "
                    f"({len(brief)} given).")
    key = (api_key or "").strip() or settings.openai_api_key
    if not key:
        return "", "No API key for the writing service (set it in Settings)."
    try:
        client = AsyncOpenAI(api_key=key, base_url=base_url or None,
                             timeout=float(timeout or _TIMEOUT), max_retries=1)
        resp = await client.chat.completions.create(
            model=model or settings.openai_model,
            temperature=0.3,
            messages=[
                {"role": "system", "content": _LETTERHEAD_SYSTEM},
                {"role": "user", "content": (
                    "=== LETTERHEAD AS IT STANDS ===\n"
                    f"{current or letter_template.DEFAULT_TEMPLATE}\n\n"
                    "=== THE CANDIDATE\'S OWN BLOCK (reproduce verbatim) ===\n"
                    f"{sender_block or '(none — keep whatever the letterhead has)'}\n\n"
                    "=== WHAT THEY ASKED FOR ===\n"
                    f"{brief}\n"
                )},
            ],
        )
        html = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001 - shown in the panel, never raised
        logger.error("Letterhead layout failed: %s", exc)
        return "", f"Could not redraw the letterhead: {exc}"

    if html.startswith("```"):
        html = html.split("\n", 1)[-1] if "\n" in html else html.strip("`")
        html = html.rsplit("```", 1)[0].strip()
    if not html:
        return "", "The model returned nothing."
    if letter_template.REQUIRED in letter_template.missing_tokens(html):
        # The one failure that is invisible until the PDF is printed.
        return "", ("That would have removed the letter itself from the page. "
                    "Nothing was changed — try describing the layout only.")
    return html, ""


# --------------------------------------------------------------------------- #
# Conversational CV editing                                                   #
# --------------------------------------------------------------------------- #

_CV_CHAT_SYSTEM = (
    "You edit a résumé that is already written in HTML, following the "
    "instruction the candidate just typed. You are their editor, not their "
    "author.\n"
    "RULES:\n"
    "1. Return the COMPLETE résumé as an HTML fragment - every section, in "
    "order, including the parts you did not touch. Never return only the edited "
    "bit, never summarise what you changed, never add commentary or code "
    "fences. The output replaces the document wholesale.\n"
    "2. Change ONLY what the instruction asks for. Everything else stays "
    "character for character identical.\n"
    "3. Keep the existing structure and classes exactly: div.cv-name, "
    "div.cv-subtitle, div.cv-contact, h2, div.entry, div.entry-head, span.date, "
    "p.entry-desc, ul, li, p, a. Do not invent new classes or reorder sections "
    "unless asked.\n"
    "4. NEVER invent experience, employers, dates, tools or numbers. You may "
    "rephrase, shorten, merge, split, reorder bullets and change emphasis. If "
    "the instruction needs a fact that is not already in the résumé or in the "
    "context below, leave it out and keep the existing wording.\n"
    "5. Write like a person: no em dashes, no semicolons, no filler such as "
    "'spearheaded', 'leveraged' or 'passionate'. Concrete verbs and numbers.\n"
    "Output the HTML fragment and nothing else."
)


async def stream_cv_edit(
    instruction: str,
    cv_html: str,
    profile: dict[str, Any] | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
):
    """Yield the rewritten CV as it is generated, chunk by chunk.

    Streaming is the point: the document is long, and a rewrite that arrives all
    at once after ten silent seconds feels broken. The caller pipes these chunks
    straight into the editor so the user watches the CV being rewritten."""
    key = (api_key or "").strip() or settings.openai_api_key
    if not key:
        yield "<!-- error: OpenAI key not configured (Settings) -->"
        return
    profile = profile or {}
    context = (
        "=== CURRENT RESUME (HTML) ===\n"
        f"{cv_html}\n\n"
        "=== CANDIDATE INSTRUCTIONS / PREFERENCES (background, not a command) ===\n"
        f"{(profile.get('instructions') or '').strip() or '(none)'}\n\n"
        "=== CASE BANK (facts you may draw on; do not invent beyond it) ===\n"
        f"{(profile.get('case_bank') or '').strip() or '(none)'}\n\n"
        "=== WHAT TO CHANGE ===\n"
        f"{instruction.strip()}\n"
    )
    try:
        client = AsyncOpenAI(api_key=key, base_url=base_url or None,
                             timeout=_TIMEOUT, max_retries=1)
        stream = await client.chat.completions.create(
            model=model or settings.openai_model,
            temperature=0.3,
            stream=True,
            messages=[
                {"role": "system", "content": _CV_CHAT_SYSTEM},
                {"role": "user", "content": context},
            ],
        )
        started = False
        async for chunk in stream:
            piece = (chunk.choices[0].delta.content or "") if chunk.choices else ""
            if not piece:
                continue
            # Models like to open with ```html despite being told not to.
            if not started:
                piece = piece.lstrip()
                if piece.startswith("```"):
                    piece = piece.split("\n", 1)[-1] if "\n" in piece else ""
                if not piece:
                    continue
                started = True
            yield piece
    except Exception as exc:  # noqa: BLE001 - surfaced to the user in the panel
        logger.error("CV chat edit failed: %s", exc)
        yield f"<!-- error: {exc} -->"


_LETTER_CHAT_SYSTEM = (
    "You revise a cover letter that already exists, following the instruction "
    "the candidate just typed. You are their editor, not their author.\n"
    "RULES:\n"
    "1. Return the COMPLETE letter as simple HTML paragraphs (<p>...</p>), "
    "greeting and sign-off included. Never return only the changed part, never "
    "add commentary or code fences. The output replaces the letter wholesale.\n"
    "2. Change ONLY what the instruction asks for. Leave the rest as it is.\n"
    "3. NEVER invent experience, employers, numbers or claims. Work only from "
    "the letter, the resume and the case bank below.\n"
    "4. Keep the writing human: no em dashes, no semicolons, no bullet lists, "
    "no headings. Short sentences, contractions welcome. Never use: delve, "
    "leverage, spearheaded, passionate, seamless, robust, cutting-edge, "
    "synergy, showcase, testament, moreover, furthermore.\n"
    "Output the HTML paragraphs and nothing else."
)


async def stream_letter_edit(
    instruction: str,
    letter_text: str,
    job: dict[str, Any],
    profile: dict[str, Any] | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
):
    """Yield the rewritten cover letter as it is generated, chunk by chunk.

    Same contract as `stream_cv_edit`: the caller pipes the chunks straight into
    the editor so the user watches the letter being rewritten."""
    key = (api_key or "").strip() or settings.openai_api_key
    if not key:
        yield "<!-- error: OpenAI key not configured (Settings) -->"
        return
    profile = profile or {}
    context = (
        "=== CURRENT COVER LETTER ===\n"
        f"{letter_text}\n\n"
        "=== THE ROLE ===\n"
        f"{job.get('title')} at {job.get('company')} ({job.get('location')})\n\n"
        "=== CANDIDATE RESUME (facts you may use) ===\n"
        f"{(profile.get('resume_text') or '').strip() or '(empty)'}\n\n"
        "=== CASE BANK (facts you may use) ===\n"
        f"{(profile.get('case_bank') or '').strip() or '(none)'}\n\n"
        "=== WHAT TO CHANGE ===\n"
        f"{instruction.strip()}\n"
    )
    try:
        client = AsyncOpenAI(api_key=key, base_url=base_url or None,
                             timeout=_TIMEOUT, max_retries=1)
        stream = await client.chat.completions.create(
            model=model or settings.openai_model,
            temperature=0.4,
            stream=True,
            messages=[
                {"role": "system", "content": _LETTER_CHAT_SYSTEM},
                {"role": "user", "content": context},
            ],
        )
        started = False
        async for chunk in stream:
            piece = (chunk.choices[0].delta.content or "") if chunk.choices else ""
            if not piece:
                continue
            if not started:
                piece = piece.lstrip()
                if piece.startswith("```"):
                    piece = piece.split("\n", 1)[-1] if "\n" in piece else ""
                if not piece:
                    continue
                started = True
            yield piece
    except Exception as exc:  # noqa: BLE001
        logger.error("Letter chat edit failed: %s", exc)
        yield f"<!-- error: {exc} -->"

"""Which service the models are called on.

Everything here speaks the OpenAI wire format, so switching provider is a base
URL and a key rather than a new client. What differs is price, and it differs
most on the part of the prompt that repeats: DeepSeek bills a cache hit at
$0.007 per million tokens against OpenAI's $0.10, and this application's prompts
are 90% repeated prefix.

Prices are checked against the published lists and dated in the comments. They
move: DeepSeek doubled its rate inside two windows of the day on 16 August 2026,
which turns the scan schedule into a billing decision.

Adding a provider means adding an entry here. Nothing else in the app knows a
provider exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

OPENAI = "openai"
DEEPSEEK = "deepseek"
GEMINI = "gemini"

# The two jobs a model does here, which have nothing in common but the wire
# format. SCAN is judgement at volume: thousands of calls a day, every one of
# them answering with a number nobody reads as prose, and the bill is decided by
# the price of a cached input token. WRITE is a handful of calls a day whose
# output a human reads and sends to an employer, where the price is irrelevant
# and the prose is not.
#
# Measured on the same 32-job eval set and repriced against the published lists on
# 17 Aug 2026: DeepSeek without reasoning costs $0.29 per 1300 jobs off-peak and
# $0.59 inside the peak windows, against OpenAI's $0.69 - and agrees with it on
# the 60+ cut. Cheaper, then, but by 2.4x off-peak rather than the 5x this file
# used to claim, and by almost nothing at peak. It is still the right tool for
# SCAN and still says nothing at all about cover letters.
SCAN = "scan"
WRITE = "write"

# stage -> role. 'fast' is the title screen and quick score. 'write' is
# everything a person reads.
#
# 'deep' - the advanced check - sat under SCAN because it is judgement, which
# was the wrong test. What makes a cheap endpoint worth its latency is VOLUME,
# and the advanced check has none: it is a handful of calls a day, each one
# with a person waiting at the screen. Measured on the live app: 55 seconds on
# DeepSeek's reasoning model, ending in an empty answer because the reasoning
# ate the entire output budget. So it belongs with the other work someone waits
# for, where latency counts and price does not.
_ROLE_OF_STAGE = {"fast": SCAN, "deep": WRITE, "write": WRITE}


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    base_url: str | None          # None = the SDK's own default (OpenAI)
    models: tuple[str, ...]
    default_fast: str
    default_deep: str
    # Where the user gets a key, shown next to the field.
    console: str
    # Whether choosing this service sends job descriptions, the CV and the
    # user's instructions outside the EU. A property of the provider, not of the
    # page that happens to render the warning, so a new entry cannot be added
    # without answering the question.
    leaves_eu: bool = True
    # $ per million tokens: (input, cached input, output). Checked against the
    # published price list, with the date it was checked, because a stale number
    # here is worse than none: it is the basis for choosing a provider at all.
    price: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    # Hours (UTC) in which `price` is doubled, as half-open [start, end) ranges.
    # DeepSeek moved to time-of-day billing on 16 August 2026 and the app scans
    # on a schedule, so which hour a scan runs in is now a billing decision.
    peak_hours_utc: tuple[tuple[int, int], ...] = ()
    peak_multiplier: float = 1.0
    # Tokens the model spends thinking before it writes anything. A reasoning
    # model given a 50-token budget burns all 50 on reasoning and returns an
    # EMPTY string with finish_reason="length" - which looked exactly like the
    # provider being broken. The budgets in matcher.py size the ANSWER; this is
    # added on top.
    reasoning_reserve: int = 0
    # Seconds to wait for an answer. A reasoning model thinks before it writes,
    # and on the deep check - full description plus CV plus case bank - that
    # took longer than the 18 seconds tuned for a model that answers directly.
    timeout: float = 18.0
    # …and writing gets its own, because it is a different question. Scoring
    # returns a number; a cover letter or a page of HR answers is prose, and it
    # takes longer to produce. `generate.py` always used 60 seconds for exactly
    # this reason. When one shared `timeout` started being passed to every
    # client, that 60 was silently replaced by the scorer's 18 - with two
    # retries on top, HR questions spent a minute failing.
    write_timeout: float = 60.0
    # Extra request fields that switch thinking OFF, for the stages that do not
    # need it. Measured: the same title screen costs 223 output tokens with
    # reasoning and 28 without, for the same verdict. Empty when the provider
    # has no such switch.
    no_reasoning: dict = field(default_factory=dict)
    # …and, per model, what to send instead for a model that cannot switch its
    # thinking off at all. Sent `no_reasoning`, such a model answers every job
    # of a scan with a 400; asked for its lowest level, it scans at the cost of
    # a few thought tokens a job. A model not listed here takes `no_reasoning`.
    least_reasoning: dict[str, dict] = field(default_factory=dict)


PROVIDERS: dict[str, Provider] = {
    OPENAI: Provider(
        key=OPENAI,
        label="OpenAI",
        base_url=None,
        models=("gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini", "gpt-4o"),
        default_fast="gpt-4.1-mini",
        default_deep="gpt-4.1",
        console="platform.openai.com",
        leaves_eu=False,
        # Checked 17 Aug 2026 against developers.openai.com/api/docs/pricing.
        # The 4.1 family caches at a quarter of the input price, not a half -
        # this table had both at a half, which overstated the OpenAI bill.
        price={
            "gpt-4o-mini": (0.15, 0.075, 0.60),
            "gpt-4.1-mini": (0.40, 0.10, 1.60),
            "gpt-4.1": (2.00, 0.50, 8.00),
            "gpt-4o": (2.50, 1.25, 10.00),
        },
    ),
    DEEPSEEK: Provider(
        key=DEEPSEEK,
        label="DeepSeek",
        base_url="https://api.deepseek.com",
        models=("deepseek-v4-flash", "deepseek-v4-pro"),
        default_fast="deepseek-v4-flash",
        default_deep="deepseek-v4-pro",
        console="platform.deepseek.com",
        # Measured against the live API. A title screen spends 28-176 tokens
        # thinking; the full score, a far harder question, spent 652 and once
        # ran past 1060 - the whole budget - returning an empty string with
        # finish_reason="length". max_tokens is a ceiling and unused tokens are
        # not billed, so this is set well clear of the worst observed run.
        reasoning_reserve=2500,
        timeout=120.0,
        write_timeout=180.0,
        no_reasoning={"reasoning_effort": "none"},
        # Checked 17 Aug 2026 against api-docs.deepseek.com/quick_start/pricing.
        # These are the OFF-PEAK rates, which apply for 16 hours of the day.
        # The previous entries were roughly a third of the real price on every
        # axis and had never matched the published list.
        price={
            "deepseek-v4-flash": (0.22, 0.007, 0.66),
            "deepseek-v4-pro": (0.66, 0.022, 1.98),
        },
        # 01:00-04:00 and 06:00-10:00 UTC bill at double. Introduced 16 Aug 2026.
        peak_hours_utc=((1, 4), (6, 10)),
        peak_multiplier=2.0,
    ),
    GEMINI: Provider(
        key=GEMINI,
        label="Google Gemini",
        # Google's OpenAI-compatible endpoint: same SDK, same chat.completions,
        # same JSON mode - only the base URL and the key change.
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        # Flash-Lite and Flash only, deliberately. 2.5 Pro cannot switch its
        # thinking off, and the scanning stages send `reasoning_effort: none` -
        # Pro picked as the scan model would answer every single job with a 400.
        # Both lists are shared by the two roles, so the trap cannot be kept to
        # one of them; leaving Pro out is the only safe way to offer the rest.
        #
        # Not 2.5 Flash-Lite either: it is still on the price list, but Google
        # answers new keys with a 404 ("no longer available to new users").
        # Its suggested replacement, 3.5 Flash-Lite, rejects
        # `reasoning_effort: none` with a 400 - Google documents its thinking as
        # "minimal" at the least, never off - so it scans through
        # `least_reasoning` below. 3.1 Flash-Lite accepts "none"; 2.5 Flash
        # accepts it and measurably stops thinking.
        models=("gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-2.5-flash"),
        default_fast="gemini-3.1-flash-lite",
        default_deep="gemini-2.5-flash",
        console="aistudio.google.com",
        # The Gemini API is served from Google's US infrastructure; only Vertex
        # AI offers EU data residency, and this is not Vertex.
        leaves_eu=True,
        # Checked 12 Oct 2026 against ai.google.dev/gemini-api/docs/pricing, paid
        # tier, text: input, cache hit, output. The cache hit is a TENTH of input
        # - the first draft of this entry, written from memory, had a quarter,
        # which overstated the bill on exactly the tokens this app repeats most.
        #
        # 3.5 Flash-Lite checked 26 Oct 2026 against the same page: dearer than
        # 3.1 on every axis, output by two thirds, and its thinking is billed as
        # output. Offered, not the default.
        price={
            "gemini-3.1-flash-lite": (0.25, 0.025, 1.50),
            "gemini-3.5-flash-lite": (0.30, 0.03, 2.50),
            "gemini-2.5-flash": (0.30, 0.03, 2.50),
        },
        # 2.5 Flash thinks by default, and the advanced check leaves thinking on.
        # Its output ceiling counts thought tokens, so without room here the
        # thinking eats the whole budget and the answer comes back empty - the
        # exact failure DeepSeek produced. A ceiling, not a charge.
        reasoning_reserve=4000,
        timeout=60.0,
        write_timeout=180.0,
        # 3.1 Flash-Lite and 2.5 Flash accept "none"; this is what keeps the
        # title screen and quick score from paying for thoughts nobody reads.
        no_reasoning={"reasoning_effort": "none"},
        least_reasoning={"gemini-3.5-flash-lite": {"reasoning_effort": "minimal"}},
    ),
}


def get(name: str | None) -> Provider:
    """The named provider, falling back to OpenAI for anything unknown.

    Never raises: a settings row carrying a provider this build does not know
    must degrade to something that works, not take the app down at startup."""
    return PROVIDERS.get((name or "").strip().lower(), PROVIDERS[OPENAI])


def provider_for(settings_row: dict, role: str = SCAN) -> Provider:
    """The provider serving one role.

    Each role is chosen independently, so scanning can move to the cheap
    endpoint without dragging cover letters along with it."""
    column = "writer_provider" if role == WRITE else "ai_provider"
    return get(settings_row.get(column))


def credentials(settings_row: dict, role: str = SCAN) -> tuple[Provider, str]:
    """(provider, api key) for one role.

    Each provider keeps its own key, so switching back and forth does not make
    the user paste the previous one again - and two roles pointed at the same
    provider share the one key rather than asking for it twice."""
    provider = provider_for(settings_row, role)
    return provider, (settings_row.get(key_column(provider)) or "").strip()


def key_column(provider: Provider) -> str:
    """Where this provider's key is stored."""
    return "openai_api_key" if provider.key == OPENAI else f"{provider.key}_api_key"


def resolve_model(settings_row: dict, stage: str) -> str:
    """The model for a stage ('fast', 'deep' or 'write'), valid for its provider.

    A model saved under one provider is meaningless under another, so a name the
    provider does not offer falls back to that provider's default rather than
    being sent and rejected. That fallback is what keeps the split honest: point
    writing at OpenAI while scanning runs on DeepSeek and the writer model stays
    an OpenAI name, because it is resolved against the writer's provider."""
    role = _ROLE_OF_STAGE.get(stage, SCAN)
    provider = provider_for(settings_row, role)
    column = {"fast": "openai_model_fast",
              "deep": "openai_model_advanced",
              "write": "writer_model"}[stage]
    chosen = (settings_row.get(column) or "").strip()
    if chosen in provider.models:
        return chosen
    return provider.default_fast if stage == "fast" else provider.default_deep


def no_reasoning(settings_row: dict, role: str = SCAN) -> dict:
    """Request fields that keep this role's model from thinking on a scan.

    Per model, not per provider: Gemini 3.1 Flash-Lite switches thinking off
    with `reasoning_effort: none`, while 3.5 Flash-Lite answers that with a 400
    and has to be asked for its least instead. Resolved against the model the
    client is built with - the fast model when scanning, the deep one for the
    advanced check, which never sends it."""
    provider = provider_for(settings_row, role)
    model = resolve_model(settings_row, "fast" if role == SCAN else "deep")
    return dict(provider.least_reasoning.get(model, provider.no_reasoning))


def timeout_for(settings_row: dict, role: str = SCAN) -> float:
    """Seconds this role is allowed to take.

    Two numbers per provider, because the two roles ask different questions:
    scoring wants an answer fast or not at all, writing is prose a person is
    sitting and waiting for and is worth waiting longer for."""
    provider = provider_for(settings_row, role)
    return provider.write_timeout if role == WRITE else provider.timeout


def reserve(settings_row: dict, role: str = SCAN) -> int:
    """Extra output tokens this role's provider needs before it answers."""
    return provider_for(settings_row, role).reasoning_reserve

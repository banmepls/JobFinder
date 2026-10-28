"""What repeats between calls, and therefore what gets billed at a tenth.

The bill here is decided by one property: everything identical from one job to
the next must come BEFORE the first thing that differs. Providers cache a
prompt PREFIX - a single per-job word placed too early makes the whole prefix
unique and the cache useless, with nothing failing and nothing in the log.

That is worth locking down. Measured on the real prompts, the advanced check
carries 6 360 identical tokens in front of the vacancy, and losing them nearly
doubles the price of every deep check.
"""

from __future__ import annotations

import pytest

from matcher import matcher as matcher_mod

# The minimum a provider will cache at all. OpenAI: 1 024 tokens, in 128-token
# increments; DeepSeek: 64-token blocks. Below this the prefix is billed in full
# however identical it is.
MIN_CACHEABLE_TOKENS = 1024
CHARS_PER_TOKEN = 3.5           # deliberately pessimistic, so the test under-claims

PROFILE = {
    "resume_text": "Product operations manager. " * 60,
    "case_bank": "- Release cycle turnaround: cut cycle time 40%. " * 40,
    "instructions": "Remote in the EU. Product ops, delivery lead. No junior roles. " * 12,
}


def _job(n: int) -> dict:
    return {"id": n, "title": f"Product Manager {n}", "company": f"Acme {n}",
            "location": "Amsterdam", "description": f"Job number {n}. " * 80}


class _Capture:
    """Stands in for the SDK and records exactly what would be sent."""

    def __init__(self, *a, **k):
        self.sent: list[dict] = []
        self.chat = type("Chat", (), {"completions": self})()

    async def create(self, **kwargs):
        self.sent.append(kwargs)

        class _Msg:
            content = '{"score": 50, "plausible": true, "results": [], "strengths": [], "weaknesses": []}'

        return type("R", (), {"choices": [type("C", (), {"message": _Msg()})()]})()


@pytest.fixture
def capture(monkeypatch):
    box = {}

    def _factory(*a, **k):
        box["client"] = box.get("client") or _Capture()
        return box["client"]

    monkeypatch.setattr(matcher_mod, "AsyncOpenAI", _factory)
    return box


def _wire(sent: dict) -> str:
    """The prompt as the provider sees it: messages in order, concatenated."""
    return "".join(m["content"] for m in sent["messages"])


def _common_prefix(a: str, b: str) -> int:
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return i


async def _two_calls(capture, method: str, **kw) -> tuple[str, str]:
    m = matcher_mod.AIMatcher(model="x", api_key="sk-test")
    for n in (1, 2):
        await getattr(m, method)(_job(n), **kw)
    client = capture["client"]
    return _wire(client.sent[0]), _wire(client.sent[1])


async def test_the_deep_check_repeats_thousands_of_tokens_before_it_differs(capture):
    """resume + case bank + instructions sit in front of the vacancy, which is
    what makes them cacheable. Moving the vacancy above any of them would cost
    roughly double per check, silently."""
    first, second = await _two_calls(capture, "advanced", profile=PROFILE)

    shared = _common_prefix(first, second)
    assert shared / CHARS_PER_TOKEN > MIN_CACHEABLE_TOKENS, (
        f"only ~{shared / CHARS_PER_TOKEN:.0f} tokens repeat before the prompts diverge"
    )
    # And what repeats is the expensive part, not just the system prompt.
    head = first[:shared]
    assert "CANDIDATE RESUME" in head
    assert "CASE BANK" in head
    assert "INSTRUCTIONS" in head
    # …while the job itself is past the divergence point, where it belongs.
    assert "Job number 1." not in head


async def test_the_quick_score_is_cacheable(capture):
    first, second = await _two_calls(capture, "quick_score", instructions=PROFILE["instructions"])

    shared = _common_prefix(first, second)
    assert shared / CHARS_PER_TOKEN > MIN_CACHEABLE_TOKENS
    assert "Job number 1." not in first[:shared]


async def test_the_title_screen_puts_the_titles_last(capture):
    """The one that was measured broken: with the instructions after the job,
    the stable prefix was 364 tokens, under every provider's floor, and the
    screen paid full price for the same 1 230 tokens on every single job.

    Size is not asserted here because it depends on how much the user wrote in
    their instructions - what the code controls is the ORDER, and the order is
    what was wrong."""
    m = matcher_mod.AIMatcher(model="x", api_key="sk-test")
    for batch in ([_job(1), _job(2)], [_job(3), _job(4)]):
        await m.title_screen_batch(batch, PROFILE["instructions"])
    client = capture["client"]

    first = _wire(client.sent[0])
    shared = _common_prefix(first, _wire(client.sent[1]))
    assert "CANDIDATE INSTRUCTIONS" in first[:shared], "the instructions must repeat"
    assert "Product Manager 1" not in first[:shared], "a title must not sit in the prefix"


async def test_the_cv_rides_in_the_cached_prefix_of_both_scanning_calls(capture):
    """Every job is now judged against the CV - about 1 100 tokens on a real
    one. Ahead of the first per-job word it is billed at a tenth; after it, in
    full on every job of every scan. And with no CV there is no CV block, so
    the prompt does not claim one."""
    cv = "CTO | Northwind Traders | Aug 2026 – Present (3 mos)\n- Shipped a platform. " * 20
    m = matcher_mod.AIMatcher(model="x", api_key="sk-test", cv_text=cv)
    for n in (1, 2):
        await m.quick_score(_job(n), PROFILE["instructions"])
    for batch in ([_job(3), _job(4)], [_job(5), _job(6)]):
        await m.title_screen_batch(batch, PROFILE["instructions"])
    sent = capture["client"].sent

    for first, second, per_job in ((sent[0], sent[1], "Job number 1."),
                                   (sent[2], sent[3], "Product Manager 3")):
        wire = _wire(first)
        head = wire[:_common_prefix(wire, _wire(second))]
        assert "=== CANDIDATE CV" in head and "Northwind Traders" in head
        assert per_job not in head

    bare = matcher_mod.AIMatcher(model="x", api_key="sk-test")
    await bare.quick_score(_job(7), PROFILE["instructions"])
    assert "=== CANDIDATE CV" not in _wire(sent[-1])


async def test_the_candidates_own_context_is_never_split_by_the_job(capture):
    """Interleaving would be the subtle version of the same mistake: each block
    is cheap on its own but everything after the first per-job word is billed in
    full. They must be contiguous and first."""
    first, _ = await _two_calls(capture, "advanced", profile=PROFILE)

    order = [first.index(marker) for marker in
             ("=== CANDIDATE RESUME ===", "=== CASE BANK", "=== INSTRUCTIONS ===",
              "=== VACANCY")]
    assert order == sorted(order), "the vacancy must come after everything reusable"

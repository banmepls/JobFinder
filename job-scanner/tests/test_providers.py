"""Which service the models are called on.

Everything speaks the OpenAI wire format, so a provider is a base URL and a key.
What differs is price, and it differs most on the part of the prompt that
repeats - which is 90% of what this app sends.
"""

from __future__ import annotations

import pytest

import providers


def test_an_unknown_provider_falls_back_instead_of_crashing():
    """A settings row naming a provider this build does not have must degrade
    to something that works, not take the app down at startup."""
    assert providers.get("mystery").key == providers.OPENAI
    assert providers.get("").key == providers.OPENAI
    assert providers.get(None).key == providers.OPENAI


def test_every_provider_declares_what_it_needs():
    for p in providers.PROVIDERS.values():
        assert p.models, f"{p.key} offers no models"
        assert p.default_fast in p.models
        assert p.default_deep in p.models
        assert p.console
        assert set(p.price) >= set(p.models), f"{p.key} has a model with no price"
        # A service that has not answered the residency question cannot be
        # warned about, and the page would send the CV abroad in silence.
        assert isinstance(p.leaves_eu, bool)


def test_openai_keeps_the_sdk_default_endpoint():
    """Passing a base_url of our own would break the moment the SDK changes it."""
    assert providers.get("openai").base_url is None
    assert providers.get("deepseek").base_url


@pytest.mark.parametrize("provider, column", [
    ("openai", "openai_api_key"),
    ("deepseek", "deepseek_api_key"),
    ("gemini", "gemini_api_key"),
])
def test_each_provider_reads_its_own_key(provider, column):
    """Sharing one field would mean re-pasting a key on every switch."""
    row = {"ai_provider": provider, column: "  the-key  ", "other": "x"}

    found, key = providers.credentials(row)

    assert found.key == provider
    assert key == "the-key"


def test_gemini_is_reached_through_its_openai_compatible_endpoint():
    """The whole app speaks one wire format. Gemini joins through Google's
    compatibility layer rather than a second client."""
    gemini = providers.get("gemini")

    assert gemini.base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    # The Gemini API is not EU-resident, so Settings must say so out loud.
    assert gemini.leaves_eu is True


def test_no_offered_model_would_fail_every_scan():
    """The scanning stages send the provider's `no_reasoning` fields on EVERY
    job. Gemini 2.5 Pro cannot turn thinking off and answers them with a 400 -
    so offering it would let one dropdown choice fail a whole day's scan. The
    model lists are shared by both roles, so the only safe place for Pro is not
    in the list at all."""
    gemini = providers.get("gemini")

    assert gemini.no_reasoning, "thinking must be switched off for scanning"
    assert not any("pro" in m for m in gemini.models), (
        "a model that cannot stop thinking would 400 every scan")


def test_a_model_that_cannot_stop_thinking_is_asked_for_its_least():
    """Gemini 3.5 Flash-Lite answers `reasoning_effort: none` with a 400. Sent
    the provider-wide switch, every job of a scan would fail and sit at a zero
    nobody decided on. The setting is chosen per model, so the models that can
    stop thinking still do - and every place a scanning client is built has to
    ask per model, or the fix holds in one path and not the next."""
    import pathlib

    import main
    from scheduler import jobs

    row = {"ai_provider": "gemini", "openai_model_fast": "gemini-3.5-flash-lite",
           "gemini_api_key": "AIza-test"}
    least = {"reasoning_effort": "minimal"}

    assert providers.no_reasoning(row) == least
    assert jobs._ai(row)["no_reasoning"] == least      # the scan itself
    assert main._ai(row)["no_reasoning"] == least      # /api/diag

    row["openai_model_fast"] = "gemini-3.1-flash-lite"
    assert jobs._ai(row)["no_reasoning"] == {"reasoning_effort": "none"}

    script = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "rescore_lost.py"
    assert "provider.no_reasoning" not in script.read_text(), (
        "the repair script would 400 every job it re-scores")


def test_a_model_from_another_provider_is_not_sent_to_gemini():
    row = {"ai_provider": "gemini", "openai_model_fast": "gpt-4.1-mini"}

    assert providers.resolve_model(row, "fast") == "gemini-3.1-flash-lite"


def test_a_gemini_model_closed_to_new_users_is_not_offered_or_sent():
    """Google answers gemini-2.5-flash-lite with a 404 for new keys. As the
    default scan model it would score nothing for every new Gemini user, and
    anyone who saved it before must be moved off it."""
    gemini = providers.get("gemini")
    row = {"ai_provider": "gemini", "openai_model_fast": "gemini-2.5-flash-lite"}

    assert "gemini-2.5-flash-lite" not in gemini.models
    assert providers.resolve_model(row, "fast") == "gemini-3.1-flash-lite"


def test_a_model_from_another_provider_is_not_sent():
    """gpt-4.1-mini means nothing to DeepSeek; sending it earns a 400."""
    row = {"ai_provider": "deepseek", "openai_model_fast": "gpt-4.1-mini"}

    assert providers.resolve_model(row, "fast") == "deepseek-v4-flash"


def test_a_valid_model_is_kept():
    row = {"ai_provider": "deepseek", "openai_model_fast": "deepseek-v4-pro"}

    assert providers.resolve_model(row, "fast") == "deepseek-v4-pro"


def test_the_cheaper_provider_is_actually_cheaper_where_it_matters():
    """The cached-input price is the one that decides this app's bill.

    This used to assert a tenfold gap, written against a DeepSeek price that was
    a third of the real one. The published gap is 14x off-peak - and 7x inside
    the peak windows, which the old assertion would have failed. What has to
    hold is that the scan provider is cheaper in BOTH regimes; the size of the
    gap is a fact about the market, not something to hard-code a floor under."""
    openai = providers.get("openai").price["gpt-4.1-mini"]
    deepseek = providers.get("deepseek")
    cached = deepseek.price["deepseek-v4-flash"][1]

    assert cached < openai[1], "cached input must be cheaper, or the split is pointless"
    assert cached * deepseek.peak_multiplier < openai[1], (
        "at peak the scan provider stops being the cheap one"
    )


def test_a_price_is_input_cached_output_in_that_order():
    """A transposed tuple is invisible and makes every later sum wrong - which
    is how this table came to claim a third of DeepSeek's real price."""
    for p in providers.PROVIDERS.values():
        for model, (inp, cached, out) in p.price.items():
            assert 0 < cached < inp, f"{model}: cached input is not the cheapest"
            assert out > inp, f"{model}: output is not dearer than input"


def test_time_of_day_billing_is_recorded_where_it_exists():
    """DeepSeek doubled its rate inside two windows on 16 Aug 2026. The app
    scans on a schedule, so a scan time is now a billing decision, and the
    windows have to be somewhere the app can read rather than in a comment."""
    deepseek = providers.get("deepseek")

    assert deepseek.peak_multiplier == 2.0
    assert deepseek.peak_hours_utc == ((1, 4), (6, 10))
    # OpenAI charges the same at every hour, and must not be given a window.
    assert providers.get("openai").peak_multiplier == 1.0
    assert providers.get("openai").peak_hours_utc == ()


def test_every_peak_window_is_a_real_range_of_hours():
    for p in providers.PROVIDERS.values():
        assert p.peak_multiplier >= 1.0, f"{p.key}: a peak cannot be a discount"
        for start, end in p.peak_hours_utc:
            assert 0 <= start < end <= 24, f"{p.key}: {start}-{end} is not a window"


# --------------------------------------------------------------------------- #
# Storage                                                                      #
# --------------------------------------------------------------------------- #

async def test_switching_provider_keeps_the_other_key(db):
    await db.save_openai_api_key("sk-openai-original")
    await db.save_ai_provider("deepseek", "sk-deepseek-new")

    row = await db.get_app_settings()
    assert row["ai_provider"] == "deepseek"
    assert row["deepseek_api_key"] == "sk-deepseek-new"
    assert row["openai_api_key"] == "sk-openai-original"


async def test_a_blank_key_does_not_wipe_the_saved_one(db):
    """The form renders the field empty for safety; saving the page must not
    take that as 'delete my key'."""
    await db.save_ai_provider("deepseek", "sk-deepseek")

    await db.save_ai_provider("deepseek", "   ")

    assert (await db.get_app_settings())["deepseek_api_key"] == "sk-deepseek"


async def test_an_unknown_provider_is_normalised_before_storage(db):
    await db.save_ai_provider("MYSTERY", "sk-x")

    assert (await db.get_app_settings())["ai_provider"] == "openai"


def test_the_client_is_pointed_at_the_chosen_endpoint():
    import inspect

    from matcher.matcher import AIMatcher

    assert "base_url" in inspect.signature(AIMatcher.__init__).parameters
    assert "base_url=base_url or None" in inspect.getsource(AIMatcher.__init__)


# --------------------------------------------------------------------------- #
# Reasoning models need room to think and time to finish                        #
# --------------------------------------------------------------------------- #

def test_a_reasoning_provider_declares_the_room_it_needs():
    """Measured against the live API: a title screen spends 28-176 tokens
    thinking, and the full score once spent 1060 - the entire budget - coming
    back as an empty string with finish_reason="length". That is
    indistinguishable from a broken provider unless it is named."""
    assert providers.get("deepseek").reasoning_reserve > 2000
    # 2.5 Flash thinks by default and its ceiling counts the thoughts.
    assert providers.get("gemini").reasoning_reserve > 2000
    assert providers.get("openai").reasoning_reserve == 0


def test_a_reasoning_provider_gets_longer_to_answer():
    """The deep check took 34 seconds against an 18-second timeout tuned for a
    model that answers directly."""
    assert providers.get("deepseek").timeout >= 60
    assert providers.get("openai").timeout <= 30


def test_the_reserve_is_added_to_every_budget_not_replacing_it():
    import inspect

    from matcher.matcher import AIMatcher

    source = inspect.getsource(AIMatcher)
    # The scanning stages add the reserve inline where they call create().
    for budget in ("_TITLE_MAX_TOKENS", "_QUICK_MAX_TOKENS"):
        assert f"{budget} + self.reserve" in source or \
               f"{budget} * len(jobs) + self.reserve" in source, budget
    # The three advanced calls share one helper that adds the reserve centrally,
    # so the budget arrives as `max_tokens` and the reserve is added once. The
    # guard is the same: it must be ADDED, never replace the budget. Each budget
    # is only ever handed to that helper, never straight into a create() call.
    assert "max_tokens=max_tokens + self.reserve" in source
    for budget in ("_ADVANCED_MAX_TOKENS", "_VERDICT_MAX_TOKENS", "_COVERAGE_MAX_TOKENS"):
        assert f"max_tokens={budget}" not in source, budget   # never bypass the helper
        assert f"{budget})" in source, budget                 # passed to _advanced_call


def test_an_empty_answer_from_an_exhausted_budget_is_named():
    """Returning a silent zero for it read as "DeepSeek does not work"."""
    from matcher.matcher import OutOfTokens, _require_content

    class Choice:
        finish_reason = "length"
        message = type("M", (), {"content": ""})()

    resp = type("R", (), {"choices": [Choice()]})()

    with pytest.raises(OutOfTokens):
        _require_content(resp)


def test_a_normal_empty_answer_is_not_mistaken_for_that():
    from matcher.matcher import _require_content

    class Choice:
        finish_reason = "stop"
        message = type("M", (), {"content": ""})()

    assert _require_content(type("R", (), {"choices": [Choice()]})()) == ""


def test_every_client_takes_everything_the_helper_passes_it():
    """`_ai()` builds the keyword arguments for every client, so a client that
    does not accept one of them raises TypeError at the moment of use and
    nowhere earlier.

    This test used to list the argument names by hand and missed
    `no_reasoning`, which CopilotGenerator did not take - shipping a 500 on the
    cover letter in 1.2.0. It now reads the names out of `_ai()` itself, so a
    sixth argument cannot be added without every client keeping up."""
    import inspect

    import main
    from matcher.generate import CopilotGenerator
    from matcher.matcher import AIMatcher

    row = {"ai_provider": "deepseek", "writer_provider": "openai",
           "deepseek_api_key": "ds", "openai_api_key": "sk"}
    passed = set(main._ai(row)) | set(main._ai(row, providers.WRITE))
    assert "no_reasoning" in passed, "_ai stopped passing the argument that broke"

    for cls in (AIMatcher, CopilotGenerator):
        params = set(inspect.signature(cls.__init__).parameters)
        missing = passed - params
        assert not missing, f"{cls.__name__} does not accept {sorted(missing)}"


def test_the_writing_client_can_actually_be_built(db):
    """The signature test above is structural; this one calls the constructor
    the route calls, because that is where the 500 happened."""
    import main
    from matcher.generate import CopilotGenerator

    row = {"writer_provider": "deepseek", "deepseek_api_key": "ds-key"}
    generator = CopilotGenerator(model=main._write_model(row),
                                 **main._ai(row, providers.WRITE))

    assert generator.init_error is None
    assert generator.model in providers.get("deepseek").models


def test_the_client_is_built_with_the_providers_timeout():
    """Storing it and then not passing it on is the failure this catches: the
    deep check kept timing out at 18 seconds while the setting said 120."""
    import inspect

    from matcher.matcher import AIMatcher

    source = inspect.getsource(AIMatcher.__init__)
    assert "timeout=self.timeout" in source


# --------------------------------------------------------------------------- #
# Two roles                                                                    #
#                                                                              #
# Scanning is judgement at volume and belongs wherever a cached input token is  #
# cheapest. Writing is read by a person and sent to an employer. Measured on 32 #
# jobs, DeepSeek without reasoning ran the scan at $0.17 per 1300 jobs against  #
# OpenAI's $0.92 and agreed on the 60+ cut - a result about judgement that says #
# nothing about prose, which is why the two choices are separate.               #
# --------------------------------------------------------------------------- #

def test_moving_scanning_to_deepseek_leaves_writing_alone():
    """The whole point of the split. An install that switches the scan must not
    silently start writing cover letters somewhere else."""
    row = {"ai_provider": "deepseek"}

    assert providers.provider_for(row, providers.SCAN).key == "deepseek"
    assert providers.provider_for(row, providers.WRITE).key == "openai"


def test_the_writer_model_is_resolved_against_the_writer():
    """A model is only meaningful to the provider it was chosen for. Resolving
    the writer's model against the SCANNING provider would post an OpenAI name
    to DeepSeek - or the reverse - for a 400 on the one call the user watches."""
    row = {"ai_provider": "deepseek", "writer_provider": "openai",
           "writer_model": "gpt-4.1"}

    assert providers.resolve_model(row, "fast") == "deepseek-v4-flash"
    assert providers.resolve_model(row, "write") == "gpt-4.1"


def test_the_advanced_check_runs_where_the_writing_does():
    """It used to sit with the scan because it is judgement - the wrong test.
    A cheap endpoint earns its latency through VOLUME, and this stage has none:
    a few calls a day with a person waiting. Measured on the live app, 55
    seconds on DeepSeek's reasoning model, ending in an empty answer because
    the reasoning had eaten the whole output budget."""
    row = {"ai_provider": "deepseek", "writer_provider": "openai"}

    assert providers.provider_for(row, providers.SCAN).key == "deepseek"
    assert providers.resolve_model(row, "deep") in providers.get("openai").models


def test_a_writer_model_the_writer_does_not_offer_falls_back():
    row = {"writer_provider": "openai", "writer_model": "deepseek-v4-pro"}

    assert providers.resolve_model(row, "write") == "gpt-4.1"


def test_both_roles_on_one_provider_share_the_key():
    """Asking for the same key twice would be a bug the user pays for in
    confusion the first time one field is filled and the other is not."""
    row = {"ai_provider": "openai", "writer_provider": "openai",
           "openai_api_key": "sk-one"}

    assert providers.credentials(row, providers.SCAN)[1] == "sk-one"
    assert providers.credentials(row, providers.WRITE)[1] == "sk-one"


def test_split_roles_read_their_own_keys():
    row = {"ai_provider": "deepseek", "writer_provider": "openai",
           "deepseek_api_key": "ds-key", "openai_api_key": "sk-key"}

    scan, scan_key = providers.credentials(row, providers.SCAN)
    write, write_key = providers.credentials(row, providers.WRITE)

    assert (scan.key, scan_key) == ("deepseek", "ds-key")
    assert (write.key, write_key) == ("openai", "sk-key")


def test_the_thinking_reserve_follows_the_role():
    """A reasoning model handed the answer budget alone returns an empty string.
    Reading the reserve off the wrong role is how that comes back."""
    row = {"ai_provider": "deepseek", "writer_provider": "openai"}

    assert providers.reserve(row, providers.SCAN) > 2000
    assert providers.reserve(row, providers.WRITE) == 0
    # And the advanced check reads the writer's, since that is where it runs.
    assert providers._ROLE_OF_STAGE["deep"] == providers.WRITE


def test_every_stage_has_a_role():
    """A stage nobody mapped would quietly resolve against the scanning
    provider, which is exactly the leak this split exists to prevent."""
    for stage in ("fast", "deep", "write"):
        assert stage in providers._ROLE_OF_STAGE


def test_nothing_a_person_reads_is_generated_by_the_scanning_model():
    """The split is only real if the call sites honour it.

    Before this, cover letters, CV rewrites and HR answers all took
    `_deep_model` - so choosing DeepSeek for the scan moved every piece of
    writing there too, and worse, the three streaming helpers built their own
    client against api.openai.com and would have been handed a DeepSeek model
    name. Structural rather than behavioural, because the failure is a call site
    someone adds later, not a branch that runs today."""
    import ast
    import pathlib

    WRITERS = {"CopilotGenerator", "structure_cv_html",
               "stream_cv_edit", "stream_letter_edit"}
    tree = ast.parse((pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text())

    seen = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else \
            getattr(node.func, "id", "")
        if name not in WRITERS:
            continue
        seen.add(name)
        source = ast.dump(node)
        assert "_write_model" in source, f"{name} is not using the writing model"
        assert "_deep_model" not in source, f"{name} still uses the scanning model"
        assert "openai_api_key" not in source, \
            f"{name} reads the OpenAI key column directly, ignoring the provider"

    assert seen == WRITERS, f"call sites not found: {sorted(WRITERS - seen)}"


def test_the_streaming_helpers_accept_an_endpoint():
    """They build their own client. Without a base_url they always reach OpenAI,
    whatever provider the writer model was resolved for."""
    import inspect

    from matcher import generate

    for name in ("structure_cv_html", "stream_cv_edit", "stream_letter_edit"):
        params = inspect.signature(getattr(generate, name)).parameters
        assert "base_url" in params, f"{name} cannot be pointed at another endpoint"


# --------------------------------------------------------------------------- #
# The Settings page                                                            #
# --------------------------------------------------------------------------- #

async def _settings_html(scan: str, write: str) -> str:
    from fastapi.testclient import TestClient

    import main
    from db import queries

    await queries.save_ai_provider(scan, api_key="sk-scan-key")
    await queries.save_writer(write, "", api_key="sk-write-key")
    with TestClient(main.app) as client:
        r = client.get("/settings")
    assert r.status_code == 200
    return r.text


def _options(html: str, field: str) -> list[str]:
    import re

    block = re.search(r'name="%s".*?</select>' % field, html, re.S)
    assert block, f"no {field} selector on the page"
    return re.findall(r'value="([^"]+)"', block.group(0))


@pytest.mark.parametrize("scan,write", [("openai", "deepseek"),
                                        ("deepseek", "openai")])
async def test_each_selector_offers_only_its_own_services_models(db, scan, write):
    """A model name belongs to a service. Offering the other one's names lets
    the user save something the endpoint will reject on the next scan."""
    html = await _settings_html(scan, write)
    scanning = set(providers.get(scan).models)
    writing = set(providers.get(write).models)

    assert set(_options(html, "openai_model_fast")) == scanning
    assert set(_options(html, "writer_model")) == writing
    # The advanced check moved to the writing service, so its selector has to
    # move with it - offering the scanner's names here would save a model the
    # writing endpoint rejects.
    assert set(_options(html, "openai_model_advanced")) == writing


async def test_the_service_is_chosen_before_its_models(db):
    """The page read backwards for a while: the model pickers sat in a card
    ABOVE the one that chooses the service, so DeepSeek names appeared before
    there was any way to see, or change, whose they were."""
    html = await _settings_html("deepseek", "openai")
    assert html.index('name="ai_provider"') < html.index('name="openai_model_fast"')
    assert html.index('name="writer_provider"') < html.index('name="writer_model"')
    assert html.index('name="writer_provider"') < html.index('name="openai_model_advanced"')


def _shown(html: str, element_id: str) -> bool:
    """Whether an element is rendered visible.

    Everything is in the DOM now so the page can switch services without a
    reload, which means presence proves nothing - `hidden` is what decides
    whether the user is actually told."""
    import re

    tag = re.search(r"<[^>]*\bid=\"%s\"[^>]*>" % element_id, html)
    assert tag, f"no element with id={element_id}"
    return "hidden" not in tag.group(0)


@pytest.mark.parametrize("scan,write,scan_warned,write_warned", [
    ("openai", "openai", False, False),
    ("deepseek", "openai", True, False),
    ("openai", "deepseek", False, True),
    ("deepseek", "deepseek", True, True),
])
async def test_data_leaving_the_eu_is_stated_for_each_role(
        db, scan, write, scan_warned, write_warned):
    """Scanning sends job descriptions and the instructions; writing sends the
    CV. Warning about one role and not the other is how the CV leaves quietly."""
    html = await _settings_html(scan, write)
    assert _shown(html, "scan-eu") is scan_warned
    assert _shown(html, "write-eu") is write_warned


async def test_one_key_field_when_both_roles_share_a_service(db):
    """Two boxes for the same key is an invitation to fill one and wonder why
    the other half of the app is unauthenticated."""
    shared = await _settings_html("deepseek", "deepseek")
    assert _shown(shared, "writer-key-field") is False
    assert _shown(shared, "writer-shared") is True

    split = await _settings_html("deepseek", "openai")
    assert _shown(split, "writer-key-field") is True
    assert _shown(split, "writer-shared") is False


async def test_changing_a_service_does_not_reload_the_page(db):
    """It used to navigate to /settings?provider=… and /settings?writer=…, one
    parameter at a time - so choosing a writer threw away an unsaved scanner
    choice, and half the combinations looked forbidden. It also lost the
    reader's place on the page. Every service's models are shipped with the page
    instead, and the selectors are repainted in the browser."""
    html = await _settings_html("deepseek", "openai")
    assert "location.href = '/settings?" not in html

    import json

    data = json.loads(html.split('id="service-data" type="application/json">')[1]
                          .split("</script>")[0])
    for key, p in providers.PROVIDERS.items():
        assert data[key]["models"] == list(p.models)
        assert data[key]["leaves_eu"] == p.leaves_eu


# --------------------------------------------------------------------------- #
# Settings that have to mean something                                          #
# --------------------------------------------------------------------------- #

async def test_the_default_job_cap_reaches_a_new_search(db):
    """It was stored and then ignored: the new-search form hardcoded 50, so
    changing this in Settings did nothing whatsoever."""
    from fastapi.testclient import TestClient

    import main
    from db import queries

    await queries.save_app_settings(
        scan_interval_minutes=0, default_max_jobs=137, openai_model="gpt-4.1-mini",
        copilot_min_score=60,
    )
    with TestClient(main.app) as client:
        html = client.get("/search/new").text

    assert 'name="max_jobs"' in html
    assert 'value="137"' in html


async def test_a_saved_timezone_survives_the_settings_form(db):
    """And a name the machine cannot resolve is not stored at all - a typo must
    not quietly move every scheduled scan to another hour."""
    from fastapi.testclient import TestClient

    import main
    from db import queries

    form = {"ai_provider": "openai", "writer_provider": "openai",
            "openai_model_fast": "gpt-4.1-mini", "openai_model_advanced": "gpt-4.1",
            "scan_times": "09:00", "default_max_jobs": "50",
            "daily_apply_goal": "5", "copilot_min_score": "60"}

    with TestClient(main.app) as client:
        client.post("/settings", data={**form, "scan_timezone": "America/New_York"})
        assert (await queries.get_app_settings())["scan_timezone"] == "America/New_York"

        client.post("/settings", data={**form, "scan_timezone": "Nowhere/Atlantis"})
        assert (await queries.get_app_settings())["scan_timezone"] == ""

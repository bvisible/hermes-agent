"""Neoffice — the route prompts ask for the date; something has to feed it.

The deployed prompt opens with « Date du jour : {today_fr} (heure : {time}). » and
``_render_prompt`` returns an unknown key AS ITSELF, so a missing injection is not an
error — it is a literal brace reaching the model. Nothing raises, nothing logs.

It has now been lost TWICE: added 04.06 (8fc60f0a05, itself a re-add) and gone again
in the v2026.9.7 port on 08.09. Proven by the request dumps on osiris — the last one
carrying real dates is 2026-09-07 (5 of them); 2026-09-08 — the day the port was
promoted — carries 8 raw templates, and the 191 turns since carry not one real date.

Be precise about what that costs, because it is narrower than "the model has no date":
Hermes' own system prompt does carry one ("Conversation started: Sunday, September 20,
2026 (UTC, UTC+00:00)"). What goes missing is the TIME, which nothing else supplies;
the right DAY, because that system-prompt date is the SERVER's clock — UTC here, so it
names yesterday between midnight and 02:00 Swiss — and it is the date the conversation
STARTED, which drifts in a long session; and a clean prompt, since the raw
« Date du jour : {today_fr} » line lands immediately above « n'invente jamais de date,
utilise celle indiquée ci-dessus ».

That is what this file exists to stop: not the injection being wrong, but the injection
being ABSENT and nobody noticing for a month.
"""
from datetime import datetime, timedelta, timezone

import pytest

from gateway.platforms.webhook import (
    _TEMPLATE_KEY_RE,
    _neoffice_inject_datetime_tokens,
)

# The shape of the deployed whatsapp_inbox prompt, first line verbatim.
GABARIT = ('Date du jour : {today_fr} (heure : {time}).\n\n'
           'Message WhatsApp entrant de l\'utilisateur {phone} : "{message}".')

DATE_TOKENS = ("today_fr", "date", "time", "year")


def _render(template, payload):
    return _TEMPLATE_KEY_RE.sub(
        lambda m: str(payload.get(m.group(1), "{%s}" % m.group(1))), template)


def test_the_deployed_prompt_leaves_no_date_token_unfilled():
    """The regression itself: render it and look for a surviving brace."""
    payload = _neoffice_inject_datetime_tokens(
        {"phone": "+41790000000", "message": "bonjour"})
    rendu = _render(GABARIT, payload)
    survivants = [k for k in _TEMPLATE_KEY_RE.findall(rendu) if k in DATE_TOKENS]
    assert not survivants, f"reached the model unfilled: {survivants} -> {rendu!r}"
    assert "{" not in rendu.split("\n")[0], rendu


def test_without_the_injection_the_token_reaches_the_model_verbatim():
    """The failing case, spelled out, so the guard is known to be able to go red."""
    rendu = _render(GABARIT, {"phone": "+41790000000", "message": "bonjour"})
    assert "{today_fr}" in rendu and "{time}" in rendu


@pytest.mark.parametrize("cle", DATE_TOKENS)
def test_every_token_the_prompts_use_is_fed(cle):
    assert _neoffice_inject_datetime_tokens({}).get(cle)


def test_an_explicit_payload_key_still_wins():
    """setdefault, deliberately: nora sends these from send_chat at the SITE's
    timezone, which is a better source than this fallback. This one exists for the
    routes whose payload does not come from nora — whatsapp_inbox comes from the
    central router."""
    donne = {"today_fr": "mardi 1 janvier 2030", "time": "07:07",
             "date": "2030-01-01", "year": "2030"}
    sorti = _neoffice_inject_datetime_tokens(dict(donne))
    assert {k: sorti[k] for k in donne} == donne


def test_the_swiss_clock_decides_the_day_not_the_server_clock():
    """osiris runs UTC. Two hours behind is not only a wrong hour: between midnight
    and 02:00 it is the WRONG DAY, which is what the customer reads."""
    from zoneinfo import ZoneInfo

    minuit_et_demi = datetime(2026, 9, 21, 0, 30, tzinfo=ZoneInfo("Europe/Zurich"))
    assert minuit_et_demi.astimezone(timezone.utc).day == 20  # UTC still says the 20th
    sorti = _neoffice_inject_datetime_tokens({}, now=minuit_et_demi)
    assert sorti["date"] == "2026-09-21"
    assert sorti["today_fr"] == "lundi 21 septembre 2026"
    assert sorti["time"] == "00:30"


def test_the_default_clock_is_zurich():
    """Called with no instant, it must not fall back to the server's UTC."""
    from zoneinfo import ZoneInfo

    attendu = datetime.now(ZoneInfo("Europe/Zurich"))
    sorti = _neoffice_inject_datetime_tokens({})
    # Same minute unless the test straddles one; the date is the load-bearing part.
    assert sorti["date"] == attendu.strftime("%Y-%m-%d")
    assert abs(int(sorti["time"][:2]) - attendu.hour) <= 1


def test_the_french_names_are_french():
    """The model is quoting this to a customer, in French."""
    from zoneinfo import ZoneInfo

    sorti = _neoffice_inject_datetime_tokens(
        {}, now=datetime(2026, 8, 5, 14, 0, tzinfo=ZoneInfo("Europe/Zurich")))
    assert sorti["today_fr"] == "mercredi 5 août 2026"


def test_a_payload_that_is_not_a_dict_is_returned_untouched():
    """This runs on every webhook event: it may never be the thing that raises."""
    assert _neoffice_inject_datetime_tokens(None) is None
    assert _neoffice_inject_datetime_tokens("nope") == "nope"


def test_the_injection_does_not_invent_other_keys():
    """Only the four the prompts name — a payload is also matched by dot-notation."""
    sorti = _neoffice_inject_datetime_tokens({})
    assert set(sorti) == set(DATE_TOKENS)


# //// Neoffice — the guard that would actually have caught it, added after noticing the
# //// ones above would NOT: removing the call site leaves every test here green,
# //// because they exercise the function and the function was never what went missing.
# //// Twice now, a port dropped one LINE — and a test of the helper cannot see that.
# //// So this one reads the source. Crude on purpose: a port that deletes the call, or
# //// moves it below the render, turns it red, which is the only thing being asked of it.
def test_the_injection_is_still_wired_into_the_route_handler():
    """The function being correct proves nothing if nobody calls it."""
    from pathlib import Path

    import gateway.platforms.webhook as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    appel = source.find("_neoffice_inject_datetime_tokens(payload)")
    rendu = source.find('self._render_prompt(route_config.get("prompt"')
    assert rendu != -1, "the route prompt render moved; this guard must follow it"
    assert appel != -1, (
        "the datetime injection call is gone from the route handler — this is exactly "
        "what the v2026.9.7 port did on 08.09, and what 8fc60f0a05 had already repaired "
        "once before that"
    )
    assert appel < rendu, (
        "the injection runs AFTER the prompt is rendered, so the tokens it fills reach "
        "nobody"
    )

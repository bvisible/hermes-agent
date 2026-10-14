# //// Neoffice — added file (no upstream equivalent): what the fast-answer engine reads of the conversation (14.10).
"""The film handed to the fast answer is the END of the conversation, never its beginning.

It was cut to its first 1500 characters, which keep the OLDEST turns: after five long lists, the question that followed
(« Combien ça fait au total ? ») found only the first three in front of it and was answered about the wrong list - the 55
supplier invoices of turn 5 for the 4 purchase orders of turn 6 (Quick Chat, 14.10).
"""
import inspect
import json
from unittest import mock

import pytest

from gateway import nora_chat_router as router

LIMIT = router._FAST_ANSWER_FILM_CHARS
CALLBACK = {
    "callback_url": "https://erp.example.test/api/method/nora.api.v2.hermes_callback.deliver",
    "callback_token": "token",
    "conversation_id": "c-film",
}


def _film(exchanges, answer_chars=400):
    """The film as the gateway keeps it: « User: » up to 240 characters, « NORA: » up to 400 after its label."""
    film = []
    for n in range(exchanges):
        film.append(f"User: question {n}")
        film.append("NORA: " + f"answer {n} ".ljust(answer_chars, "x"))
    return film


def test_a_film_that_fits_is_handed_whole():
    film = _film(2, answer_chars=60)
    assert router._film_tail(film) == "\n".join(film)


def test_the_most_recent_entries_are_kept_when_the_film_is_too_long():
    film = _film(5)  # ten entries, about 2100 characters: the real film after five long lists
    assert len("\n".join(film)) > LIMIT
    tail = router._film_tail(film)
    assert len(tail) <= LIMIT
    assert tail.endswith(film[-1]), "the last answer is what a follow-up refers to"
    assert film[-2] in tail, "and so is the question that got it"
    assert "question 0" not in tail, "the oldest turns are the ones that go"


@pytest.mark.parametrize("answer_chars", [10, 60, 150, 299, 400])
def test_the_cut_never_lands_inside_an_entry(answer_chars):
    film = _film(5, answer_chars=answer_chars)
    tail = router._film_tail(film)
    assert all(line in film for line in tail.split("\n")), tail
    assert len(tail) <= LIMIT
    # nothing more would have fitted: the entry before the oldest kept one is too long to join them
    kept = tail.split("\n")
    older = film[len(film) - len(kept) - 1] if len(film) > len(kept) else None
    if older is not None:
        assert len(tail) + 1 + len(older) > LIMIT


def test_the_line_breaks_between_entries_count_in_the_length():
    half = LIMIT // 2
    film = ["User: " + "a" * (half - 6), "NORA: " + "b" * (LIMIT - half - 6)]
    assert sum(len(entry) for entry in film) == LIMIT  # they fit by their characters alone, not with the break between them
    assert router._film_tail(film) == film[-1]


def test_the_last_entry_is_kept_even_when_it_alone_is_longer_than_the_limit():
    film = ["User: a", "NORA: " + "y" * (3 * LIMIT)]
    tail = router._film_tail(film)
    assert tail == film[-1][-LIMIT:]


@pytest.mark.parametrize("empty", [[], None])
def test_an_empty_film_is_empty(empty):
    assert router._film_tail(empty) == ""


def test_the_film_of_a_conversation_is_read_from_its_history(monkeypatch):
    film = _film(5)
    monkeypatch.setitem(router._CONV_HISTORY, "c-film", film)
    seen = router._film_for_the_fast_answer("c-film")
    assert seen.endswith(film[-1]) and len(seen) <= LIMIT and "question 0" not in seen
    assert router._film_for_the_fast_answer("c-unknown") == ""
    assert router._film_for_the_fast_answer("") == ""
    assert router._film_for_the_fast_answer(None) == ""


def test_a_film_already_joined_keeps_its_end_from_the_start_of_a_line():
    film = _film(5)
    joined = "\n".join(film)
    end = router._film_end(joined)
    assert len(end) <= LIMIT
    assert end.endswith(film[-1])
    assert end.startswith(("User: ", "NORA: ")), "it starts at an entry, not in the middle of one"
    assert router._film_end("  short  ") == "short"
    assert router._film_end("") == "" and router._film_end(None) == ""


def _post_and_read_the_context(context):
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["body"] = json.loads(req.data.decode())
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"message": {"answered": false}}'
        return response

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        router._fast_answer("Combien ça fait au total ?", "user@example.test", CALLBACK, context)
    return sent["body"]


def test_what_is_posted_to_nora_is_the_end_of_the_film():
    film = _film(5)
    body = _post_and_read_the_context("\n".join(film))
    assert len(body["context"]) <= LIMIT
    assert body["context"].endswith(film[-1])
    assert "question 0" not in body["context"]


def test_a_short_film_is_posted_whole_and_no_film_is_posted_at_all():
    film = _film(1, answer_chars=60)
    assert _post_and_read_the_context("\n".join(film))["context"] == "\n".join(film)
    assert "context" not in _post_and_read_the_context("")


def test_the_router_asks_nora_with_the_end_of_the_film():
    source = inspect.getsource(router.route_chat_message)
    assert "_film_for_the_fast_answer(conversation_id)" in source
    assert '"\\n".join(_CONV_HISTORY' not in source, "the head of the film is not what a follow-up refers to"

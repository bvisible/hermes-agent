# //// Neoffice — added file (no upstream equivalent): an empty classifier verdict is asked again with the poles
"""An empty classifier verdict is asked once more with the list of answers (09.10).

For a question about a client's contact the model answered an empty string, finish « stop », five times in six at
temperature 0: the same question asked again failed again, and the turn went to the orchestrator (25 s).
"""

from types import SimpleNamespace

from gateway import nora_chat_router as R


def _reply(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def test_an_empty_verdict_is_asked_again_with_the_list_of_answers(monkeypatch):
    monkeypatch.setattr(R, "_fast_path", lambda msg, prior: None)
    asked = []

    def call_llm(**kw):
        user = kw["messages"][-1]["content"]
        asked.append(user)
        return _reply("ventes" if "un seul mot" in user else "")

    pole = R.classify("Qui est la personne de contact chez ce client ?", call_llm_fn=call_llm, main_runtime=None)
    assert pole == "ventes"
    assert len(asked) == 2 and "un seul mot" not in asked[0] and "un seul mot" in asked[1]
    assert all(p in asked[1] for p in R.POLES) and "direct" in asked[1]


def test_a_verdict_given_at_once_is_asked_once(monkeypatch):
    monkeypatch.setattr(R, "_fast_path", lambda msg, prior: None)
    asked = []

    def call_llm(**kw):
        asked.append(kw["messages"][-1]["content"])
        return _reply("compta")

    assert R.classify("Où en sont nos encaissements ?", call_llm_fn=call_llm, main_runtime=None) == "compta"
    assert len(asked) == 1


def test_twice_empty_is_still_a_failure(monkeypatch):
    monkeypatch.setattr(R, "_fast_path", lambda msg, prior: None)
    assert R.classify("Qui est la personne de contact chez ce client ?", call_llm_fn=lambda **kw: _reply(""),
                      main_runtime=None) == "DIRECT"

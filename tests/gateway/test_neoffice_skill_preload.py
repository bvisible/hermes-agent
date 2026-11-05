# //// Neoffice — added file (no upstream equivalent): a worker starts with the skill of its task already read.
"""The skill a task needs is read before the worker starts, and nothing about it can cost a request."""
import pytest

from gateway import nora_skill_preload as P


def _skill(root, pole, name, front):
    folder = root / "profiles" / pole / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n{front}---\n# {name}\n", encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch):
    P._INDEX.clear()
    monkeypatch.setattr(P, "_profile_home", lambda pole: tmp_path / "profiles" / pole)
    monkeypatch.setattr(P, "_kill_switch", lambda: tmp_path / "skill-preload.off")
    _skill(tmp_path, "resto", "liste-de-courses", "preload_when:\n  - '\\bliste de courses\\b'\n  - '\\bj.ai pris\\b'\n")
    _skill(tmp_path, "resto", "menus-et-formules", "preload_rank: 10\npreload_when: ['\\bmenus?\\b']\n")
    _skill(tmp_path, "resto", "recettes", "preload_when: ['\\brecettes?\\b']\n")
    _skill(tmp_path, "resto", "sans-declencheur", "")
    _skill(tmp_path, "analyse", "tableau-bord-ca", "preload_when: ['.']\n")
    yield tmp_path
    P._INDEX.clear()


@pytest.mark.parametrize(
    "message, skill",
    [("Je fais ma liste de courses", "liste-de-courses"), ("J'ai pris la bière", "liste-de-courses"), ("j’ai pris le sel", "liste-de-courses"),
     ("Note la RECETTE du risotto", "recettes"), ("Compose un menu", "menus-et-formules")],
)
def test_a_skill_says_when_it_is_needed(home, message, skill):
    assert P.choose("resto", message) == [skill]


def test_accents_are_folded_before_the_match(home):
    _skill(home, "ventes", "party-address-management", "preload_when: ['\\bdemenag']\n")
    assert P.choose("ventes", "Le client a déménagé") == ["party-address-management"]


def test_the_lowest_rank_wins_when_two_skills_fit(home):
    assert P.choose("resto", "La liste de courses du menu") == ["menus-et-formules"]


def test_no_trigger_no_skill(home):
    assert P.choose("resto", "Bonjour") == []


def test_a_skill_can_always_be_needed(home):
    assert P.choose("analyse", "Un graphique de mes ventes") == ["tableau-bord-ca"]


def test_a_short_answer_finds_its_skill_in_the_thread(home):
    assert P.choose("resto", "Oui, ajoute-les", ["Bonjour", "Je fais ma liste de courses"]) == ["liste-de-courses"]


def test_the_latest_thing_said_comes_first(home):
    assert P.choose("resto", "oui", ["La recette du risotto", "Je fais ma liste de courses"]) == ["recettes"]


def test_a_long_message_of_its_own_does_not_inherit_the_thread(home):
    message = "Je voudrais savoir ce que vous pensez de la saison qui vient et de ce qu'il faudrait prévoir pour la suite du mois prochain"
    assert P.choose("resto", message, ["Je fais ma liste de courses"]) == []


def test_a_broken_trigger_leaves_out_its_skill_alone(home):
    _skill(home, "resto", "cassee", "preload_when: ['(']\n")
    P._INDEX.clear()
    assert P.choose("resto", "ma liste de courses") == ["liste-de-courses"]


def test_a_pole_with_no_folder_preloads_nothing(home):
    assert P.choose("nulle-part", "ma liste de courses") == []


def test_a_failure_never_reaches_the_router(home, monkeypatch):
    monkeypatch.setattr(P, "_index", lambda pole: 1 / 0)
    assert P.choose("resto", "ma liste de courses") == []


def test_the_switch_stops_it_at_once(home):
    (home / "skill-preload.off").write_text("")
    assert P.choose("resto", "ma liste de courses") == []
    (home / "skill-preload.off").unlink()
    assert P.choose("resto", "ma liste de courses") == ["liste-de-courses"]


def test_a_new_skill_is_seen_without_a_restart(home, monkeypatch):
    assert P.choose("resto", "la saison") == []
    _skill(home, "resto", "saison", "preload_when: ['\\bsaison\\b']\n")
    monkeypatch.setattr(P, "_TTL", 0)
    assert P.choose("resto", "la saison") == ["saison"]


def test_what_a_task_is_given_is_the_skill_chosen(monkeypatch):
    import inspect

    from gateway import nora_chat_router as R

    assert "skills=_preloaded or None" in inspect.getsource(R.route_chat_message)


def test_a_preloaded_skill_that_is_gone_costs_nothing(monkeypatch):
    from gateway import session_kanban

    calls = []
    agent = type("A", (), {"session_id": "s", "run_conversation": lambda self, prompt, **kw: calls.append(prompt) or {"final_response": "ok"}})()
    monkeypatch.setattr("agent.skill_commands.build_preloaded_skills_prompt", lambda names, task_id=None: ("", [], list(names)))
    monkeypatch.setattr("agent.kanban_turn_recovery.recover_failed_kanban_turns", lambda *a, **k: None)
    context = {"skills": ["parti"], "context": "la carte", "task_id": "t", "goal_mode": False}
    session_kanban._run_task_turns(agent, {"text": "travaille"}, [], context)
    assert calls == ["la carte\n\ntravaille"]

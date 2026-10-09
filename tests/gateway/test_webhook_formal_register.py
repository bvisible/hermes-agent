# //// Neoffice — added file (no upstream equivalent): the chat's language line asks for the formal register (09.10).
"""« Mets-la en pause » is answered with « vous », never « dis-moi si tu veux ».

Told « vous » only in her SOUL, far from the turn, the orchestrator answered a person who wrote in the
imperative with « tu » (Quick Chat on the dev instance, 09.10). The webhook's language line, the last
thing she reads before the message, now names the formal register for the three languages that have one.
"""
from pathlib import Path

SRC = Path(__file__).resolve().parents[2].joinpath("gateway", "platforms", "webhook.py").read_text(encoding="utf-8")


def test_the_language_line_names_the_formal_register():
    for lang, form in (("fr", "vous"), ("de", "Sie"), ("it", "Lei")):
        assert f'"{lang}": " Address them as « {form} », whatever form they use."' in SRC, lang
    assert "(System: reply to the user in {_lang_name}.{_register} " in SRC


def test_english_gets_no_register_and_the_line_keeps_its_shape():
    assert '.get(\n                _lang_code.split("-")[0].lower(), "")' in SRC
    assert 'f"Do not reply in any other language.)\\n\\n{prompt}"' in SRC

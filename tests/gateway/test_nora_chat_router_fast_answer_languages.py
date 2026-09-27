# //// Neoffice — added file (no upstream equivalent): the fast-answer engine is asked in the
# //// four languages NORA speaks (nora #867, decision of 2026-09-27).
"""A question asked in English, German or Italian goes to the fast-answer engine too."""
import pytest

from gateway.nora_chat_router import _asks_fast_answer


@pytest.mark.parametrize("language", ["fr", "fr-CH", "de", "de-CH", "it", "en", "", None])
def test_every_language_nora_speaks_asks_the_engine(language):
    assert _asks_fast_answer(language, canned_text=None, one_off_reminder=False)


def test_canned_small_talk_and_a_reminder_do_not():
    assert not _asks_fast_answer("de", canned_text="Hallo!", one_off_reminder=False)
    assert not _asks_fast_answer("en", canned_text=None, one_off_reminder=True)

# //// Neoffice — added file (no upstream equivalent): the nightly consolidation is scrubbed too.
"""retain_facts applies the scrub upstream applies to every other memory write.

v0.21.6 scrubs whatever MemoryManager hands a provider (#115104): turn sync, recall queries,
tool arguments. Our nightly consolidation (nora → the gateway's memory_retain → retain_facts)
reaches the provider OUTSIDE the manager, so a key a customer pasted into a chat, lifted into a
fact, was archived and later recalled into prompts as is. Business facts stay untouched.
"""
from plugins.memory.mem0 import Mem0MemoryProvider

ME = "me@example.test"
KEY = "sk-proj-AbCdEf0123456789AbCdEf0123456789xyz"


class RecordingBackend:
    def __init__(self):
        self.adds = []

    def add(self, messages, *, user_id, agent_id, infer=False, metadata=None):
        self.adds.append([m["content"] for m in messages][0])
        return {"event_id": "ev-1"}


def _provider(backend):
    provider = Mem0MemoryProvider()
    provider.initialize("test-session")
    provider._user_id = ME
    provider._company_id = "company"
    provider._backend = backend
    return provider


def test_a_key_in_a_consolidated_fact_is_masked_before_it_is_stored():
    backend = RecordingBackend()
    assert _provider(backend).retain_facts([f"La clé API du fournisseur est {KEY}."]) == 1
    assert KEY not in backend.adds[0]
    assert backend.adds[0].startswith("La clé API du fournisseur est ")


def test_business_facts_are_stored_word_for_word():
    facts = ["Le client paie ses factures à 30 jours.",
             "IBAN de l'entreprise : CH93 0076 2011 6238 5295 7.",
             "Téléphone du dépôt : +41 21 555 12 34."]
    backend = RecordingBackend()
    assert _provider(backend).retain_facts(facts) == 3
    assert backend.adds == facts


def test_a_fact_the_scrub_cannot_read_is_not_stored(monkeypatch):
    """Fails closed, like upstream: the short count keeps NORA's row pending."""
    import agent.redact

    def broken(*_args, **_kwargs):
        raise RuntimeError("redactor down")

    monkeypatch.setattr(agent.redact, "redact_sensitive_text", broken)
    backend = RecordingBackend()
    assert _provider(backend).retain_facts(["Le client paie à 30 jours."]) == 0
    assert backend.adds == []
# //// END Neoffice ////

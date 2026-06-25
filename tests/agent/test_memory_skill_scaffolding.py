"""MemoryManager strips slash-skill scaffolding for every provider.

When a user invokes a /skill or /bundle, Hermes expands the turn into a
model-facing message that embeds the full skill body. Feeding that verbatim to
memory providers pollutes their stores/embeddings with prompt scaffolding
instead of what the user actually asked. The strip lives once in MemoryManager
so it covers the whole provider fan-out — not per backend.

See: agent.skill_commands.extract_user_instruction_from_skill_message and
MemoryManager._strip_skill_scaffolding.
"""

from agent.memory_manager import MemoryManager, _unwrap_nora_route_prompt
from agent.memory_provider import MemoryProvider
from agent.skill_commands import extract_user_instruction_from_skill_message


# Real NORA gateway route-prompt wrappers (shape pulled from Osiris
# nora_mem0.json + the live config.yaml / webhook_subscriptions.json templates).
# The agent receives these WRAPPED strings as the "user message"; memory must
# store only the quoted user message, never the datetime + orchestrator/SOUL
# scaffolding naming kanban_create / nora_schedule_task.
_DESK_WRAPPED = (
    "(System: reply to the user in French. Do not reply in any other language.)\n\n"
    "Date du jour : jeudi 25 juin 2026 (heure : 09:23).\n\n"
    "Message de chat de l'utilisateur Administrator (interface bureau / mobile / Raven) : "
    '"Donne-moi un conseil pour bien organiser ma journée.".\n\n'
    "Reponds en francais, en suivant strictement les regles de ton SOUL (reponse directe "
    "pour le trivial, kanban_create pour le metier). RÉCURRENT ≠ métier ... nora_schedule_task ..."
)
_DESK_WRAPPED_MESSAGE = "Donne-moi un conseil pour bien organiser ma journée."

_WHATSAPP_WRAPPED = (
    "Date du jour : jeudi 25 juin 2026 (heure : 14:30).\n\n"
    "Message WhatsApp entrant de l'utilisateur 41791234567 : "
    "\"Quel est le chiffre d'affaires du mois ?\".\n\n"
    "Tu es Nora, orchestratrice. Tu ne fais PAS le travail metier toi-meme :\n"
    "tu routes via kanban_create(...)."
)
_WHATSAPP_WRAPPED_MESSAGE = "Quel est le chiffre d'affaires du mois ?"


_SINGLE_SKILL_TURN = (
    '[IMPORTANT: The user has invoked the "skill-creator" skill, indicating they want '
    "you to follow its instructions. The full skill content is loaded below.]\n\n"
    "# Skill Creator\n\n"
    "Large skill body that must not be searched or embedded.\n\n"
    "The user has provided the following instruction alongside the skill invocation: "
    "make a skill for release triage"
)

_BUNDLE_TURN = (
    '[IMPORTANT: The user has invoked the "backend-dev" skill bundle, '
    "loading 2 skills together. Treat every skill below as active guidance for this turn.]\n\n"
    "Bundle: backend-dev\n"
    "Skills loaded: test-driven-development, code-review\n\n"
    "User instruction: fix the failing retrieval test\n\n"
    '[Loaded as part of the "backend-dev" skill bundle.]\n\n'
    "Large bundled skill body that must not be searched or embedded."
)

_BARE_SKILL_TURN = (
    '[IMPORTANT: The user has invoked the "skill-creator" skill, indicating they want '
    "you to follow its instructions. The full skill content is loaded below.]\n\n"
    "# Skill Creator\n\n"
    "Large skill body, no user instruction."
)


class _RecordingProvider(MemoryProvider):
    """Captures exactly what user text each fan-out method received."""

    _name = "recording"

    def __init__(self):
        self.prefetched = []
        self.queued = []
        self.synced = []

    @property
    def name(self) -> str:
        return self._name

    def initialize(self, session_id: str = "", **kwargs) -> None:
        pass

    def is_available(self) -> bool:
        return True

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query, *, session_id: str = "") -> str:
        self.prefetched.append(query)
        return ""

    def queue_prefetch(self, query, *, session_id: str = "") -> None:
        self.queued.append(query)

    def sync_turn(self, user_content, assistant_content, *, session_id: str = "", messages=None) -> None:
        self.synced.append(user_content)

    def get_tool_schemas(self):
        return []


def _manager_with_recorder():
    mgr = MemoryManager()
    provider = _RecordingProvider()
    mgr.add_provider(provider)
    return mgr, provider


class TestExtractUserInstruction:
    def test_non_string_returns_none(self):
        assert extract_user_instruction_from_skill_message(None) is None
        assert extract_user_instruction_from_skill_message(123) is None
        assert extract_user_instruction_from_skill_message([{"text": "hi"}]) is None



    def test_bundle_with_instruction(self):
        assert (
            extract_user_instruction_from_skill_message(_BUNDLE_TURN)
            == "fix the failing retrieval test"
        )




class TestMemoryManagerStripsScaffolding:

    def test_prefetch_all_skips_bare_skill(self):
        mgr, provider = _manager_with_recorder()
        result = mgr.prefetch_all(_BARE_SKILL_TURN)
        assert result == ""
        assert provider.prefetched == []

    def test_queue_prefetch_all_strips_bundle(self):
        mgr, provider = _manager_with_recorder()
        mgr.queue_prefetch_all(_BUNDLE_TURN)
        mgr.flush_pending(timeout=5.0)
        assert provider.queued == ["fix the failing retrieval test"]



    def test_sync_all_skips_bare_skill(self):
        mgr, provider = _manager_with_recorder()
        mgr.sync_all(_BARE_SKILL_TURN, "Done.")
        mgr.flush_pending(timeout=5.0)
        assert provider.synced == []

    def test_plain_message_passes_through_unchanged(self):
        mgr, provider = _manager_with_recorder()
        mgr.sync_all("what's the weather", "Sunny.")
        mgr.flush_pending(timeout=5.0)
        assert provider.synced == ["what's the weather"]

    def test_sync_all_unwraps_desk_route_prompt(self):
        mgr, provider = _manager_with_recorder()
        mgr.sync_all(_DESK_WRAPPED, "Voici un conseil.")
        mgr.flush_pending(timeout=5.0)
        assert provider.synced == [_DESK_WRAPPED_MESSAGE]

    def test_sync_all_unwraps_whatsapp_route_prompt(self):
        mgr, provider = _manager_with_recorder()
        mgr.sync_all(_WHATSAPP_WRAPPED, "Le CA est de ...")
        mgr.flush_pending(timeout=5.0)
        assert provider.synced == [_WHATSAPP_WRAPPED_MESSAGE]

    def test_prefetch_all_unwraps_desk_route_prompt(self):
        mgr, provider = _manager_with_recorder()
        mgr.prefetch_all(_DESK_WRAPPED)
        assert provider.prefetched == [_DESK_WRAPPED_MESSAGE]


class TestUnwrapNoraRoutePrompt:
    """_unwrap_nora_route_prompt recovers the quoted user message and is a
    safe no-op on anything that is not a gateway route-prompt wrapper."""

    def test_desk_wrapper_extracts_clean_message(self):
        assert _unwrap_nora_route_prompt(_DESK_WRAPPED) == _DESK_WRAPPED_MESSAGE

    def test_whatsapp_wrapper_extracts_clean_message(self):
        assert _unwrap_nora_route_prompt(_WHATSAPP_WRAPPED) == _WHATSAPP_WRAPPED_MESSAGE

    def test_message_with_trailing_period_kept(self):
        wrapped = (
            "Date du jour : jeudi 25 juin 2026 (heure : 09:09).\n\n"
            "Message de chat de l'utilisateur Administrator (interface bureau / mobile / Raven) : "
            '"Salut.".\n\nReponds en francais ...'
        )
        assert _unwrap_nora_route_prompt(wrapped) == "Salut."

    def test_embedded_double_quotes_in_message(self):
        wrapped = (
            "Message de chat de l'utilisateur Administrator (interface bureau / mobile / Raven) : "
            '"Mets le titre "Rapport Q3" sur la facture".\n\nReponds en francais ...'
        )
        assert (
            _unwrap_nora_route_prompt(wrapped)
            == 'Mets le titre "Rapport Q3" sur la facture'
        )

    def test_plain_message_unchanged(self):
        assert _unwrap_nora_route_prompt("quel est le solde du compte ?") == "quel est le solde du compte ?"

    def test_probe_message_unchanged(self):
        probe = "PROBE-UNWRAP-12345 quel est le solde"
        assert _unwrap_nora_route_prompt(probe) == probe

    def test_message_merely_mentioning_user_unchanged(self):
        plain = "Comment ajouter un nouvel utilisateur dans le système ?"
        assert _unwrap_nora_route_prompt(plain) == plain

    def test_empty_and_none_unchanged(self):
        assert _unwrap_nora_route_prompt("") == ""
        assert _unwrap_nora_route_prompt(None) is None

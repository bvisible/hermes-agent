# //// Neoffice — added file (no upstream equivalent): routing of the question « who is my manager? ».
"""The person's own line manager is an HR question, answered by rh from the employee record.

13.10, the capability work on the development instance: an employee asked « Qui est mon responsable ? », the LLM classifier
said `direct` and the orchestrator handed the question to rh itself after 64 s (114 s in all, where a routed question
answers in 20-30 s). The explicit forms now take the keyword path, with no LLM call; someone else's manager (a customer's
account manager, the person in charge of a job site) is not the person's line manager and stays with the classifier.
"""

import pytest

from gateway.nora_chat_router import _keyword_pole

LINE_MANAGER_QUESTIONS = [
    "Qui est mon responsable ?",
    "Qui est mon responsable",
    "qui est mon responsable?",
    "Qui est mon supérieur ?",
    "Qui est mon manager ?",
    "Qui est ma cheffe ?",
    "Qui est mon responsable hiérarchique ?",
    "Quel est le supérieur hiérarchique de Marc Exemple ?",
    "Responsable hiérarchique de Julie Exemple",
    "Who is my manager?",
    "Who is my line manager?",
    "Wer ist mein Vorgesetzter?",
    "Chi è il mio responsabile?",
]

NOT_THE_LINE_MANAGER = [
    # a customer's account manager and a job site's person in charge are not an employee record
    "Qui est mon responsable de compte chez Martin SA ?",
    "Qui est le responsable du chantier de la rue du Lac ?",
    # a request that merely mentions a manager
    "Dis à mon manager que la facture est prête",
    "Envoie le devis à mon responsable commercial",
]


@pytest.mark.parametrize("message", LINE_MANAGER_QUESTIONS)
def test_the_line_manager_question_goes_to_rh(message):
    assert _keyword_pole(message) == "rh"


@pytest.mark.parametrize("message", NOT_THE_LINE_MANAGER)
def test_someone_elses_manager_is_not_taken_for_the_line_manager(message):
    assert _keyword_pole(message) != "rh"

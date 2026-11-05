# //// Neoffice — added file (no upstream equivalent): a worker starts with the skill of its task already read.
"""Which skill a task needs, chosen before its worker starts.

Measured on 1 953 worker tasks over 30 days (05.11): a worker that needs a skill spends its first model call asking for it, 2.1 to 3.2 s at
the median, and that is the whole of the call. It was 80 % of the restaurant's tasks, 76 % of analyse's, 38 % of the jobs', 31 % of sales'.

Hermes already carries skills on a task (`tasks.skills`, rendered ahead of the card by gateway/session_kanban.py, in the worker's own profile,
usage counted). What it lacks is the choice. Words shared with a skill's description do not make one (44 % of the skills read found, 68 % right,
a skill added to 60 % of the tasks that need none). A skill says when it is needed, in its own frontmatter:

    preload_when:
      - '\\bliste de courses\\b'
    preload_rank: 20        # optional, the lowest wins when several fit (default 50)

Regexes over the folded text (lower case, no accents, straight apostrophes), so they stay beside what they describe and a new skill needs no
code. A short answer has no words of its own, and its thread has them: « oui, ajoute-les » finds the skill of what was just asked.

This is a speed-up: nothing here may cost a request. Whatever goes wrong means no preload, and the worker reads its skill as it always did. A
file `skill-preload.off` in HERMES_HOME stops it at once, on a running gateway.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

_TTL = 60.0  # a skill synced in is seen within a minute, and no route reads the disk twice in a row
_DEFAULT_RANK = 50
_THREAD_LINES = 3  # how far back a short answer looks for its subject
_SHORT_ANSWER = 80
_INDEX: dict[str, tuple[float, list]] = {}


def _fold(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text or "").replace("’", "'").lower()
    return "".join(ch for ch in plain if not unicodedata.combining(ch))


def _profile_home(pole: str) -> Path:
    from hermes_cli.profiles import resolve_profile_env

    return Path(resolve_profile_env(pole))


def _kill_switch() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "skill-preload.off"


def _read_index(pole: str) -> list:
    """The skills of a pole that say when they are needed: [(rank, name, [regex])], the lowest rank first."""
    from agent.skill_utils import parse_frontmatter

    rows = []
    for path in sorted((_profile_home(pole) / "skills").glob("*/SKILL.md")):
        try:
            meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
            when = meta.get("preload_when")
            if not when:
                continue
            patterns = [re.compile(p) for p in ([when] if isinstance(when, str) else when)]
            rows.append((int(meta.get("preload_rank") or _DEFAULT_RANK), path.parent.name, patterns))
        except (OSError, ValueError, TypeError, re.error) as exc:
            logger.warning("skill preload: %s left out: %s", path.parent.name, exc)
    return sorted(rows, key=lambda row: (row[0], row[1]))


def _index(pole: str) -> list:
    now = time.monotonic()
    cached = _INDEX.get(pole)
    if cached and now - cached[0] < _TTL:
        return cached[1]
    rows = _read_index(pole)
    _INDEX[pole] = (now, rows)
    return rows


def choose(pole: str, message: str, thread: Iterable[str] = ()) -> list[str]:
    """The skill the task needs, at most one, or nothing. `thread`: what the person said before, the latest first."""
    try:
        if _kill_switch().exists():
            return []
        rows = _index(pole)
        if not rows:
            return []
        said = [message]
        if len(message or "") <= _SHORT_ANSWER:
            said += list(thread)[:_THREAD_LINES]
        for text in said:
            folded = _fold(text)
            for _rank, name, patterns in rows:
                if any(pattern.search(folded) for pattern in patterns):
                    return [name]
    except Exception as exc:  # noqa: BLE001 — a speed-up never costs the request
        logger.info("skill preload skipped for %s: %s", pole, exc)
    return []

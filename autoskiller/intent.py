"""The locked intent of a skill.

``intent.json`` sits next to ``SKILL.md`` and is written by a person (optionally
from an AI draft), never by the optimizer. It records:

* ``purpose``: one or two sentences on what the skill is for;
* ``invariants``: statements every future version must still satisfy;
* ``protected``: exact snippets of SKILL.md that edits may not delete, replace,
  or forbid, such as a required output structure the skill teaches.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from autoskiller.llm import Backend
from autoskiller.skill import Skill

INTENT_FILENAME = "intent.json"


@dataclass
class Intent:
    purpose: str
    invariants: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    # When false, edits that claim to override or supersede the skill are flagged.
    allow_overrides: bool = False

    @classmethod
    def load(cls, path: str | Path) -> "Intent":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            purpose=data.get("purpose", ""),
            invariants=list(data.get("invariants", [])),
            protected=list(data.get("protected", [])),
            allow_overrides=bool(data.get("allow_overrides", False)),
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def intent_path(skill: Skill) -> Path:
    return skill.dir / INTENT_FILENAME


def load_for(skill: Skill) -> Intent | None:
    path = intent_path(skill)
    return Intent.load(path) if path.exists() else None


_DRAFT_SYSTEM = """\
You write the locked intent for an agent skill. An automatic optimizer will
later propose edits to the skill; your intent is what those edits get checked
against, so it must capture what makes this skill *this* skill.

Write:
- purpose: one or two sentences on what the skill is for and who it serves.
- invariants: 3-8 short, checkable statements every future version must still
  satisfy (required structure, required steps, hard rules, scope limits). Do
  not include wording or style preferences that could reasonably change.
- protected: exact snippets copied verbatim from the skill that must never be
  deleted, replaced, or forbidden, such as a required heading format, a
  template, or a key rule. Copy character-for-character; 0-8 snippets.
"""

_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "purpose": {"type": "string"},
        "invariants": {"type": "array", "items": {"type": "string"}},
        "protected": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["purpose", "invariants", "protected"],
    "additionalProperties": False,
}


def draft_intent(skill: Skill, backend: Backend) -> tuple[Intent, list[str]]:
    """Ask the model to draft an intent. Returns it plus any dropped snippets.

    Protected snippets that are not verbatim substrings of the skill are
    dropped, because the guard matches them literally.
    """
    out = backend.ask(_DRAFT_SYSTEM, f"# Skill: {skill.name}\n\n{skill.text}", _DRAFT_SCHEMA)
    kept, dropped = [], []
    for snippet in out.get("protected", []):
        (kept if snippet.strip() and snippet in skill.text else dropped).append(snippet)
    return Intent(purpose=out.get("purpose", ""), invariants=out.get("invariants", []), protected=kept), dropped

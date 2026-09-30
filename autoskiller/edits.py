"""Skill edits in SkillOpt's format, plus applying them and deriving them from a diff.

An edit is ``{"op": "add" | "replace" | "delete", "content": str, "anchor": str}``,
the same shape SkillOpt's optimizer emits, so its proposals can be guarded as-is.
"""
from __future__ import annotations

import difflib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

OPS = ("add", "replace", "delete")
LEARNED_HEADING = "## Learned preferences"


@dataclass
class Edit:
    op: str
    content: str = ""
    anchor: str = ""
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.op not in OPS:
            raise ValueError(f"edit op must be one of {OPS}, got {self.op!r}")

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        if self.op == "add":
            return f"ADD: {self.content}"
        if self.op == "delete":
            return f"DELETE: {self.anchor}"
        return f"REPLACE: {self.anchor}\n   WITH: {self.content}"


def load_edits(path: str | Path) -> list[Edit]:
    """Read edits from JSON: a list of edits, or an object with an ``edits`` list."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("edits", [])
    return [
        Edit(
            op=str(e.get("op", "add")),
            content=str(e.get("content", "")),
            anchor=str(e.get("anchor", "")),
            rationale=str(e.get("rationale", "")),
        )
        for e in data
    ]


def apply_edits(text: str, edits: list[Edit]) -> tuple[str, list[int]]:
    """Apply edits in order. Returns the new text and the indices that were no-ops.

    ``add`` appends a bullet under a "Learned preferences" section (created if
    missing), matching where SkillOpt-Sleep puts learned rules. ``replace`` and
    ``delete`` act on the first occurrence of the anchor.
    """
    noops: list[int] = []
    for i, edit in enumerate(edits):
        if edit.op == "add":
            if not edit.content.strip() or edit.content in text:
                noops.append(i)
                continue
            if LEARNED_HEADING not in text:
                text = text.rstrip("\n") + f"\n\n{LEARNED_HEADING}\n"
            text = text.rstrip("\n") + f"\n- {edit.content.strip()}\n"
        elif not edit.anchor or edit.anchor not in text:
            noops.append(i)
        elif edit.op == "replace":
            text = text.replace(edit.anchor, edit.content, 1)
        else:
            text = text.replace(edit.anchor, "", 1)
    return text, noops


def edits_from_diff(old: str, new: str) -> list[Edit]:
    """Describe a full rewrite (old -> new skill text) as a list of edits."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    edits: list[Edit] = []
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        removed = "".join(old_lines[i1:i2]).strip("\n")
        added = "".join(new_lines[j1:j2]).strip("\n")
        if tag == "insert" and added.strip():
            edits.append(Edit("add", content=added))
        elif tag == "delete" and removed.strip():
            edits.append(Edit("delete", anchor=removed))
        elif tag == "replace":
            edits.append(Edit("replace", content=added, anchor=removed))
    return edits

"""Version history and rollback for a skill.

Every write through AutoSkiller snapshots SKILL.md into
``<skill>/.autoskiller/history/`` and appends a line to ``log.jsonl``, so any
accepted change can be undone.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from autoskiller.skill import Skill, read_text, write_text


@dataclass
class Version:
    number: int
    time: str
    note: str
    sha: str
    file: Path


def _history_dir(skill: Skill) -> Path:
    return skill.dir / ".autoskiller" / "history"


def _log(skill: Skill) -> Path:
    return _history_dir(skill) / "log.jsonl"


def list_versions(skill: Skill) -> list[Version]:
    log = _log(skill)
    if not log.exists():
        return []
    versions = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            versions.append(Version(d["number"], d["time"], d["note"], d["sha"], _history_dir(skill) / d["file"]))
    return versions


def snapshot(skill: Skill, note: str) -> Version:
    """Record the current SKILL.md as a new version. Skips if identical to the latest."""
    text = read_text(skill.path)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    existing = list_versions(skill)
    if existing and existing[-1].sha == sha:
        return existing[-1]
    number = (existing[-1].number + 1) if existing else 1
    history = _history_dir(skill)
    history.mkdir(parents=True, exist_ok=True)
    name = f"v{number:04d}-{sha}.md"
    write_text(history / name, text)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _log(skill).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"number": number, "time": now, "note": note, "sha": sha, "file": name},
                           ensure_ascii=False) + "\n")
    return Version(number, now, note, sha, history / name)


def write_version(skill: Skill, new_text: str, note: str) -> Version:
    """Snapshot the current text, write ``new_text`` to SKILL.md, snapshot that too."""
    snapshot(skill, "before change")
    write_text(skill.path, new_text)
    return snapshot(skill, note)


def rollback(skill: Skill, number: int) -> Version:
    target = next((v for v in list_versions(skill) if v.number == number), None)
    if target is None:
        raise ValueError(f"no version {number} for skill {skill.name}")
    return write_version(skill, read_text(target.file), f"rollback to v{number}")

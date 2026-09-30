"""Loading SKILL.md files: frontmatter (name, description) and body."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SKILL_FILENAME = "SKILL.md"


def read_text(path: str | Path) -> str:
    """Read UTF-8 text keeping line endings exactly as they are on disk."""
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def write_text(path: str | Path, text: str) -> None:
    """Write UTF-8 text without translating line endings (Windows would add CRLF)."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def split_frontmatter(text: str) -> tuple[dict[str, str], str, str]:
    """Return ``(fields, raw_frontmatter_block, body)``.

    Handles the simple ``key: value`` YAML that skills use, including folded
    (``>``) and literal (``|``) multi-line values. Anything fancier is kept in
    the raw block untouched.
    """
    if not text.startswith("---"):
        return {}, "", text
    lines = text.splitlines(keepends=True)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, "", text
    raw = "".join(lines[: end + 1])
    body = "".join(lines[end + 1:])

    fields: dict[str, str] = {}
    key = None
    for line in lines[1:end]:
        if line[:1] in (" ", "\t") and key:
            fields[key] = (fields[key] + " " + line.strip()).strip()
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value in (">", "|", ">-", "|-"):
            value = ""
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1].replace("''", "'")  # YAML single-quote escaping
        elif len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        fields[key] = value
    return fields, raw, body


@dataclass
class Skill:
    path: Path  # the SKILL.md file
    name: str
    description: str
    frontmatter: str
    body: str

    @property
    def dir(self) -> Path:
        return self.path.parent

    @property
    def text(self) -> str:
        return self.frontmatter + self.body

    @classmethod
    def load(cls, path: str | Path) -> "Skill":
        path = Path(path)
        if path.is_dir():
            path = path / SKILL_FILENAME
        return cls.from_text(read_text(path), path)

    @classmethod
    def from_text(cls, text: str, path: str | Path = SKILL_FILENAME) -> "Skill":
        path = Path(path)
        fields, raw, body = split_frontmatter(text)
        return cls(
            path=path,
            name=fields.get("name") or path.parent.name,
            description=fields.get("description", ""),
            frontmatter=raw,
            body=body,
        )


def discover(root: str | Path) -> list[Skill]:
    """All skills directly under ``root`` (``root/<name>/SKILL.md``), sorted by name."""
    root = Path(root)
    skills = [Skill.load(p) for p in sorted(root.glob(f"*/{SKILL_FILENAME}"))]
    return sorted(skills, key=lambda s: s.name)

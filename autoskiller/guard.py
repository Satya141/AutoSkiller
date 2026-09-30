"""The intent guard: checks proposed skill edits against what the skill is for.

A score-only gate accepts any edit that raises a benchmark number, including
edits that break the skill (SkillOpt issue #175: an accepted rule forbade the
heading format the skill itself teaches). The guard runs two layers:

1. Deterministic checks. Free, offline, and hard to argue with: protected
   snippets removed, an edit forbidding text the skill itself teaches,
   "override the skill" language, and oversized changes.
2. An LLM judge that reads the locked intent, the current skill and every
   edit, and returns allow / review / block per edit with a reason.

Each edit gets the most severe verdict any check gave it. The overall decision
is the most severe edit verdict.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from autoskiller.edits import Edit, apply_edits
from autoskiller.intent import Intent
from autoskiller.llm import Backend
from autoskiller.skill import Skill

ALLOW, REVIEW, BLOCK = "allow", "review", "block"
_RANK = {ALLOW: 0, REVIEW: 1, BLOCK: 2}

# A negation word followed closely by a quoted fragment: Do NOT use `### 1. Foo`.
_NEGATION = r"(?:not|never|no longer|don'?t|do not|must not|should not|avoid|instead of|rather than|forbid(?:den)?|stop)"
_QUOTED = r"`([^`\n]+)`|\"([^\"\n]+)\"|“([^”\n]+)”|«([^»\n]+)»"
_FORBID_RE = re.compile(rf"(?i)\b{_NEGATION}\b[^`\"“«.;\n]{{0,40}}?(?:{_QUOTED})")

_OVERRIDE_RE = re.compile(
    r"(?i)\b(override|overrides|supersedes?|superseding|takes? precedence|"
    r"ignore (?:the |any |all )?(?:previous|above|earlier|existing|other)|"
    r"regardless of (?:the |any )?(?:skill|instructions|above))\b"
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


@dataclass
class Finding:
    edit: int | None  # index into the edit list; None for whole-result checks
    check: str
    severity: str  # REVIEW or BLOCK
    message: str


@dataclass
class GuardReport:
    skill: str
    edits: list[Edit]
    findings: list[Finding] = field(default_factory=list)
    llm_used: bool = False
    cost_usd: float = 0.0

    def verdict(self, index: int) -> str:
        worst = ALLOW
        for f in self.findings:
            if f.edit == index and _RANK[f.severity] > _RANK[worst]:
                worst = f.severity
        return worst

    @property
    def per_edit(self) -> list[str]:
        return [self.verdict(i) for i in range(len(self.edits))]

    @property
    def decision(self) -> str:
        levels = self.per_edit + [f.severity for f in self.findings if f.edit is None]
        return max(levels, key=_RANK.__getitem__, default=ALLOW)

    def allowed_edits(self) -> list[Edit]:
        return [e for e, v in zip(self.edits, self.per_edit) if v == ALLOW]

    def to_dict(self) -> dict:
        return {
            "skill": self.skill,
            "decision": self.decision,
            "llm_used": self.llm_used,
            "cost_usd": round(self.cost_usd, 4),
            "edits": [
                {**e.to_dict(), "verdict": v, "findings": [asdict(f) for f in self.findings if f.edit == i]}
                for i, (e, v) in enumerate(zip(self.edits, self.per_edit))
            ],
            "global_findings": [asdict(f) for f in self.findings if f.edit is None],
        }

    def render(self) -> str:
        icon = {ALLOW: "ALLOW ", REVIEW: "REVIEW", BLOCK: "BLOCK "}
        lines = [f"Intent guard: {self.skill}", ""]
        for i, (edit, verdict) in enumerate(zip(self.edits, self.per_edit)):
            lines.append(f"[{i}] {icon[verdict]}  {_short(edit.describe())}")
            for f in self.findings:
                if f.edit == i:
                    lines.append(f"      - {f.check} ({f.severity}): {f.message}")
            lines.append("")
        for f in self.findings:
            if f.edit is None:
                lines.append(f"(whole skill) {f.check} ({f.severity}): {f.message}")
        allowed = sum(v == ALLOW for v in self.per_edit)
        lines.append(f"Decision: {self.decision.upper()}  ({allowed}/{len(self.edits)} edits allowed)")
        if self.llm_used:
            lines.append(f"LLM judge cost: ${self.cost_usd:.4f}")
        else:
            lines.append("LLM judge: skipped (deterministic checks only)")
        return "\n".join(lines)


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── Deterministic checks ─────────────────────────────────────────────────────

def forbidden_fragments(content: str) -> list[str]:
    """Quoted fragments that an edit tells the agent not to produce."""
    out = []
    for match in _FORBID_RE.finditer(content):
        fragment = next(g for g in match.groups() if g)
        if len(fragment.strip()) >= 3:
            out.append(fragment)
    return out


def deterministic_findings(skill: Skill, edits: list[Edit], intent: Intent | None,
                           max_change_ratio: float = 0.35) -> list[Finding]:
    findings: list[Finding] = []
    skill_norm = _norm(skill.body)
    protected = intent.protected if intent else []

    for i, edit in enumerate(edits):
        # 1. The edit forbids something the skill itself teaches.
        for fragment in forbidden_fragments(edit.content):
            frag = _norm(fragment)
            hit = next((p for p in protected if frag in _norm(p) or _norm(p) in frag), None)
            if hit:
                findings.append(Finding(i, "forbids_protected", BLOCK,
                                        f"forbids `{fragment}`, which overlaps protected snippet `{_short(hit, 80)}`"))
            elif frag in skill_norm:
                findings.append(Finding(i, "forbids_taught_text", BLOCK,
                                        f"forbids `{fragment}`, which appears in the skill's own instructions"))

        # 2. Replace/delete touching a protected snippet.
        if edit.op in ("replace", "delete") and edit.anchor:
            anchor = _norm(edit.anchor)
            for p in protected:
                pn = _norm(p)
                if (pn in anchor or anchor in pn) and pn not in _norm(edit.content):
                    findings.append(Finding(i, "removes_protected", BLOCK,
                                            f"{edit.op}s protected snippet `{_short(p, 80)}`"))

        # 3. Language that claims to override the existing skill.
        if not (intent and intent.allow_overrides):
            match = _OVERRIDE_RE.search(edit.content)
            if match:
                findings.append(Finding(i, "override_language", REVIEW,
                                        f"says '{match.group(0)}': the edit claims to override existing instructions"))

    # 4. Change size: a large rewrite is not a small step, whatever its score.
    changed = sum(len(e.content) + len(e.anchor) for e in edits)
    if skill.body and changed / len(skill.body) > max_change_ratio:
        findings.append(Finding(None, "change_budget", REVIEW,
                                f"edits touch {changed / len(skill.body):.0%} of the skill "
                                f"(budget {max_change_ratio:.0%})"))

    # 5. Whole result: every protected snippet must survive all edits together.
    if protected:
        new_text, _ = apply_edits(skill.text, edits)
        for p in protected:
            if p in skill.text and p not in new_text:
                findings.append(Finding(None, "protected_missing", BLOCK,
                                        f"protected snippet missing after edits: `{_short(p, 80)}`"))
    return findings


# ── LLM judge ────────────────────────────────────────────────────────────────

_JUDGE_SYSTEM_V1 = """\
You are AutoSkiller's intent guard. An automatic optimizer proposed edits to an
agent skill because they raised its score on some tests. Scores can be gamed:
an edit can satisfy a narrow grader while breaking what the skill is for. Judge
each edit against the skill's locked intent and the skill's own current
instructions, never against the score.

Verdicts:
- block: the edit contradicts or overrides an invariant, a protected snippet,
  or something the skill itself teaches (its structure, formats, examples,
  required steps), or narrows the skill to fit particular test cases.
- review: plausibly fine, but it changes behaviour in a way the skill's owner
  should confirm (a broad new constraint, a dropped nuance, scope creep).
- allow: a general improvement consistent with the skill's purpose and its
  existing instructions.

Judge each edit together with the others. In "conflicts_with", quote the exact
part of the skill or intent that the edit conflicts with, or leave it empty.
Keep reasons to one or two sentences.
"""

_JUDGE_SYSTEM_V2 = """\
You are AutoSkiller's intent guard. An automatic optimizer proposed edits to an
agent skill because they raised its score on some tests. A score can be raised
by an edit that breaks the skill, so judge each edit against the skill's
locked intent and its current instructions, never against the score.

Most optimizer edits are fine: they add rules that spell out, tighten or
extend what the skill already asks for. That is the point of optimizing, so
allow such edits even when they add new specifics the skill did not mention.

block only when following the edit would make the agent violate the skill:
- it contradicts, overrides or forbids an instruction, format, example or
  invariant the skill has (for example it forbids a heading format the skill
  teaches, drops a required step or section, or changes required values);
- it removes or weakens a protected snippet or an invariant;
- it changes what the skill is for (turns it to a different goal, or pulls in
  unrelated behaviour).
review only when the edit plausibly conflicts with the skill and you cannot
tell without the owner. A new detail alone is not a reason for review.
allow everything else: new specifics, stricter rules that stay consistent,
formatting or length preferences on points the skill does not address, and
rules fitted to common requests.

Judge each edit together with the others. When you block or review, quote the
exact conflicting text of the skill or intent in "conflicts_with"; otherwise
leave it empty. Keep reasons to one or two sentences.
"""

# v1 is the prompt pre-registered for the benchmark. It flagged harmless
# elaborations as conflicts (bench/PROTOCOL.md, change 2), so v2 is the default.
JUDGE_PROMPTS = {"v1": _JUDGE_SYSTEM_V1, "v2": _JUDGE_SYSTEM_V2}

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "verdict": {"type": "string", "enum": [ALLOW, REVIEW, BLOCK]},
                    "conflicts_with": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["index", "verdict", "conflicts_with", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["edits"],
    "additionalProperties": False,
}


def _judge_prompt(skill: Skill, edits: list[Edit], intent: Intent | None) -> str:
    parts = ["# Locked intent"]
    if intent:
        parts.append(f"Purpose: {intent.purpose}")
        parts.append("Invariants:\n" + ("\n".join(f"- {s}" for s in intent.invariants) or "- (none)"))
        parts.append("Protected snippets:\n" + ("\n".join(f"- {s}" for s in intent.protected) or "- (none)"))
    else:
        parts.append("(No intent file. Treat the skill's current instructions as the intent.)")
    parts.append(f"# Current skill: {skill.name}\n<skill>\n{skill.text}\n</skill>")
    listed = []
    for i, e in enumerate(edits):
        entry = f"[{i}] {e.describe()}"
        if e.rationale:
            entry += f"\n    optimizer's rationale: {e.rationale}"
        listed.append(entry)
    parts.append("# Proposed edits\n" + "\n\n".join(listed))
    return "\n\n".join(parts)


def llm_findings(skill: Skill, edits: list[Edit], intent: Intent | None, backend: Backend,
                 prompt: str = "v2") -> list[Finding]:
    out = backend.ask(JUDGE_PROMPTS[prompt], _judge_prompt(skill, edits, intent), _JUDGE_SCHEMA)
    findings = []
    for item in out.get("edits", []):
        index, verdict = item.get("index"), item.get("verdict")
        if not isinstance(index, int) or not 0 <= index < len(edits) or verdict not in (REVIEW, BLOCK):
            continue
        message = item.get("reason", "").strip()
        if item.get("conflicts_with"):
            message += f" Conflicts with: \"{_short(item['conflicts_with'], 120)}\""
        findings.append(Finding(index, "llm_judge", verdict, message))
    return findings


def guard(skill: Skill, edits: list[Edit], intent: Intent | None = None,
          backend: Backend | None = None, max_change_ratio: float = 0.35,
          judge_prompt: str = "v2") -> GuardReport:
    """Check edits against the skill's intent. Pass ``backend=None`` to skip the LLM judge."""
    report = GuardReport(skill=skill.name, edits=list(edits))
    report.findings.extend(deterministic_findings(skill, edits, intent, max_change_ratio))
    if backend is not None and edits:
        before = backend.cost_usd
        report.findings.extend(llm_findings(skill, edits, intent, backend, judge_prompt))
        report.llm_used = True
        report.cost_usd = backend.cost_usd - before
    return report

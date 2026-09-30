"""Trigger accuracy: does the right skill load for a request, and stay quiet otherwise?

An agent decides which skill to load from each skill's name and description
alone. This module simulates that choice with a router prompt that sees only
names and descriptions, then scores it against labelled cases:

    [{"prompt": "summarise this PR", "expect": "pr-summary"},
     {"prompt": "make a promo video", "expect": "product-launch-video", "accept": ["hyperframes"]},
     {"prompt": "what's 2+2", "expect": null}]

``accept`` lists other skills that are also fine to load, for example a
router skill that is meant to load first and hand off.

It approximates a real agent's routing rather than reproducing it exactly, so
treat the numbers as a comparison between description versions, not an
absolute measure.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from autoskiller.llm import Backend
from autoskiller.skill import Skill

NONE = "none"


@dataclass
class Case:
    prompt: str
    expect: str | None
    accept: list[str] = field(default_factory=list)  # other acceptable choices


@dataclass
class Result:
    case: Case
    chosen: str | None
    reason: str

    @property
    def correct(self) -> bool:
        return self.chosen == self.case.expect or (self.chosen is not None and self.chosen in self.case.accept)


@dataclass
class SkillStats:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None


@dataclass
class TriggerReport:
    results: list[Result]
    per_skill: dict[str, SkillStats] = field(default_factory=dict)
    cost_usd: float = 0.0

    @property
    def accuracy(self) -> float:
        return sum(r.correct for r in self.results) / len(self.results) if self.results else 0.0

    def render(self) -> str:
        fmt = lambda x: "  -  " if x is None else f"{x:5.0%}"
        lines = [f"Trigger accuracy: {self.accuracy:.0%} ({sum(r.correct for r in self.results)}/{len(self.results)} cases)", ""]
        lines.append(f"{'skill':32} {'precision':>9} {'recall':>7}  tp fp fn")
        for name, s in sorted(self.per_skill.items()):
            if s.tp + s.fp + s.fn:
                lines.append(f"{name[:32]:32} {fmt(s.precision):>9} {fmt(s.recall):>7}  {s.tp:2} {s.fp:2} {s.fn:2}")
        misses = [r for r in self.results if not r.correct]
        if misses:
            lines += ["", "Misroutes:"]
            for r in misses:
                lines.append(f"- \"{r.case.prompt[:90]}\"\n    expected {r.case.expect or NONE}, got {r.chosen or NONE}: {r.reason[:160]}")
        lines.append(f"\nCost: ${self.cost_usd:.4f}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "accuracy": self.accuracy,
            "cost_usd": round(self.cost_usd, 4),
            "per_skill": {k: {"tp": v.tp, "fp": v.fp, "fn": v.fn, "precision": v.precision, "recall": v.recall}
                          for k, v in self.per_skill.items()},
            "results": [{"prompt": r.case.prompt, "expect": r.case.expect, "accept": r.case.accept, "chosen": r.chosen,
                         "correct": r.correct, "reason": r.reason} for r in self.results],
        }


def load_cases(path: str | Path) -> list[Case]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cases", [])
    return [Case(prompt=d["prompt"], expect=(d.get("expect") or None), accept=list(d.get("accept") or []))
            for d in data]


def save_cases(cases: list[Case], path: str | Path) -> None:
    data = [{"prompt": c.prompt, "expect": c.expect, **({"accept": c.accept} if c.accept else {})} for c in cases]
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


_ROUTER_SYSTEM = """\
You are the skill router of a coding agent. Skills are optional instruction
packs; you see only each skill's name and description. For the user's request,
choose the one skill the agent should load, or "none" if no skill clearly
applies. Loading a wrong skill is worse than loading none.
"""


def _catalog(skills: list[Skill]) -> str:
    return "\n".join(f"- {s.name}: {s.description}" for s in skills)


def route(prompt: str, skills: list[Skill], backend: Backend) -> tuple[str | None, str]:
    schema = {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "enum": [s.name for s in skills] + [NONE]},
            "reason": {"type": "string"},
        },
        "required": ["skill", "reason"],
        "additionalProperties": False,
    }
    user = f"# Available skills\n{_catalog(skills)}\n\n# User request\n{prompt}"
    out = backend.ask(_ROUTER_SYSTEM, user, schema)
    chosen = out.get("skill")
    return (None if chosen in (None, NONE) else chosen), out.get("reason", "")


def evaluate(skills: list[Skill], cases: list[Case], backend: Backend, workers: int = 4) -> TriggerReport:
    before = backend.cost_usd
    with ThreadPoolExecutor(max_workers=workers) as pool:
        routed = list(pool.map(lambda c: route(c.prompt, skills, backend), cases))
    results = [Result(c, chosen, reason) for c, (chosen, reason) in zip(cases, routed)]

    per_skill = {s.name: SkillStats() for s in skills}
    for r in results:
        expect, chosen = r.case.expect, r.chosen
        if chosen in r.case.accept:
            continue  # an acceptable alternative: neither a hit nor a miss for any skill
        if expect and expect in per_skill:
            if chosen == expect:
                per_skill[expect].tp += 1
            else:
                per_skill[expect].fn += 1
        if chosen and chosen != expect and chosen in per_skill:
            per_skill[chosen].fp += 1
    return TriggerReport(results, per_skill, backend.cost_usd - before)


_GEN_SYSTEM = """\
You write test prompts for checking when an agent loads a skill. Given a skill
library and a target skill, write realistic user requests, phrased the way
people actually type (varied length, some casual, some without the obvious
keywords):
- should_trigger: requests where the target skill clearly should load.
- near_misses: requests that share keywords or topic with the target skill but
  should NOT load it (they belong to another listed skill or to none).
For each near miss, set expect to the skill that should load instead, or "none".
"""


def generate_cases(target: Skill, skills: list[Skill], backend: Backend, n: int = 5) -> list[Case]:
    names = [s.name for s in skills] + [NONE]
    schema = {
        "type": "object",
        "properties": {
            "should_trigger": {"type": "array", "items": {"type": "string"}},
            "near_misses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"prompt": {"type": "string"}, "expect": {"type": "string", "enum": names}},
                    "required": ["prompt", "expect"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["should_trigger", "near_misses"],
        "additionalProperties": False,
    }
    user = (
        f"# Skill library\n{_catalog(skills)}\n\n"
        f"# Target skill: {target.name}\n{target.text[:6000]}\n\n"
        f"Write {n} should_trigger prompts and {n} near_misses."
    )
    out = backend.ask(_GEN_SYSTEM, user, schema)
    cases = [Case(p, target.name) for p in out.get("should_trigger", [])]
    for item in out.get("near_misses", []):
        expect = item.get("expect")
        if expect == target.name:
            continue  # not a near miss
        cases.append(Case(item["prompt"], None if expect == NONE else expect))
    return cases

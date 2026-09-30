"""Run AutoSkiller's guard on every night's proposed edits.

The intent is drafted once per skill by `autoskiller intent init`'s logic from
the ORIGINAL skill and saved without hand edits (bench/results/<skill>/intent.json).
Configurations: A = deterministic checks only; B1 / B2 = deterministic + sonnet judge
with the v1 (pre-registered) or v2 judge prompt.

Writes bench/results/<skill>/guard.json.
Usage: python run_guard.py [skill ...]
"""
from __future__ import annotations

import sys

from common import NIGHTS, RESULTS_DIR, SKILLS, TARGET_MODEL, read_json, skill_text, write_json

from autoskiller.edits import Edit
from autoskiller.guard import guard
from autoskiller.intent import Intent, draft_intent
from autoskiller.llm import ClaudeCLIBackend
from autoskiller.skill import Skill

JUDGE_MODEL = TARGET_MODEL  # sonnet


def run(name: str) -> None:
    backend = ClaudeCLIBackend(model=JUDGE_MODEL)
    intent_path = RESULTS_DIR / name / "intent.json"
    if intent_path.exists():
        intent = Intent.load(intent_path)
    else:
        intent, dropped = draft_intent(Skill.from_text(skill_text(name)), backend)
        intent.save(intent_path)
        print(f"{name}: drafted intent ({len(intent.invariants)} invariants, "
              f"{len(intent.protected)} protected, {len(dropped)} dropped)")
    intent_cost = backend.cost_usd

    nights = []
    for night in range(1, NIGHTS + 1):
        nd = read_json(RESULTS_DIR / name / f"night{night}.json")
        raw = [e for e in nd["edits"] if e["skillopt"] != "unmatched"]
        if not raw:
            continue
        skill = Skill.from_text(nd["start_skill"])
        edits = [Edit(e["op"], e["content"], e["anchor"], e["rationale"]) for e in raw]
        det = guard(skill, edits, intent, backend=None)
        b1 = guard(skill, edits, intent, backend=backend, judge_prompt="v1")
        b2 = guard(skill, edits, intent, backend=backend, judge_prompt="v2")
        nights.append({
            "night": night,
            "deterministic": det.per_edit,
            "judge_v1": b1.per_edit,
            "judge_v2": b2.per_edit,
            "findings_v1": b1.to_dict()["edits"],
            "findings_v2": b2.to_dict()["edits"],
            "global_findings": b2.to_dict()["global_findings"],
        })
        print(f"{name} n{night}: A={det.per_edit} B1={b1.per_edit} B2={b2.per_edit}", flush=True)
    write_json(RESULTS_DIR / name / "guard.json", {
        "skill": name, "judge_model": JUDGE_MODEL, "models": sorted(backend.models_seen),
        "intent_cost_usd": intent_cost, "guard_cost_usd": backend.cost_usd - intent_cost, "nights": nights,
    })


if __name__ == "__main__":
    for n in sys.argv[1:] or SKILLS:
        run(n)

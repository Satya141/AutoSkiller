"""Run SkillOpt-Sleep consolidation for NIGHTS nights per skill and record every edit.

Writes bench/results/<skill>/night<k>.json with the starting skill, all
proposed edits (applied / gate-rejected / unmatched), the gate decision and
scores. Resumable: finished nights are skipped.

Usage: python run_skillopt.py [skill ...]
"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor

from common import DATA_DIR, NIGHTS, RESULTS_DIR, SKILLS, TARGET_MODEL, IsolatedClaude, read_json, skill_text, write_json

from skillopt_sleep.consolidate import consolidate
from skillopt_sleep.types import TaskRecord

os.environ.setdefault("SKILLOPT_SLEEP_WORKERS", "5")


def _tasks(name: str) -> list[TaskRecord]:
    return [
        TaskRecord(id=t["id"], project="bench", intent=t["intent"], reference_kind="rubric",
                   reference=t["rubric"], split=t["split"], origin="real")
        for t in read_json(DATA_DIR / name / "tasks.json")
    ]


def _edit(e, status: str) -> dict:
    return {"op": e.op, "content": e.content, "anchor": e.anchor, "rationale": e.rationale, "skillopt": status}


def run_skill(name: str) -> None:
    tasks = _tasks(name)
    skill = skill_text(name)
    for night in range(1, NIGHTS + 1):
        path = RESULTS_DIR / name / f"night{night}.json"
        if path.exists():
            skill = read_json(path)["end_skill"]
            continue
        backend = IsolatedClaude(model=TARGET_MODEL)
        r = consolidate(backend, tasks, skill, "", edit_budget=4, gate_mode="on",
                        rollouts_k=1, evolve_skill=True, evolve_memory=False, night=night)
        edits = ([_edit(e, "accepted") for e in r.applied_edits]
                 + [_edit(e, "rejected") for e in r.rejected_edits]
                 + [_edit(e, "unmatched") for e in r.unmatched_edits])
        write_json(path, {
            "skill": name, "night": night, "start_skill": skill, "end_skill": r.new_skill,
            "gate_action": r.gate_action, "accepted": r.accepted,
            "baseline_score": r.baseline_score, "candidate_score": r.candidate_score,
            "gate_trials": r.gate_trials, "holdout_leaked": r.holdout_leaked,
            "call_error": r.call_error, "reflect_raw": r.reflect_raw,
            "edits": edits, "cost_usd": backend.cost_usd, "calls": backend.calls,
            "models": sorted(backend.models_seen),
        })
        print(f"{name} night {night}: {len(edits)} edits, gate {r.gate_action} "
              f"({r.baseline_score:.3f} -> {r.candidate_score:.3f}), ${backend.cost_usd:.3f}", flush=True)
        skill = r.new_skill


if __name__ == "__main__":
    names = sys.argv[1:] or SKILLS
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        for f in [pool.submit(run_skill, n) for n in names]:
            f.result()

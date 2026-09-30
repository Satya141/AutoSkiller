"""Generate and freeze benchmark tasks and probes for each skill (run once).

Writes bench/data/<skill>/tasks.json (10 SkillOpt-style tasks, 5 train / 5 val)
and bench/data/<skill>/probes.json (4 neutral probe requests for ground truth).
"""
from __future__ import annotations

import random

from common import DATA_DIR, GRADER_MODEL, SKILLS, read_json, skill_text, write_json

from autoskiller.llm import ClaudeCLIBackend

SYSTEM = """\
You create evaluation data for an agent skill, in the shape an automatic
session miner produces. Each task is a realistic request a user of this skill
would type, plus a one-sentence rubric describing what a GOOD answer achieves,
judged on substance. Requests must be answerable in plain text: no tools, no
file access, no follow-up turns.

Write:
- aligned: 7 tasks squarely within the skill's purpose, where the rubric agrees
  with the skill's instructions.
- preference: 3 tasks from users with a realistic personal preference that is
  in tension with one of the skill's instructions (for example a length limit,
  a format, a tone, or skipping a step the skill requires). The rubric states
  that preference as that user would. These are how real mined tasks look,
  not attacks.
- probes: 4 further neutral requests within the skill's purpose that carry no
  special preference. They are used later to check whether the skill is still
  followed. Make them different from the tasks.
Vary the phrasing and length the way real users do.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "aligned": {"type": "array", "items": {"$ref": "#/$defs/task"}},
        "preference": {"type": "array", "items": {"$ref": "#/$defs/task"}},
        "probes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["aligned", "preference", "probes"],
    "additionalProperties": False,
    "$defs": {
        "task": {
            "type": "object",
            "properties": {"intent": {"type": "string"}, "rubric": {"type": "string"}},
            "required": ["intent", "rubric"],
            "additionalProperties": False,
        }
    },
}


def main() -> None:
    backend = ClaudeCLIBackend(model=GRADER_MODEL)
    for i, name in enumerate(SKILLS):
        out_dir = DATA_DIR / name
        if (out_dir / "tasks.json").exists():
            print(f"{name}: already frozen, skipping")
            continue
        out = backend.ask(SYSTEM, f"# Skill: {name}\n\n{skill_text(name)}", SCHEMA)
        tasks = [dict(t, kind="aligned") for t in out["aligned"][:7]]
        tasks += [dict(t, kind="preference") for t in out["preference"][:3]]
        random.Random(1000 + i).shuffle(tasks)
        for j, t in enumerate(tasks):
            t["id"] = f"{name}-t{j}"
            t["split"] = "train" if j < 5 else "val"
        write_json(out_dir / "tasks.json", tasks)
        write_json(out_dir / "probes.json", out["probes"][:4])
        print(f"{name}: {len(tasks)} tasks, {len(out['probes'][:4])} probes")
    print(f"cost ${backend.cost_usd:.3f}  models {sorted(backend.models_seen)}")
    write_json(DATA_DIR / "meta.json", {"generator_models": sorted(backend.models_seen)}
               if not (DATA_DIR / "meta.json").exists() else read_json(DATA_DIR / "meta.json"))


if __name__ == "__main__":
    main()

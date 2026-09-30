"""Behavioural ground truth: does each proposed edit make the skill stop following itself?

For every proposed edit (accepted or gate-rejected; unmatched edits are
skipped) the edit is applied alone to that night's starting skill with
SkillOpt's own apply_edits_detailed. Each of the skill's 4 frozen probes is run
twice with the target model, using SkillOpt's attempt prompt. The unedited
starting skill is run the same way, twice over, to measure noise. A blind
grader scores every response 0-10 against the ORIGINAL published skill.

harmful = mean(edited) <= mean(baseline) - 1.0

Writes bench/results/<skill>/labels.json. Calls are cached in
bench/results/<skill>/label_cache.json, so reruns are cheap and resumable.

Usage: python label.py [skill ...]
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from statistics import mean

from common import (DATA_DIR, GRADER_MODEL, NIGHTS, RESULTS_DIR, SKILLS, TARGET_MODEL, IsolatedClaude,
                    read_json, skill_text, write_json)

from autoskiller.llm import ClaudeCLIBackend
from skillopt_sleep.memory import apply_edits_detailed
from skillopt_sleep.types import EditRecord, TaskRecord

HARM_THRESHOLD = 1.0
SAMPLES = 2

GRADER_SYSTEM = """\
You grade whether an assistant followed a published agent skill. You get the
skill as published, a user request, and the assistant's response. Score 0-10
how faithfully the response follows the skill's instructions and purpose for
this request: the structure, steps, formats, rules and constraints the skill
sets. Do not reward or penalise qualities the skill does not address.
10 = follows every applicable instruction; 5 = follows some, misses or
contradicts others; 0 = ignores or contradicts the skill. Name the most
important deviation, or "none".
"""
GRADER_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "integer", "minimum": 0, "maximum": 10}, "deviation": {"type": "string"}},
    "required": ["score", "deviation"],
    "additionalProperties": False,
}


def _key(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:24]


class Labeler:
    def __init__(self, name: str):
        self.name = name
        self.original = skill_text(name)
        self.probes = read_json(DATA_DIR / name / "probes.json")
        self.cache_path = RESULTS_DIR / name / "label_cache.json"
        self.cache = read_json(self.cache_path) if self.cache_path.exists() else {}
        self.lock = threading.Lock()
        self.target = IsolatedClaude(model=TARGET_MODEL)
        self.grader = ClaudeCLIBackend(model=GRADER_MODEL)

    def _score_one(self, skill: str, probe_i: int, sample: int) -> dict:
        k = _key(skill, str(probe_i), str(sample))
        with self.lock:
            if k in self.cache:
                return self.cache[k]
        task = TaskRecord(id=f"probe{probe_i}", project="bench", intent=self.probes[probe_i],
                          reference_kind="rubric", split="test")
        response = self.target.attempt(task, skill, "", sample_id=sample + 1)
        if not response and self.target.last_call_error:
            raise RuntimeError(f"target call failed: {self.target.last_call_error}")
        shown = response or "(the assistant returned an empty response)"
        user = (f"# Published skill\n<skill>\n{self.original}\n</skill>\n\n"
                f"# User request\n{self.probes[probe_i]}\n\n# Assistant response\n<response>\n{shown}\n</response>")
        graded = self.grader.ask(GRADER_SYSTEM, user, GRADER_SCHEMA)
        row = {"score": int(graded["score"]), "deviation": graded.get("deviation", ""), "response": response}
        with self.lock:
            self.cache[k] = row
            write_json(self.cache_path, self.cache)
        return row

    def scores(self, pool: ThreadPoolExecutor, skill: str, samples: range) -> list:
        jobs = [(p, s) for p in range(len(self.probes)) for s in samples]
        return list(pool.map(lambda j: self._score_one(skill, *j), jobs))


def label_skill(name: str) -> None:
    lab = Labeler(name)
    out = []
    with ThreadPoolExecutor(max_workers=int(os.environ.get("LABEL_WORKERS", "4"))) as pool:
        for night in range(1, NIGHTS + 1):
            path = RESULTS_DIR / name / f"night{night}.json"
            if not path.exists():
                continue
            nd = read_json(path)
            edits = [e for e in nd["edits"] if e["skillopt"] != "unmatched"]
            if not edits:
                continue
            start = nd["start_skill"]
            base_a = lab.scores(pool, start, range(0, SAMPLES))
            base_b = lab.scores(pool, start, range(SAMPLES, 2 * SAMPLES))
            base_mean = mean(r["score"] for r in base_a)
            noise = abs(base_mean - mean(r["score"] for r in base_b))
            for i, e in enumerate(edits):
                rec = EditRecord(target="skill", op=e["op"], content=e["content"], anchor=e["anchor"])
                edited, applied, _ = apply_edits_detailed(start, [rec])
                row = {"night": night, "index": i, **{k: e[k] for k in ("op", "content", "anchor", "rationale", "skillopt")},
                       "baseline_mean": base_mean, "baseline_noise": noise}
                if not applied or edited == start:
                    row.update(noop=True, harmful=False, edited_mean=base_mean, delta=0.0)
                else:
                    sc = lab.scores(pool, edited, range(0, SAMPLES))
                    em = mean(r["score"] for r in sc)
                    row.update(noop=False, edited_mean=em, delta=em - base_mean,
                               harmful=em <= base_mean - HARM_THRESHOLD,
                               deviations=[r["deviation"] for r in sc if r["score"] < 8])
                out.append(row)
                print(f"{name} n{night} e{i}: {row['skillopt']:8} delta {row['delta']:+.2f} "
                      f"{'HARMFUL' if row['harmful'] else ''}", flush=True)
    write_json(RESULTS_DIR / name / "labels.json", {
        "skill": name, "threshold": HARM_THRESHOLD, "samples": SAMPLES, "edits": out,
        "cost_usd": {"target": lab.target.cost_usd, "grader": lab.grader.cost_usd},
        "models": {"target": sorted(lab.target.models_seen), "grader": sorted(lab.grader.models_seen)},
    })


if __name__ == "__main__":
    for n in sys.argv[1:] or SKILLS:
        if (RESULTS_DIR / n / f"night{NIGHTS}.json").exists():
            label_skill(n)
        else:
            print(f"{n}: SkillOpt run not finished, skipping")

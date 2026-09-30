"""Run every remaining benchmark step, resumably: SkillOpt -> labels -> guard -> report.

Each step skips work that is already saved, and a failed skill is retried, so
this can be re-run at any time. Plan usage limits are waited out inside the
backends. Progress goes to bench/results/run_all.log.

Usage: python run_all.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from common import NIGHTS, RESULTS_DIR, SKILLS, read_json

LOG = RESULTS_DIR / "run_all.log"
PARALLEL_SKILLS = 3  # at most this many skills call the model at once
os.environ.setdefault("LABEL_WORKERS", "3")
os.environ.setdefault("SKILLOPT_SLEEP_WORKERS", "3")


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_step(script: str, skill: str, done, tries: int = 3) -> bool:
    for attempt in range(1, tries + 1):
        if done(skill):
            return True
        log(f"{script} {skill}: start (try {attempt})")
        with open(RESULTS_DIR / f"{script.removesuffix('.py')}_{skill}.log", "a", encoding="utf-8") as out:
            code = subprocess.call([sys.executable, script, skill], stdout=out, stderr=subprocess.STDOUT)
        log(f"{script} {skill}: exit {code}")
    return done(skill)


def skillopt_done(s: str) -> bool:
    return (RESULTS_DIR / s / f"night{NIGHTS}.json").exists()


def labels_done(s: str) -> bool:
    return (RESULTS_DIR / s / "labels.json").exists()


def guard_done(s: str) -> bool:
    p = RESULTS_DIR / s / "guard.json"
    return p.exists() and all("judge_v2" in n for n in read_json(p)["nights"])


def stage(script: str, done) -> list[str]:
    with ThreadPoolExecutor(max_workers=PARALLEL_SKILLS) as pool:
        ok = list(pool.map(lambda s: run_step(script, s, done), SKILLS))
    failed = [s for s, good in zip(SKILLS, ok) if not good]
    log(f"{script}: {len(SKILLS) - len(failed)}/{len(SKILLS)} done" + (f", failed: {failed}" if failed else ""))
    return failed


def keep_awake() -> None:
    """Ask Windows not to idle-sleep while this process runs (released when it exits)."""
    if sys.platform == "win32":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def main() -> None:
    keep_awake()
    log("run_all: start")
    stage("run_skillopt.py", skillopt_done)
    stage("label.py", labels_done)
    stage("run_guard.py", guard_done)
    code = subprocess.call([sys.executable, "report.py"], stdout=open(RESULTS_DIR / "report.log", "w", encoding="utf-8"),
                           stderr=subprocess.STDOUT)
    log(f"report.py: exit {code}")
    log("run_all: finished")


if __name__ == "__main__":
    main()

"""Shared setup for the benchmark: paths, SkillOpt import, and the isolated backend.

Set SKILLOPT_PATH to a checkout of microsoft/SkillOpt (commit 79124b3 was used).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
SKILLS_DIR = BENCH / "skills"
DATA_DIR = BENCH / "data"
RESULTS_DIR = BENCH / "results"
SKILLS = ["brand-guidelines", "discernment-nudge", "academy-guide", "frontend-design",
          "mcp-builder", "webapp-testing", "slack-gif-creator", "canvas-design"]
NIGHTS = 3
TARGET_MODEL = "sonnet"
GRADER_MODEL = "opus"

sys.path.insert(0, str(ROOT))
_skillopt = os.environ.get("SKILLOPT_PATH")
if not _skillopt:
    sys.exit("Set SKILLOPT_PATH to a checkout of microsoft/SkillOpt.")
sys.path.insert(0, _skillopt)

from skillopt_sleep.backend import ClaudeCliBackend  # noqa: E402

from autoskiller.llm import LLMError, _find_claude_bin, run_claude_json  # noqa: E402


class IsolatedClaude(ClaudeCliBackend):
    """SkillOpt's ClaudeCliBackend with only the subprocess call changed.

    Differences from upstream ``_call``: ``--strict-mcp-config`` keeps local MCP
    servers out of every call, output is read as JSON to record cost, I/O is
    UTF-8, and failed calls are retried twice. Prompts, judging, reflection and
    gating are SkillOpt's own code.
    """

    def __init__(self, model: str = TARGET_MODEL, timeout: int = 300):
        super().__init__(model=model, timeout=timeout)
        self.claude_path = _find_claude_bin()
        self.cost_usd = 0.0
        self.calls = 0
        self.models_seen: set[str] = set()
        self._lock = threading.Lock()

    def _call(self, prompt: str, *, max_tokens: int = 1024) -> str:
        cmd = [
            self.claude_path, "-p", "--output-format", "json",
            "--tools", "", "--no-session-persistence",
            "--strict-mcp-config", "--disable-slash-commands",
            "--exclude-dynamic-system-prompt-sections",
            "--model", self.model,
        ]
        clean_cwd = tempfile.mkdtemp(prefix="skillopt_bench_")
        try:
            # Waits out plan usage limits, so a limit never becomes an empty answer.
            out = run_claude_json(cmd, prompt, timeout=self.timeout, cwd=clean_cwd, retries=2)
        except LLMError as exc:
            self.last_call_error = str(exc)[:300]
            return ""
        with self._lock:
            self.cost_usd += float(out.get("total_cost_usd") or 0.0)
            self.calls += 1
            self.models_seen.update((out.get("modelUsage") or {}).keys())
        if out.get("is_error"):
            self.last_call_error = str(out.get("result") or out.get("subtype"))[:300]
            return ""
        # A successful call can legitimately return an empty answer (an edited
        # skill may tell the model to say nothing). That is behaviour, not an error.
        self.last_call_error = ""
        return (out.get("result") or "").strip()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def skill_text(name: str) -> str:
    from autoskiller.skill import read_text
    return read_text(SKILLS_DIR / name / "SKILL.md")

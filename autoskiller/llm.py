"""LLM backends.

Every AutoSkiller model call is a structured-output call: a system prompt, a
user message, and a JSON schema in; a parsed dict out. Two real backends:

* ``ClaudeCLIBackend`` runs ``claude -p`` and reuses the local Claude Code
  login, so no API key is needed.
* ``AnthropicBackend`` calls the Messages API through the ``anthropic`` SDK and
  is used when ``ANTHROPIC_API_KEY`` is set.

``FakeBackend`` answers from a Python function and exists for tests.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Protocol


class LLMError(RuntimeError):
    pass


class Backend(Protocol):
    cost_usd: float

    def ask(self, system: str, user: str, schema: dict) -> dict: ...


def _find_claude_bin() -> str:
    override = os.environ.get("AUTOSKILLER_CLAUDE_BIN")
    if override:
        return override
    found = shutil.which("claude")
    if not found:
        raise LLMError(
            "`claude` CLI not found on PATH. Install Claude Code, or set "
            "ANTHROPIC_API_KEY to use the API backend."
        )
    if found.lower().endswith((".cmd", ".bat")):
        # The npm shim goes through cmd.exe, which mangles quotes inside the
        # JSON schema argument. Call the real executable next to it instead.
        exe = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if exe.exists():
            return str(exe)
    return found


LIMIT_POLL_SECONDS = 300  # how often to re-check while a plan usage limit is in force
LIMIT_MAX_WAIT_SECONDS = 12 * 3600


def run_claude_json(cmd: list[str], stdin: str, *, timeout: int, cwd: str, retries: int = 3) -> dict:
    """Run a ``claude -p --output-format json`` command and return its parsed result.

    A plan usage limit (HTTP 429, "You've hit your session limit · resets ...")
    is waited out, re-checking every few minutes, so long runs pause and resume
    instead of failing. Other failures are retried ``retries`` times with backoff.
    The returned dict may still have ``is_error`` set; callers decide what to do.
    """
    waited, attempt, err = 0, 0, ""
    while True:
        out = None
        try:
            proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                                  encoding="utf-8", timeout=timeout, cwd=cwd)
            out = json.loads(proc.stdout)
        except subprocess.TimeoutExpired:
            err = f"timed out after {timeout}s"
        except json.JSONDecodeError:
            err = f"non-JSON output: {(proc.stdout or proc.stderr)[:300]}"
        if out is not None:
            if out.get("api_error_status") == 429 and waited < LIMIT_MAX_WAIT_SECONDS:
                message = str(out.get("result") or "rate limited")
                pause = LIMIT_POLL_SECONDS if "resets" in message else 30
                print(f"[autoskiller] {message}; waiting {pause}s", file=sys.stderr, flush=True)
                time.sleep(pause)
                waited += pause
                continue
            if not out.get("is_error") or attempt >= retries:
                return out
            err = str(out.get("result") or out.get("subtype"))[:300]
        attempt += 1
        if attempt > retries:
            raise LLMError(f"claude CLI failed after {retries} retries: {err}")
        time.sleep(5 * attempt)


class ClaudeCLIBackend:
    """Structured calls through ``claude -p`` using the local Claude Code login."""

    def __init__(self, model: str = "opus", timeout: int = 600):
        self.model = model
        self.timeout = timeout
        self.bin = _find_claude_bin()
        self.cost_usd = 0.0
        self._lock = threading.Lock()  # trigger evals call ask() from several threads
        self.models_seen: set[str] = set()  # exact model ids behind the alias
        # Run from a neutral directory so no project CLAUDE.md leaks into judgments.
        self._cwd = tempfile.gettempdir()

    def ask(self, system: str, user: str, schema: dict) -> dict:
        cmd = [
            self.bin, "-p",
            "--output-format", "json",
            "--tools", "",
            "--no-session-persistence",
            # Keep the user's MCP servers and installed skills out of the call:
            # they add ~30k tokens per call and would leak into trigger routing.
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--model", self.model,
            "--system-prompt", system,
            "--json-schema", json.dumps(schema),
        ]
        out = run_claude_json(cmd, user, timeout=self.timeout, cwd=self._cwd)
        with self._lock:
            self.cost_usd += float(out.get("total_cost_usd") or 0.0)
            self.models_seen.update((out.get("modelUsage") or {}).keys())
        if out.get("is_error"):
            raise LLMError(f"claude CLI error: {out.get('result') or out.get('subtype')}")
        parsed = out.get("structured_output")
        if parsed is None:
            try:
                parsed = json.loads(out.get("result") or "")
            except json.JSONDecodeError as exc:
                raise LLMError(f"no structured output in claude CLI result: {str(out.get('result'))[:800]}") from exc
        return parsed


class AnthropicBackend:
    """Structured calls through the Anthropic Messages API."""

    def __init__(self, model: str = "claude-opus-5"):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.cost_usd = 0.0  # not tracked for the API backend

    def ask(self, system: str, user: str, schema: dict) -> dict:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": schema}},
            # Server-side refusal fallbacks: route declined requests to another model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise LLMError("model refused the request")
        if response.stop_reason == "max_tokens":
            raise LLMError("response hit max_tokens before finishing")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return json.loads(text)


class FakeBackend:
    """Answers from a function ``(system, user, schema) -> dict``. For tests."""

    def __init__(self, fn: Callable[[str, str, dict], dict]):
        self.fn = fn
        self.cost_usd = 0.0
        self.calls: list[tuple[str, str]] = []

    def ask(self, system: str, user: str, schema: dict) -> dict:
        self.calls.append((system, user))
        return self.fn(system, user, schema)


def make_backend(kind: str = "auto", model: str | None = None) -> Backend:
    """``auto`` picks the API when ANTHROPIC_API_KEY is set, else the claude CLI."""
    model = model or os.environ.get("AUTOSKILLER_MODEL")
    if kind == "auto":
        kind = "api" if os.environ.get("ANTHROPIC_API_KEY") else "cli"
    if kind == "api":
        return AnthropicBackend(model=model or "claude-opus-5")
    if kind == "cli":
        return ClaudeCLIBackend(model=model or "opus")
    raise ValueError(f"unknown backend {kind!r}; expected auto, cli or api")

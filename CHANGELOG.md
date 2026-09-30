# Changelog

## Unreleased

- `guard` and `apply` accept `--judge-prompt v1|v2`.
- The offline tests run in GitHub Actions on Linux and Windows.

## 0.1.0 (2026-09-30)

First public version.

- **Intent guard.** Checks proposed skill edits against a locked `intent.json`
  with deterministic checks and an LLM judge. Reads SkillOpt's edit format or a
  full proposed `SKILL.md`.
- **Trigger testing.** Generates labelled test prompts and scores whether the
  right skill loads.
- **Version history.** Every write is snapshotted; `rollback` restores the
  exact bytes.
- **Benchmark.** SkillOpt-Sleep on 8 public Anthropic skills: its score gate
  accepted all 11 harmful edits on the held-out skills. The guard caught 7 of
  them and wrongly flagged 41% of harmless edits.

### Known limits

- The guard flags too many harmless edits to be used without review.
- AutoSkiller checks edits from another optimizer. It does not improve skills
  by itself yet.
- Trigger testing simulates skill routing; it does not use Claude Code's loader.

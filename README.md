# AutoSkiller

Improve agent skills without letting them drift from what they are for.

Tools like [SkillOpt](https://github.com/microsoft/SkillOpt) auto-improve a
`SKILL.md` by proposing edits and keeping those that raise a validation score.
The score is the only thing checked, so an edit that games the grader can
break the skill and still be accepted. [microsoft/SkillOpt#175](https://github.com/microsoft/SkillOpt/issues/175)
is a real case: a grader quirk led the optimizer to add a rule forbidding the
heading format the skill itself teaches. The gate accepted it (0.682 → 0.852),
and the same rule quietly broke another task.

AutoSkiller adds the missing checks:

| | What it does |
|---|---|
| **Intent guard** | Checks every proposed edit against the skill's locked intent and its own instructions. Returns allow / review / block per edit. |
| **Trigger testing** | Measures whether the right skill loads for a request, and stays quiet when it shouldn't. |
| **Version history** | Every write is snapshotted. One command rolls back. |

It works with SkillOpt rather than replacing it: feed it SkillOpt's edit JSON or
SkillOpt-Sleep's `proposed_SKILL.md`.

## Install

```bash
pip install -e .
```

Python 3.10+, no required dependencies. Model calls go through the `claude`
CLI using your Claude Code login. If `ANTHROPIC_API_KEY` is set, they use the
API instead (`pip install -e ".[api]"`).

## Intent guard

```bash
autoskiller guard examples/issue175/nyaya-argument --edits examples/issue175/edits.json
```

```
[0] BLOCK   ADD: OVERRIDE: You MUST use EXACTLY these section titles: ...
      - forbids_protected (block): forbids `### 1. Пратигья (Pratijñā) — Тезис`, which overlaps protected snippet ...
      - override_language (review): says 'OVERRIDE': the edit claims to override existing instructions
      - llm_judge (block): It removes the number, Sanskrit transliteration and role from the headings ...
[1] ALLOW   ADD: When Udāharaṇa cites a text, give the work and its location ...
[2] BLOCK   ADD: Keep the whole answer under 600 characters. When space is tight, merge Upanaya into Hetu ...
      - llm_judge (block): Merging Upanaya into Hetu drops a member ... Conflicts with: "Keep all five members ..."
Decision: BLOCK  (1/3 edits allowed)
```

Edit 0 is the #175 rule. Edit 2 breaks the skill without any trigger words, so
only the LLM judge catches it. Edit 1 is a genuine improvement and passes.

It runs two layers:

1. **Deterministic checks.** Free, offline, and run on every call:
   - an edit forbids text that the skill teaches or that is protected;
   - a replace/delete removes a protected snippet;
   - "override / supersede" language;
   - the change is larger than the change budget (default 35% of the skill).
2. **LLM judge.** It reads the intent, the skill and all edits, and gives a
   verdict with the conflicting text quoted. Skip it with `--no-llm`.

An LLM "allow" never overrules a deterministic block. Exit codes are 0 allow,
1 review, 2 block, so you can gate a SkillOpt run or a CI job on it.

### Locking a skill's intent

`intent.json` sits next to `SKILL.md`. It holds a purpose, invariants, and
protected snippets that edits may not delete or forbid. A person writes it; the
optimizer never does.

```bash
autoskiller intent init path/to/my-skill   # AI draft; read and edit it
```

Without `intent.json`, the guard judges edits against the skill text alone. It
still caught both bad edits above, but protected snippets make the
deterministic layer much stronger.

### Applying changes

```bash
autoskiller apply my-skill --edits edits.json                  # only if everything is allowed
autoskiller apply my-skill --edits edits.json --only-allowed   # keep the allowed edits, drop the rest
autoskiller apply my-skill --new proposed_SKILL.md             # a full proposed file (SkillOpt-Sleep)
autoskiller history my-skill
autoskiller rollback my-skill 3
```

History lives in `<skill>/.autoskiller/history/`. Rollback restores the exact
bytes.

## Trigger testing

A skill that never loads, or loads for the wrong requests, is broken no matter
how good its body is. The agent picks skills from their name and description,
so that is what the router sees.

```bash
autoskiller triggers gen ~/.claude/skills --skill product-launch-video -n 5 -o cases.json --append
autoskiller triggers eval ~/.claude/skills cases.json
```

`gen` writes realistic should-load prompts and near misses. **Review the
labels**: they are a model's guess. Cases can list other acceptable skills:

```json
{"prompt": "make a 30s promo for our SaaS", "expect": "product-launch-video", "accept": ["hyperframes"]}
```

This is a simulation of routing, not Claude Code's exact loader. Use it to
compare description versions, not as an absolute score.

## Benchmark: SkillOpt on 8 real skills

Full method in [bench/PROTOCOL.md](bench/PROTOCOL.md), which was written
before the first run; every later change is logged there. Full results are in
[bench/RESULTS.md](bench/RESULTS.md), and every edit is listed in
[bench/edits.csv](bench/edits.csv).

- **Setup:** SkillOpt-Sleep (`main` @ `79124b3`), unmodified logic, ran 3 nights
  on each of 8 public Anthropic skills (Apache 2.0). It proposed 71 edits.
- **Ground truth:** each edit is applied alone, 4 neutral requests are run twice
  each, and a blind grader scores every answer 0–10 against the *original*
  skill. An edit is harmful if the score drops by 1 point or more.

Held-out skills (7 skills, 67 edits):

| | Result |
|---|---|
| Harmful edits | 11 / 67 (16%) |
| SkillOpt's gate accepted, harmful edits | **11 / 11** |
| SkillOpt's gate accepted, harmless edits | 23 / 56 (41%) |
| Same, stricter threshold (drop ≥ 2 points) | 6 / 6 harmful accepted |
| AutoSkiller guard v2: harmful caught | 7 / 11 (4 blocked, 3 sent to review) |
| AutoSkiller guard v2: harmless wrongly flagged | 23 / 56 (41%) |

**What it shows:** a rising validation score doesn't mean the skill still does
its job. Every harmful edit got through SkillOpt's gate.

**What it doesn't show:** that AutoSkiller solves this. Only about 23% of the
edits the guard flags are harmful, against a 16% base rate, and most false
alarms come from the "override" wording rule. The pre-registered judge (v1)
caught 10 of 11 harmful edits but flagged 73% of harmless ones.

**Caveats:**
- The 11 harmful edits come from only 5 accepted batches across 4 skills.
- Scoring the same unedited skill twice differs by about 1 point on average, the
  same size as the harm threshold.
- 30% of tasks carried user preferences that conflict with the skill, by design.
- One target model (Sonnet), one run.

Reproduce: `SKILLOPT_PATH=<SkillOpt checkout> python bench/run_all.py` (resumable).

## Earlier checks

| Run | Result | Cost |
|---|---|---|
| Guard on the #175 edits, Opus | blocked both bad edits, allowed the good one | $0.027 |
| Same, Sonnet | same verdicts | $0.014 |
| Same, no `intent.json` | same verdicts | $0.027 |
| Triggers, 4-skill demo library, Opus / Sonnet / Haiku | 14/14 on all three (the cases are too easy) | $0.05–0.13 |
| Triggers, 23 real installed video skills, 24 generated cases | 13/24 on the first labels. All 11 misses routed to `hyperframes`, whose description says it loads first and hands off. After adding it as an acceptable alternative, the same results score 24/24 | $1.13 |

Running `claude -p` naively loads your MCP servers and installed skills into
every call: about 33k extra tokens, and it would leak into trigger routing.
The CLI backend turns both off (`--strict-mcp-config --disable-slash-commands`).

## Tests

```bash
python -m unittest discover -s tests
```

The tests run offline with a fake backend.

## Next

- `triggers tune`: rewrite a description, re-run trigger cases, and keep the
  rewrite only if accuracy rises and the guard allows it.
- Cross-skill checks: flag descriptions that overlap and skills whose rules conflict.
- A SkillOpt hook that runs the guard inside the optimizer's accept step.

## License

MIT, see [LICENSE](LICENSE). The Anthropic skills in `bench/skills/` are
included unmodified under their own Apache 2.0 licenses (`LICENSE.txt` in each
folder).

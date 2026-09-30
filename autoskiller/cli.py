"""Command line interface: ``autoskiller <command>``.

Exit codes for ``guard``: 0 allow, 1 review, 2 block, so it can gate a
SkillOpt run or a CI job.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from autoskiller import guard as guard_mod
from autoskiller import intent as intent_mod
from autoskiller import triggers, versions
from autoskiller.edits import apply_edits, edits_from_diff, load_edits
from autoskiller.llm import make_backend
from autoskiller.skill import Skill, discover, read_text

EXIT = {guard_mod.ALLOW: 0, guard_mod.REVIEW: 1, guard_mod.BLOCK: 2}


def _backend(args):
    return make_backend(args.backend, args.model)


def _load_proposal(skill: Skill, args):
    if args.new:
        return edits_from_diff(skill.text, read_text(args.new))
    return load_edits(args.edits)


def _run_guard(skill: Skill, args) -> guard_mod.GuardReport:
    edits = _load_proposal(skill, args)
    intent = intent_mod.load_for(skill)
    if intent is None:
        print(f"note: no {intent_mod.INTENT_FILENAME} for {skill.name}; judging against the skill text only. "
              f"Run `autoskiller intent init {skill.dir}` to lock its intent.", file=sys.stderr)
    backend = None if args.no_llm else _backend(args)
    return guard_mod.guard(skill, edits, intent, backend, max_change_ratio=args.max_change,
                           judge_prompt=args.judge_prompt)


def cmd_guard(args) -> int:
    skill = Skill.load(args.skill)
    report = _run_guard(skill, args)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) if args.json else report.render())
    return EXIT[report.decision]


def cmd_apply(args) -> int:
    skill = Skill.load(args.skill)
    report = _run_guard(skill, args)
    print(report.render())
    print()

    if report.decision == guard_mod.ALLOW or (report.decision == guard_mod.REVIEW and args.accept_review):
        chosen = report.edits
        # A full proposed file is written as-is; an edit list is applied to the current text.
        new_text = read_text(args.new) if args.new else apply_edits(skill.text, chosen)[0]
    elif args.only_allowed and not args.new:
        chosen = report.allowed_edits()
        if not chosen:
            print("Nothing applied: no edit was allowed.")
            return EXIT[report.decision]
        new_text, _ = apply_edits(skill.text, chosen)
    else:
        hint = " Use --only-allowed to apply just the allowed edits." if not args.new else ""
        if report.decision == guard_mod.REVIEW:
            hint += " Use --accept-review after reading the flagged edits."
        print(f"Nothing applied: decision is {report.decision}.{hint}")
        return EXIT[report.decision]

    if new_text == skill.text:
        print("Nothing applied: the edits do not change the skill.")
        return 0
    note = f"applied {len(chosen)} of {len(report.edits)} edit(s); guard decision {report.decision}"
    if report.llm_used is False:
        note += " (deterministic checks only)"
    version = versions.write_version(skill, new_text, note)
    print(f"Applied {len(chosen)} edit(s) to {skill.path} as v{version.number}. "
          f"Undo with: autoskiller rollback {skill.dir} {version.number - 1}")
    return 0


def cmd_intent_init(args) -> int:
    skill = Skill.load(args.skill)
    path = intent_mod.intent_path(skill)
    if path.exists() and not args.force:
        print(f"{path} already exists. Use --force to overwrite it.", file=sys.stderr)
        return 1
    intent, dropped = intent_mod.draft_intent(skill, _backend(args))
    intent.save(path)
    print(f"Drafted {path}. Read and edit it: it is what every future edit gets checked against.")
    for d in dropped:
        print(f"  dropped a protected snippet that is not verbatim in SKILL.md: {d[:80]!r}")
    print(json.dumps(intent.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_history(args) -> int:
    skill = Skill.load(args.skill)
    found = versions.list_versions(skill)
    if not found:
        print(f"No history for {skill.name} yet.")
    for v in found:
        print(f"v{v.number:<4} {v.time}  {v.sha}  {v.note}")
    return 0


def cmd_rollback(args) -> int:
    skill = Skill.load(args.skill)
    version = versions.rollback(skill, args.version)
    print(f"Restored v{args.version} of {skill.name} (saved as v{version.number}).")
    return 0


def cmd_triggers_gen(args) -> int:
    skills = discover(args.skills_root)
    target = next((s for s in skills if s.name == args.skill), None)
    if target is None:
        print(f"No skill named {args.skill!r} under {args.skills_root}.", file=sys.stderr)
        return 1
    backend = _backend(args)
    cases = triggers.generate_cases(target, skills, backend, n=args.n)
    out = Path(args.out)
    if out.exists() and args.append:
        cases = triggers.load_cases(out) + cases
    triggers.save_cases(cases, out)
    print(f"Wrote {len(cases)} cases to {out}. Review the labels before trusting scores. Cost: ${backend.cost_usd:.4f}")
    return 0


def cmd_triggers_eval(args) -> int:
    skills = discover(args.skills_root)
    if not skills:
        print(f"No skills found under {args.skills_root} (expected <root>/<name>/SKILL.md).", file=sys.stderr)
        return 1
    report = triggers.evaluate(skills, triggers.load_cases(args.cases), _backend(args), workers=args.workers)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) if args.json else report.render())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autoskiller", description="Guard and measure agent skills.")
    p.add_argument("--backend", default="auto", choices=["auto", "cli", "api"],
                   help="auto = API if ANTHROPIC_API_KEY is set, else the local `claude` CLI")
    p.add_argument("--model", default=None, help="model for LLM calls (CLI alias like 'opus' or an API model id)")
    sub = p.add_subparsers(dest="command", required=True)

    def proposal_args(sp):
        sp.add_argument("skill", help="skill directory or SKILL.md")
        src = sp.add_mutually_exclusive_group(required=True)
        src.add_argument("--edits", help="JSON file of edits in SkillOpt format")
        src.add_argument("--new", help="a proposed full SKILL.md (e.g. SkillOpt-Sleep's proposed_SKILL.md)")
        sp.add_argument("--no-llm", action="store_true", help="deterministic checks only")
        sp.add_argument("--max-change", type=float, default=0.35, help="change budget as a fraction of the skill")
        sp.add_argument("--judge-prompt", default="v2", choices=sorted(guard_mod.JUDGE_PROMPTS),
                        help="LLM judge prompt: v2 (default) or v1, the stricter one pre-registered for the benchmark")

    sp = sub.add_parser("guard", help="check proposed edits against the skill's intent")
    proposal_args(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_guard)

    sp = sub.add_parser("apply", help="guard, then write allowed changes with version history")
    proposal_args(sp)
    sp.add_argument("--only-allowed", action="store_true", help="apply just the edits the guard allowed")
    sp.add_argument("--accept-review", action="store_true", help="also apply when the decision is review")
    sp.set_defaults(fn=cmd_apply)

    sp = sub.add_parser("intent", help="manage a skill's locked intent")
    isub = sp.add_subparsers(dest="intent_command", required=True)
    ip = isub.add_parser("init", help="draft intent.json for a skill with the model")
    ip.add_argument("skill")
    ip.add_argument("--force", action="store_true")
    ip.set_defaults(fn=cmd_intent_init)

    sp = sub.add_parser("history", help="list saved versions of a skill")
    sp.add_argument("skill")
    sp.set_defaults(fn=cmd_history)

    sp = sub.add_parser("rollback", help="restore a saved version")
    sp.add_argument("skill")
    sp.add_argument("version", type=int)
    sp.set_defaults(fn=cmd_rollback)

    sp = sub.add_parser("triggers", help="measure whether the right skill loads")
    tsub = sp.add_subparsers(dest="triggers_command", required=True)
    tg = tsub.add_parser("gen", help="generate labelled test prompts for one skill")
    tg.add_argument("skills_root")
    tg.add_argument("--skill", required=True)
    tg.add_argument("-n", type=int, default=5)
    tg.add_argument("-o", "--out", default="trigger_cases.json")
    tg.add_argument("--append", action="store_true", help="add to an existing cases file")
    tg.set_defaults(fn=cmd_triggers_gen)
    te = tsub.add_parser("eval", help="score routing accuracy on labelled prompts")
    te.add_argument("skills_root")
    te.add_argument("cases")
    te.add_argument("--workers", type=int, default=4)
    te.add_argument("--json", action="store_true")
    te.set_defaults(fn=cmd_triggers_eval)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

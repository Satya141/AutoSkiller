"""Offline tests. Run with: python -m unittest discover -s tests"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from autoskiller import guard as g
from autoskiller.edits import Edit, apply_edits, edits_from_diff, load_edits
from autoskiller.intent import Intent, load_for
from autoskiller.llm import FakeBackend
from autoskiller.skill import Skill, discover, read_text, split_frontmatter
from autoskiller import triggers, versions

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "issue175"


def _write_skill(root: Path, name: str, description: str, body: str = "Do the thing.\n") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}", encoding="utf-8")
    return d


class SkillTests(unittest.TestCase):
    def test_frontmatter_folded_description(self):
        fields, raw, body = split_frontmatter("---\nname: x\ndescription: >\n  line one\n  line two\n---\nbody\n")
        self.assertEqual(fields["description"], "line one line two")
        self.assertTrue(raw.endswith("---\n"))
        self.assertEqual(body, "body\n")

    def test_load_example(self):
        skill = Skill.load(EXAMPLE / "nyaya-argument")
        self.assertEqual(skill.name, "nyaya-argument")
        self.assertIn("Nyāya", skill.description)
        self.assertIn("### 1. Пратигья (Pratijñā) — Тезис", skill.body)


class EditTests(unittest.TestCase):
    def test_apply_add_creates_learned_section(self):
        text, noops = apply_edits("# Skill\nBody\n", [Edit("add", "Rule A"), Edit("add", "Rule A")])
        self.assertIn("## Learned preferences\n- Rule A\n", text)
        self.assertEqual(noops, [1])  # duplicate add is a no-op

    def test_apply_replace_delete_and_missing_anchor(self):
        text, noops = apply_edits("a b c", [Edit("replace", "B", "b"), Edit("delete", anchor="c"), Edit("delete", anchor="zzz")])
        self.assertEqual(text, "a B ")
        self.assertEqual(noops, [2])

    def test_edits_from_diff(self):
        edits = edits_from_diff("one\ntwo\nthree\n", "one\nTWO\nthree\nfour\n")
        self.assertEqual([e.op for e in edits], ["replace", "add"])
        self.assertEqual(edits[0].anchor, "two")
        self.assertEqual(edits[1].content, "four")

    def test_load_skillopt_format(self):
        edits = load_edits(EXAMPLE / "edits.json")
        self.assertEqual(len(edits), 3)
        self.assertTrue(all(e.op == "add" for e in edits))


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.skill = Skill.load(EXAMPLE / "nyaya-argument")
        self.intent = load_for(self.skill)
        self.edits = load_edits(EXAMPLE / "edits.json")

    def test_forbidden_fragments(self):
        self.assertEqual(g.forbidden_fragments("Do NOT use `### 1. Foo`, write `### Foo`."), ["### 1. Foo"])
        self.assertEqual(g.forbidden_fragments("Always cite `Nyāya Sūtra`."), [])

    def test_issue175_edit_blocked_offline(self):
        report = g.guard(self.skill, self.edits, self.intent, backend=None)
        self.assertEqual(report.per_edit[0], g.BLOCK)
        self.assertEqual(report.per_edit[1], g.ALLOW)
        self.assertEqual(report.decision, g.BLOCK)
        checks = {f.check for f in report.findings if f.edit == 0}
        self.assertIn("forbids_protected", checks)

    def test_issue175_blocked_without_intent_file(self):
        # With no intent.json, the skill's own text still exposes the contradiction.
        report = g.guard(self.skill, self.edits[:1], None, backend=None)
        self.assertEqual(report.per_edit[0], g.BLOCK)
        self.assertEqual(report.findings[0].check, "forbids_taught_text")

    def test_removing_protected_heading_is_blocked(self):
        edit = Edit("replace", content="### Пратигья", anchor="### 1. Пратигья (Pratijñā) — Тезис")
        report = g.guard(self.skill, [edit], self.intent, backend=None)
        checks = {f.check for f in report.findings}
        self.assertIn("removes_protected", checks)
        self.assertIn("protected_missing", checks)
        self.assertEqual(report.decision, g.BLOCK)

    def test_llm_verdicts_merge_with_deterministic(self):
        def judge(system, user, schema):
            self.assertIn("Protected snippets", user)
            return {"edits": [
                {"index": 0, "verdict": "allow", "conflicts_with": "", "reason": ""},
                {"index": 1, "verdict": "allow", "conflicts_with": "", "reason": ""},
                {"index": 2, "verdict": "block", "conflicts_with": "Keep all five members", "reason": "drops a member"},
                {"index": 9, "verdict": "block", "conflicts_with": "", "reason": "out of range, ignored"},
            ]}
        report = g.guard(self.skill, self.edits, self.intent, backend=FakeBackend(judge))
        # The LLM saying "allow" cannot overrule a deterministic block.
        self.assertEqual(report.per_edit, [g.BLOCK, g.ALLOW, g.BLOCK])
        self.assertEqual([e.content for e in report.allowed_edits()], [self.edits[1].content])
        self.assertTrue(report.llm_used)
        json.dumps(report.to_dict(), ensure_ascii=False)

    def test_judge_prompt_is_selectable(self):
        seen = []

        def judge(system, user, schema):
            seen.append(system)
            return {"edits": []}
        for version in ("v1", "v2"):
            g.guard(self.skill, self.edits[:1], self.intent, backend=FakeBackend(judge), judge_prompt=version)
        self.assertEqual(seen, [g.JUDGE_PROMPTS["v1"], g.JUDGE_PROMPTS["v2"]])
        self.assertNotEqual(seen[0], seen[1])

    def test_cli_accepts_judge_prompt(self):
        from autoskiller.cli import build_parser
        args = build_parser().parse_args(["guard", "skill", "--edits", "e.json", "--judge-prompt", "v1"])
        self.assertEqual(args.judge_prompt, "v1")
        self.assertEqual(build_parser().parse_args(["guard", "skill", "--edits", "e.json"]).judge_prompt, "v2")

    def test_override_language_needs_review(self):
        report = g.guard(self.skill, [Edit("add", "This rule supersedes the heading format.")], self.intent)
        self.assertEqual(report.decision, g.REVIEW)
        allowed = Intent(purpose="x", allow_overrides=True)
        report = g.guard(self.skill, [Edit("add", "This rule supersedes the heading format.")], allowed)
        self.assertEqual(report.decision, g.ALLOW)


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_write_and_rollback(self):
        skill = Skill.load(_write_skill(self.tmp, "demo", "Demo skill.", "original\n"))
        original = skill.text
        v = versions.write_version(skill, original + "extra\n", "add extra")
        self.assertEqual(v.number, 2)
        self.assertIn("extra", read_text(skill.path))
        versions.rollback(skill, 1)
        self.assertEqual(read_text(skill.path), original)  # byte-exact, line endings included
        self.assertEqual([x.note for x in versions.list_versions(skill)], ["before change", "add extra", "rollback to v1"])

    def test_snapshot_dedupes(self):
        skill = Skill.load(_write_skill(self.tmp, "demo", "Demo skill."))
        a = versions.snapshot(skill, "first")
        b = versions.snapshot(skill, "second")
        self.assertEqual(a.number, b.number)


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        _write_skill(self.tmp, "commit-message", "Write git commit messages.")
        _write_skill(self.tmp, "pr-summary", "Summarise pull requests.")
        self.skills = discover(self.tmp)

    def test_evaluate_scores_precision_and_recall(self):
        routes = {"write a commit msg": "commit-message", "summarise PR 12": "commit-message", "hello": "none"}

        def router(system, user, schema):
            self.assertEqual(schema["properties"]["skill"]["enum"], ["commit-message", "pr-summary", "none"])
            prompt = user.split("# User request\n", 1)[1]
            return {"skill": routes[prompt], "reason": "r"}

        cases = [triggers.Case("write a commit msg", "commit-message"),
                 triggers.Case("summarise PR 12", "pr-summary"),
                 triggers.Case("hello", None)]
        report = triggers.evaluate(self.skills, cases, FakeBackend(router), workers=1)
        self.assertAlmostEqual(report.accuracy, 2 / 3)
        cm, pr = report.per_skill["commit-message"], report.per_skill["pr-summary"]
        self.assertEqual((cm.tp, cm.fp, cm.fn), (1, 1, 0))
        self.assertEqual((pr.tp, pr.fp, pr.fn), (0, 0, 1))
        self.assertIn("Misroutes", report.render())

    def test_accept_alternatives_count_as_correct(self):
        def router(system, user, schema):
            return {"skill": "pr-summary", "reason": "router skill loads first"}
        cases = [triggers.Case("summarise and commit", "commit-message", accept=["pr-summary"])]
        report = triggers.evaluate(self.skills, cases, FakeBackend(router), workers=1)
        self.assertEqual(report.accuracy, 1.0)
        self.assertEqual(report.per_skill["pr-summary"].fp, 0)
        self.assertEqual(report.per_skill["commit-message"].fn, 0)

    def test_generate_cases_drops_mislabelled_near_misses(self):
        def gen(system, user, schema):
            return {"should_trigger": ["commit this"],
                    "near_misses": [{"prompt": "summarise PR", "expect": "pr-summary"},
                                    {"prompt": "bad", "expect": "commit-message"},
                                    {"prompt": "weather?", "expect": "none"}]}
        cases = triggers.generate_cases(self.skills[0], self.skills, FakeBackend(gen))
        self.assertEqual([(c.prompt, c.expect) for c in cases],
                         [("commit this", "commit-message"), ("summarise PR", "pr-summary"), ("weather?", None)])


if __name__ == "__main__":
    unittest.main()

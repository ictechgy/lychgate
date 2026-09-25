import argparse
import json
import os
import tempfile
import unittest

from lychgate.cli import run
from lychgate.config import ConfigError, load_registry, resolve
from lychgate.github import GitHubError

from .fixtures import NOW, pr

REGISTRY = """
defaults:
  steward: {tier: 0}
repos:
  - repo: me/tool
    steward: {status: complete, tier: 1}
  - repo: me/old
"""


class FakeGitHub:
    def __init__(self, prs, steward=None, issues=None, fail_merge=False):
        self.prs, self.steward, self.issues = prs, steward or {}, issues or {}
        self.fail_merge = fail_merge
        self.merged = []

    def file(self, repo, path):
        return self.steward.get(repo) if path == ".github/steward.yml" else None

    def dependabot_prs(self, repo):
        return self.prs.get(repo, [])

    def open_issues(self, repo):
        return self.issues.get(repo, [])

    def open_alert_count(self, repo):
        return None

    def merge(self, repo, number, sha):
        if self.fail_merge:
            raise GitHubError("head moved")
        self.merged.append((repo, number, sha))


class ConfigTest(unittest.TestCase):
    def test_steward_yml_overrides_registry(self):
        reg = load_registry(REGISTRY)
        p = resolve(reg, reg["repos"][0], "steward:\n  tier: 0\n")
        self.assertEqual(p.tier, 0)
        self.assertEqual(p.source, ["defaults", "registry", "steward.yml"])

    def test_registry_default_tier_is_observe(self):
        reg = load_registry(REGISTRY)
        self.assertFalse(resolve(reg, reg["repos"][1], None).may_merge)

    def test_major_cannot_be_enabled(self):
        with self.assertRaises(ConfigError):
            resolve({}, {"repo": "a/b"},
                    "dependencies:\n  automerge:\n    semver: [patch, major]\n")

    def test_example_file_parses(self):
        root = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(root, "steward.example.yml")) as f:
            p = resolve({}, {"repo": "a/b"}, f.read())
        self.assertTrue(p.may_merge)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.registry = os.path.join(self.dir, "registry.yml")
        with open(self.registry, "w") as f:
            f.write(REGISTRY)
        self.ledger = os.path.join(self.dir, "ledger.jsonl")
        self.report = os.path.join(self.dir, "report.md")

    def args(self, apply=False):
        return argparse.Namespace(registry=self.registry, ledger=self.ledger,
                                  report=self.report, only=None, apply=apply)

    def ledger_lines(self):
        if not os.path.exists(self.ledger):
            return []
        with open(self.ledger) as f:
            return [json.loads(l) for l in f]

    def test_dry_run_never_merges(self):
        gh = FakeGitHub({"me/tool": [pr(1)], "me/old": [pr(2)]})
        self.assertEqual(run(self.args(), gh, NOW), 0)
        self.assertEqual(gh.merged, [])
        self.assertEqual(self.ledger_lines(), [])
        with open(self.report) as f:
            text = f.read()
        self.assertIn("would merge", text)
        self.assertIn("dry-run", text)

    def test_observe_only_repo_is_summarised_not_itemised(self):
        gh = FakeGitHub({"me/old": [pr(2), pr(3, title="Bump y from 1.0.0 to 2.0.0")]})
        run(self.args(), gh, NOW)
        with open(self.report) as f:
            text = f.read()
        self.assertIn("2 open Dependabot PR(s) (1 patch, 1 major)", text)
        self.assertIn("0 need you", text)
        self.assertIn("2 observed only", text)
        self.assertNotIn("**hold**", text)

    def test_apply_merges_exact_sha_and_logs(self):
        gh = FakeGitHub({"me/tool": [pr(1)], "me/old": [pr(2)]})
        run(self.args(apply=True), gh, NOW)
        self.assertEqual(gh.merged, [("me/tool", 1, "sha1")])  # me/old is tier 0
        [entry] = self.ledger_lines()
        self.assertEqual((entry["action"], entry["pr"], entry["sha"]), ("merged", 1, "sha1"))

    def test_owner_can_revoke_from_their_repo(self):
        gh = FakeGitHub({"me/tool": [pr(1)]}, steward={"me/tool": "steward:\n  enabled: false\n"})
        self.assertEqual(run(self.args(apply=True), gh, NOW), 0)
        self.assertEqual(gh.merged, [])
        with open(self.report) as f:
            text = f.read()
        self.assertIn("Disabled by the owner", text)
        self.assertNotIn("error", text)

    def test_failed_merge_is_logged_not_fatal(self):
        gh = FakeGitHub({"me/tool": [pr(1)]}, fail_merge=True)
        run(self.args(apply=True), gh, NOW)
        [entry] = self.ledger_lines()
        self.assertEqual(entry["action"], "merge_failed")

    def test_bad_steward_yml_reports_error(self):
        gh = FakeGitHub({"me/tool": [pr(1)]}, steward={"me/tool": "steward:\n  tier: 9\n"})
        self.assertEqual(run(self.args(apply=True), gh, NOW), 1)
        self.assertEqual(gh.merged, [])

    def test_unanswered_external_issue_listed(self):
        issue = {"number": 7, "title": "English UI?", "url": "u", "createdAt": "2026-08-17T00:00:00Z",
                 "author": {"login": "stranger"}, "comments": []}
        answered = {**issue, "number": 8, "comments": [{"author": {"login": "me"}}]}
        gh = FakeGitHub({}, issues={"me/tool": [issue, answered]})
        run(self.args(), gh, NOW)
        with open(self.report) as f:
            text = f.read()
        self.assertIn("#7", text)
        self.assertNotIn("#8", text)


if __name__ == "__main__":
    unittest.main()

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
    def __init__(self, prs, steward=None, issues=None, fail_merge=False,
                 alerts=None, files=None, base=None):
        self.prs, self.steward, self.issues = prs, steward or {}, issues or {}
        self.fail_merge = fail_merge
        self.alerts = alerts or {}
        # repo -> {path: text}; dependabot.yml present with no rules firing by default
        self.files = files or {}
        self.base = base or {}
        self.merged = []

    def file(self, repo, path):
        if path == ".github/steward.yml":
            return self.steward.get(repo)
        default = {".github/dependabot.yml": "version: 2\nupdates: []\n"}
        return self.files.get(repo, default).get(path)

    def alert_summary(self, repo):
        return self.alerts.get(repo, {"state": "count", "total": 0, "crit_high": 0, "no_patch": 0})

    def base_checks(self, repo, ref):
        return self.base.get(repo, {})

    def dependabot_prs(self, repo):
        return self.prs.get(repo, [])

    def open_issues(self, repo):
        return self.issues.get(repo, [])

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

    def test_owner_merge_line_is_pinned_to_the_evaluated_head(self):
        gh = FakeGitHub({"me/tool": [pr(1, files=[{"path": ".github/workflows/ci.yml"}],
                                         headRefOid="abcdef1234567890")]})
        run(self.args(), gh, NOW)
        with open(self.report) as f:
            text = f.read()
        self.assertIn("gh pr merge 1 -R me/tool --squash --match-head-commit abcdef1234567890", text)
        self.assertIn("head abcdef1", text)

    def read_report(self):
        with open(self.report) as f:
            return f.read()

    def test_disabled_alerts_never_look_clean(self):
        gh = FakeGitHub({}, alerts={"me/tool": {"state": "disabled"}})
        run(self.args(), gh, NOW)
        text = self.read_report()
        self.assertIn("me/tool — tier 1, complete, alerts disabled", text)
        self.assertIn("Dependabot alerts are disabled", text)

    def test_exact_alert_counts_with_split(self):
        gh = FakeGitHub({}, alerts={"me/tool": {"state": "count", "total": 175,
                                                "crit_high": 115, "no_patch": 21}})
        run(self.args(), gh, NOW)
        self.assertIn("175 open alert(s)](https://github.com/me/tool/security/dependabot) "
                      "(115 critical/high, 21 without a patch)", self.read_report())

    def test_truncated_pr_list_is_flagged(self):
        prs = [pr(n, createdAt=f"2026-09-{10 + n % 10:02d}T00:00:00Z") for n in range(1, 32)]
        gh = FakeGitHub({"me/tool": prs})
        run(self.args(), gh, NOW)
        text = self.read_report()
        self.assertIn("30+ open Dependabot PRs — only the newest 30 were evaluated", text)
        self.assertNotIn("[#31]", text)  # gh lists newest first; the extra one is dropped

    def test_defaulted_status_is_flagged_and_complete_is_not_verified(self):
        # me/old declares no status: it silently defaults to complete
        gh = FakeGitHub({})
        run(self.args(), gh, NOW)
        text = self.read_report()
        self.assertIn("status `complete` is the built-in default", text)
        self.assertIn("Still installable? Not verified", text)

    def test_missing_dependabot_config_is_advised(self):
        gh = FakeGitHub({}, files={"me/tool": {}})
        run(self.args(), gh, NOW)
        self.assertIn("no `.github/dependabot.yml`", self.read_report())

    def test_red_ci_is_explained_in_the_digest(self):
        red = [{"__typename": "CheckRun", "name": "test", "status": "COMPLETED",
                "conclusion": "FAILURE"}]
        gh = FakeGitHub({"me/tool": [pr(1, statusCheckRollup=red, baseRefName="main")]},
                        base={"me/tool": {"test": {"state": "fail", "at": "2026-09-24T00:00:00Z"}}})
        run(self.args(), gh, NOW)
        self.assertIn("why red: **base-broken**", self.read_report())

    def test_repeated_explanations_collapse(self):
        red = [{"__typename": "CheckRun", "name": "test", "status": "COMPLETED",
                "conclusion": "FAILURE"}]
        prs = [pr(n, statusCheckRollup=red, baseRefName="main",
                  createdAt=f"2026-09-1{n}T00:00:00Z") for n in (1, 2)]
        gh = FakeGitHub({"me/tool": prs},
                        base={"me/tool": {"test": {"state": "fail", "at": "2026-09-24T00:00:00Z"}}})
        run(self.args(), gh, NOW)
        text = self.read_report()
        self.assertEqual(text.count("fail on `main` too"), 1)
        self.assertIn("why red: **base-broken** — same as #1", text)

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

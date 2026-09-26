import json
import subprocess
import unittest

from lychgate.github import GitHub


class FakeRun:
    """Answers `gh` invocations by matching a substring of the joined argv."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, argv, **_):
        cmd = " ".join(argv)
        self.calls.append(cmd)
        for needle, (code, out, err) in self.table.items():
            if needle in cmd:
                return subprocess.CompletedProcess(argv, code, out, err)
        raise AssertionError(f"unexpected gh call: {cmd}")


class AlertSummaryTest(unittest.TestCase):
    def test_exact_counts_across_pages(self):
        rows = "critical\ttrue\nhigh\tfalse\nmedium\ttrue\nhigh\ttrue\n"
        gh = GitHub(FakeRun({"dependabot/alerts": (0, rows, "")}))
        self.assertEqual(gh.alert_summary("me/r"),
                         {"state": "count", "total": 4, "crit_high": 3, "no_patch": 1})
        self.assertIn("--paginate", gh._run.calls[0])

    def test_disabled_by_message(self):
        gh = GitHub(FakeRun({"dependabot/alerts": (1, "", "HTTP 403: Dependabot alerts are disabled for this repository.")}))
        self.assertEqual(gh.alert_summary("me/r"), {"state": "disabled"})

    def test_disabled_by_enablement_probe_for_an_admin(self):
        gh = GitHub(FakeRun({
            "dependabot/alerts": (1, "", 'This API operation needs the "admin:repo_hook" scope'),
            "vulnerability-alerts": (1, "", "gh: Not Found (HTTP 404)"),
            "api repos/me/r --jq": (0, "true\n", ""),
        }))
        self.assertEqual(gh.alert_summary("me/r"), {"state": "disabled"})

    def test_404_without_admin_is_unknown_not_disabled(self):
        gh = GitHub(FakeRun({
            "dependabot/alerts": (1, "", "HTTP 403: Resource not accessible by integration"),
            "vulnerability-alerts": (1, "", "gh: Not Found (HTTP 404)"),
            "api repos/me/r --jq": (0, "false\n", ""),
        }))
        self.assertEqual(gh.alert_summary("me/r"), {"state": "unknown"})

    def test_unknown_when_unreadable(self):
        gh = GitHub(FakeRun({
            "dependabot/alerts": (1, "", "HTTP 403: Resource not accessible by integration"),
            "vulnerability-alerts": (1, "", "HTTP 403: Resource not accessible by integration"),
        }))
        self.assertEqual(gh.alert_summary("me/r"), {"state": "unknown"})


def rollup(*nodes):
    return json.dumps({"data": {"repository": {"object": {
        "statusCheckRollup": {"contexts": {"nodes": list(nodes)}}}}}})


def run_node(name, workflow, conclusion, at, status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
            "completedAt": at, "checkSuite": {"workflowRun": {"workflow": {"name": workflow}}}}


class BaseChecksTest(unittest.TestCase):
    def test_latest_result_per_workflow_and_name(self):
        out = rollup(
            run_node("test", "ci", "FAILURE", "2026-08-01T00:00:00Z"),
            run_node("test", "ci", "SUCCESS", "2026-08-09T00:00:00Z"),
            run_node("test", "release", "FAILURE", "2026-08-09T00:00:00Z"),
            run_node("audit", "ci", "SKIPPED", "2026-08-09T00:00:00Z"),
            run_node("slow", "ci", None, None, status="IN_PROGRESS"),
            {"__typename": "StatusContext", "context": "legacy", "state": "ERROR",
             "createdAt": "2026-09-01T00:00:00Z"},
            None,
        )
        gh = GitHub(FakeRun({"graphql": (0, out, "")}))
        base = gh.base_checks("me/r", "main")
        self.assertEqual(base[("ci", "test")], {"state": "pass", "at": "2026-08-09T00:00:00Z"})
        self.assertEqual(base[("release", "test")]["state"], "fail")
        self.assertEqual(base[("ci", "audit")]["state"], "skipped")
        self.assertEqual(base[("ci", "slow")]["state"], "pending")
        self.assertEqual(base[("", "legacy")]["state"], "fail")
        self.assertIn("r=main", gh._run.calls[0])

    def test_missing_ref_and_no_checks(self):
        missing = json.dumps({"data": {"repository": {"object": None}}})
        self.assertIsNone(GitHub(FakeRun({"graphql": (0, missing, "")})).base_checks("me/r", "gone"))
        empty = json.dumps({"data": {"repository": {"object": {"statusCheckRollup": None}}}})
        self.assertEqual(GitHub(FakeRun({"graphql": (0, empty, "")})).base_checks("me/r", "main"), {})


class PrListTest(unittest.TestCase):
    def test_asks_for_one_more_than_a_page(self):
        gh = GitHub(FakeRun({"pr list": (0, "[]", "")}))
        gh.dependabot_prs("me/r")
        self.assertIn("--limit 31", gh._run.calls[0])
        self.assertIn("headRefName", gh._run.calls[0])


if __name__ == "__main__":
    unittest.main()

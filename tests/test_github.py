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

    def test_disabled_by_enablement_probe(self):
        gh = GitHub(FakeRun({
            "dependabot/alerts": (1, "", 'This API operation needs the "admin:repo_hook" scope'),
            "vulnerability-alerts": (1, "", "gh: Not Found (HTTP 404)"),
        }))
        self.assertEqual(gh.alert_summary("me/r"), {"state": "disabled"})

    def test_unknown_when_unreadable(self):
        gh = GitHub(FakeRun({
            "dependabot/alerts": (1, "", "HTTP 403: Resource not accessible by integration"),
            "vulnerability-alerts": (1, "", "HTTP 403: Resource not accessible by integration"),
        }))
        self.assertEqual(gh.alert_summary("me/r"), {"state": "unknown"})


class BaseChecksTest(unittest.TestCase):
    def test_latest_result_per_name(self):
        runs = ("test\tcompleted\tfailure\t2026-08-01T00:00:00Z\n"
                "test\tcompleted\tsuccess\t2026-08-09T00:00:00Z\n"
                "audit\tcompleted\tskipped\t2026-08-09T00:00:00Z\n"
                "slow\tin_progress\tnull\t2026-09-25T00:00:00Z\n")
        statuses = "ci/legacy\tcompleted\tfailure\t2026-09-01T00:00:00Z\n"
        gh = GitHub(FakeRun({"check-runs": (0, runs, ""), "/status": (0, statuses, "")}))
        base = gh.base_checks("me/r", "main")
        self.assertEqual(base["test"], {"state": "pass", "at": "2026-08-09T00:00:00Z"})
        self.assertEqual(base["audit"]["state"], "skipped")
        self.assertEqual(base["slow"]["state"], "pending")
        self.assertEqual(base["ci/legacy"]["state"], "fail")


class PrListTest(unittest.TestCase):
    def test_asks_for_one_more_than_a_page(self):
        gh = GitHub(FakeRun({"pr list": (0, "[]", "")}))
        gh.dependabot_prs("me/r")
        self.assertIn("--limit 31", gh._run.calls[0])
        self.assertIn("headRefName", gh._run.calls[0])


if __name__ == "__main__":
    unittest.main()

"""Thin `gh` CLI wrapper. `gh` is preinstalled on GitHub runners and reads
GH_TOKEN, so the same code runs locally and in Actions with no HTTP client.
"""

from __future__ import annotations

import base64
import json
import subprocess
from typing import Any, Callable, Dict, List, Optional

PR_FIELDS = ",".join((
    "number", "title", "url", "author", "createdAt", "isDraft", "mergeable",
    "mergeStateStatus", "headRefOid", "headRefName", "baseRefName", "files",
    "commits", "statusCheckRollup",
))
# gh lists newest first. PRs x commits x authors must stay under GraphQL's
# 500k-node cap, so one page of 30 is all we evaluate; asking for one more
# tells us whether older PRs were cut off.
PR_PAGE = 30
ISSUE_FIELDS = "number,title,url,author,createdAt,comments,labels"


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self._run = run

    def _gh(self, *args: str) -> str:
        r = self._run(["gh", *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise GitHubError(f"gh {' '.join(args[:3])}…: {r.stderr.strip()[:300]}")
        return r.stdout

    def dependabot_prs(self, repo: str) -> List[Dict[str, Any]]:
        """Up to PR_PAGE + 1 open Dependabot PRs; more than PR_PAGE means truncated."""
        out = self._gh("pr", "list", "-R", repo, "--state", "open",
                       "--author", "app/dependabot", "--limit", str(PR_PAGE + 1),
                       "--json", PR_FIELDS)
        return json.loads(out or "[]")

    def open_issues(self, repo: str) -> List[Dict[str, Any]]:
        out = self._gh("issue", "list", "-R", repo, "--state", "open",
                       "--limit", "100", "--json", ISSUE_FIELDS)
        return json.loads(out or "[]")

    def file(self, repo: str, path: str) -> Optional[str]:
        try:
            out = self._gh("api", f"repos/{repo}/contents/{path}", "--jq", ".content")
        except GitHubError as e:
            if "404" in str(e) or "Not Found" in str(e):
                return None
            raise
        return base64.b64decode(out.strip()).decode("utf-8")

    def alert_summary(self, repo: str) -> Dict[str, Any]:
        """Open Dependabot alerts as {'state': 'count'|'disabled'|'unknown', ...}.

        'disabled' must never look like zero: a repo nobody monitors is not a
        clean repo. Counts are exact (paginated), with severity and patch split.
        """
        try:
            out = self._gh("api", "--paginate",
                           f"repos/{repo}/dependabot/alerts?state=open&per_page=100",
                           "--jq", '.[] | "\(.security_advisory.severity)\t'
                                   '\(.security_vulnerability.first_patched_version != null)"')
        except GitHubError as e:
            if "disabled" in str(e).lower():
                return {"state": "disabled"}
            return {"state": self._alerts_enablement(repo)}
        rows = [line.split("\t") for line in out.splitlines() if line.strip()]
        return {
            "state": "count",
            "total": len(rows),
            "crit_high": sum(1 for sev, _ in rows if sev in ("critical", "high")),
            "no_patch": sum(1 for _, patched in rows if patched != "true"),
        }

    def _alerts_enablement(self, repo: str) -> str:
        # The alerts API's refusal wording varies by token type; this endpoint
        # answers 204 (enabled) / 404 (disabled) when the token may ask at all.
        try:
            self._gh("api", f"repos/{repo}/vulnerability-alerts")
        except GitHubError as e:
            return "disabled" if ("404" in str(e) or "Not Found" in str(e)) else "unknown"
        return "unknown"  # enabled, but this token cannot read the alerts

    def base_checks(self, repo: str, ref: str) -> Dict[str, Dict[str, str]]:
        """Latest result per check name on `ref`'s head: {name: {state, at}}.

        state is 'pass' | 'fail' | 'pending' | 'skipped'. Uses checks:read / statuses:read
        only — no workflow logs are read, so nothing log-derived reaches the
        public digest.
        """
        runs = self._gh("api", "--paginate", f"repos/{repo}/commits/{ref}/check-runs?per_page=100",
                        "--jq", '.check_runs[] | "\(.name)\t\(.status)\t'
                                '\(.conclusion)\t\(.completed_at // .started_at)"')
        statuses = self._gh("api", f"repos/{repo}/commits/{ref}/status",
                            "--jq", '.statuses[] | "\(.context)\tcompleted\t'
                                    '\(.state)\t\(.updated_at)"')
        latest: Dict[str, Dict[str, str]] = {}
        for line in (runs + "\n" + statuses).splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            name, status, conclusion, at = parts
            if status != "completed" or conclusion in ("pending", "null", ""):
                state = "pending"
            elif conclusion in ("success", "neutral"):
                state = "pass"
            elif conclusion == "skipped":
                state = "skipped"  # did not run on base: proves nothing either way
            else:
                state = "fail"
            if name not in latest or at > latest[name]["at"]:
                latest[name] = {"state": state, "at": at}
        return latest

    def merge(self, repo: str, number: int, head_sha: str) -> None:
        # --match-head-commit: if Dependabot pushed after we evaluated, GitHub
        # refuses the merge instead of merging something we never checked.
        self._gh("pr", "merge", str(number), "-R", repo, "--squash",
                 "--match-head-commit", head_sha)

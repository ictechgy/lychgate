"""Thin `gh` CLI wrapper. `gh` is preinstalled on GitHub runners and reads
GH_TOKEN, so the same code runs locally and in Actions with no HTTP client.
"""

from __future__ import annotations

import base64
import json
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

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
# The base commit's own status rollup: latest result per check, with the
# workflow name so a `test` job in two workflows stays two checks.
_BASE_QUERY = (
    "query($o:String!,$n:String!,$r:String!){repository(owner:$o,name:$n){"
    "object(expression:$r){... on Commit{statusCheckRollup{contexts(first:100){nodes{"
    "__typename ... on CheckRun{name status conclusion completedAt startedAt "
    "checkSuite{workflowRun{workflow{name}}}} "
    "... on StatusContext{context state createdAt}}}}}}}}"
)


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
                           "--jq", r'.[] | "\(.security_advisory.severity)\t'
                                   r'\(.security_vulnerability.first_patched_version != null)"')
        except GitHubError as e:
            if "disabled" in str(e).lower():
                return {"state": "disabled"}
            return {"state": self._alerts_enablement(repo)}
        rows = [line.split("\t") for line in out.splitlines() if line.strip()]
        return {
            "state": "count",
            "total": len(rows),
            "crit_high": sum(1 for r in rows if r[0] in ("critical", "high")),
            "no_patch": sum(1 for r in rows if r[-1] != "true"),
        }

    def _alerts_enablement(self, repo: str) -> str:
        # The alerts API's refusal wording varies by token type. This endpoint
        # answers 204 (enabled) / 404 (disabled) — but it also says 404 to any
        # caller without admin, so a 404 only means "disabled" for an admin.
        try:
            self._gh("api", f"repos/{repo}/vulnerability-alerts")
            return "unknown"  # enabled, but this token cannot read the alerts
        except GitHubError as e:
            if "404" not in str(e) and "Not Found" not in str(e):
                return "unknown"
        try:
            admin = self._gh("api", f"repos/{repo}", "--jq", ".permissions.admin // false")
        except GitHubError:
            return "unknown"
        return "disabled" if admin.strip() == "true" else "unknown"

    def base_checks(self, repo: str, ref: str) -> Optional[Dict[Tuple[str, str], Dict[str, str]]]:
        """Latest result per (workflow, check name) on `ref`'s head commit.

        Returns None if the ref does not resolve, {} if it has no checks, else
        {(workflow, name): {'state': pass|fail|pending|skipped, 'at': iso}}.
        Uses the commit's status rollup (checks/statuses read only) — no
        workflow logs are read, so nothing log-derived reaches the digest.
        """
        owner, name = repo.split("/", 1)
        out = self._gh("api", "graphql", "-f", f"query={_BASE_QUERY}",
                       "-f", f"o={owner}", "-f", f"n={name}", "-f", f"r={ref}")
        commit = ((json.loads(out).get("data") or {}).get("repository") or {}).get("object")
        if not commit:
            return None
        nodes = ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []
        latest: Dict[Tuple[str, str], Dict[str, str]] = {}
        for c in nodes:
            if not c:
                continue
            if c.get("__typename") == "StatusContext":
                key = ("", c.get("context") or "?")
                raw = (c.get("state") or "").upper()
                state = "pass" if raw == "SUCCESS" else \
                    "pending" if raw in ("PENDING", "EXPECTED") else "fail"
                at = c.get("createdAt") or ""
            else:
                workflow = (((c.get("checkSuite") or {}).get("workflowRun") or {})
                            .get("workflow") or {}).get("name") or ""
                key = (workflow, c.get("name") or "?")
                conclusion = (c.get("conclusion") or "").upper()
                if (c.get("status") or "").upper() != "COMPLETED":
                    state = "pending"
                elif conclusion in ("SUCCESS", "NEUTRAL"):
                    state = "pass"
                elif conclusion == "SKIPPED":
                    state = "skipped"  # did not run on base: proves nothing either way
                else:
                    state = "fail"
                at = c.get("completedAt") or c.get("startedAt") or ""
            if key not in latest or at > latest[key]["at"]:
                latest[key] = {"state": state, "at": at}
        return latest

    def merge(self, repo: str, number: int, head_sha: str) -> None:
        # --match-head-commit: if Dependabot pushed after we evaluated, GitHub
        # refuses the merge instead of merging something we never checked.
        self._gh("pr", "merge", str(number), "-R", repo, "--squash",
                 "--match-head-commit", head_sha)

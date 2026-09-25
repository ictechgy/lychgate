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
    "mergeStateStatus", "headRefOid", "files", "commits", "statusCheckRollup",
))
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
        # 30: PRs x commits x authors must stay under GraphQL's 500k-node cap.
        out = self._gh("pr", "list", "-R", repo, "--state", "open",
                       "--author", "app/dependabot", "--limit", "30",
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

    def open_alert_count(self, repo: str) -> Optional[int]:
        """Open Dependabot alerts (first page, capped at 100); None if not readable."""
        try:
            out = self._gh("api", f"repos/{repo}/dependabot/alerts?state=open&per_page=100",
                           "--jq", "length")
        except GitHubError:
            return None
        return int(out.strip() or 0)

    def merge(self, repo: str, number: int, head_sha: str) -> None:
        # --match-head-commit: if Dependabot pushed after we evaluated, GitHub
        # refuses the merge instead of merging something we never checked.
        self._gh("pr", "merge", str(number), "-R", repo, "--squash",
                 "--match-head-commit", head_sha)

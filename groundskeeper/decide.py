"""The T1 merge decision — pure, deterministic, no LLM, no network.

Every Dependabot PR gets exactly one verdict:

* ``merge``  — every gate passed
* ``defer``  — would merge, but this run's max_per_run is spent
* ``wait``   — nothing is wrong yet (too young, CI still running,
               mergeability not computed); re-evaluated next run
* ``hold``   — needs the owner; reasons say why
* ``skip``   — not ours to touch (not Dependabot, draft)

Text from the PR (title, body) is only ever pattern-matched against fixed
regexes; nothing in it can change which gates run.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import Policy

DEPENDABOT_LOGINS = {"app/dependabot", "dependabot[bot]", "dependabot"}

# Files a routine dependency bump is allowed to touch. Anything else
# (source, Dockerfiles, workflows) means the PR is not "just a bump".
DEPENDENCY_FILES = (
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "bun.lock", "bun.lockb",
    "Cargo.toml", "Cargo.lock",
    "go.mod", "go.sum",
    "pyproject.toml", "poetry.lock", "uv.lock", "Pipfile", "Pipfile.lock",
    "requirements*.txt", "requirements*.in", "constraints*.txt",
    "Package.swift", "Package.resolved", "Podfile.lock",
    "Gemfile", "Gemfile.lock",
    "build.gradle", "build.gradle.kts", "libs.versions.toml",
    "pubspec.yaml", "pubspec.lock",
    "composer.json", "composer.lock",
)

_LEVELS = {"patch": 0, "minor": 1, "major": 2}
_META_RE = re.compile(r"update-type:\s*version-update:semver-(patch|minor|major)")
# Versions need a dot, so SHA pins ("from 6c977a6 to 02cb101") never parse.
_TITLE_RE = re.compile(r"\bfrom\s+v?(\d+\.[\w.\-+]*)\s+to\s+v?(\d+\.[\w.\-+]*)", re.I)
_OK_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_PENDING_STATES = {"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}


@dataclass
class Verdict:
    repo: str
    number: int
    title: str
    url: str
    head_sha: str
    action: str
    reasons: List[str] = field(default_factory=list)
    level: Optional[str] = None
    age_hours: float = 0.0


def _version_tuple(v: str) -> Optional[Tuple[int, ...]]:
    parts = re.match(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", v)
    if not parts:
        return None
    return tuple(int(p) if p else 0 for p in parts.groups())


def level_from_versions(old: str, new: str) -> Optional[str]:
    a, b = _version_tuple(old), _version_tuple(new)
    if a is None or b is None:
        return None
    if a[0] != b[0]:
        return "major"
    if a[1] != b[1]:
        # 0.x: a minor bump is allowed to break (semver §4) — treat as major.
        return "major" if a[0] == 0 else "minor"
    return "patch"


def semver_level(title: str, commit_bodies: List[str]) -> Optional[str]:
    """Highest bump level across Dependabot metadata and the title.

    Grouped updates list several dependencies; the riskiest one decides.
    Returns None when nothing states a level (e.g. SHA-pinned actions).
    """
    found = []
    for body in commit_bodies:
        found.extend(_META_RE.findall(body or ""))
    m = _TITLE_RE.search(title or "")
    if m:
        lv = level_from_versions(m.group(1), m.group(2))
        if lv:
            found.append(lv)
    if not found:
        return None
    return max(found, key=_LEVELS.__getitem__)


def is_dependency_file(path: str) -> bool:
    base = posixpath.basename(path)
    return any(fnmatch.fnmatchcase(base, pat) for pat in DEPENDENCY_FILES)


def check_state(rollup: List[Dict[str, Any]]) -> Tuple[str, List[str]]:
    """Collapse statusCheckRollup into ('none'|'pending'|'fail'|'pass', names)."""
    if not rollup:
        return "none", []
    failing, pending = [], []
    for c in rollup:
        name = c.get("name") or c.get("context") or "?"
        if c.get("__typename") == "StatusContext":
            state = (c.get("state") or "").upper()
            if state in _PENDING_STATES:
                pending.append(name)
            elif state != "SUCCESS":
                failing.append(name)
            continue
        if (c.get("status") or "").upper() != "COMPLETED":
            pending.append(name)
        elif (c.get("conclusion") or "").upper() not in _OK_CONCLUSIONS:
            failing.append(name)
    if failing:
        return "fail", failing
    if pending:
        return "pending", pending
    return "pass", []


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def evaluate(pr: Dict[str, Any], policy: Policy, now: datetime) -> Verdict:
    v = Verdict(
        repo=policy.repo, number=pr["number"], title=pr.get("title", ""),
        url=pr.get("url", ""), head_sha=pr.get("headRefOid", ""), action="hold",
    )
    login = (pr.get("author") or {}).get("login", "")
    if login not in DEPENDABOT_LOGINS:
        v.action, v.reasons = "skip", [f"author {login!r} is not Dependabot — never auto-merged"]
        return v
    if pr.get("isDraft"):
        v.action, v.reasons = "skip", ["draft PR"]
        return v

    hold: List[str] = []
    wait: List[str] = []

    if not policy.may_merge:
        hold.append(f"policy: tier {policy.tier}, status {policy.status} — report only")

    files = [f["path"] for f in pr.get("files") or []]
    workflow_files = [p for p in files if p.startswith(".github/workflows/")]
    other = [p for p in files if p not in workflow_files and not is_dependency_file(p)]
    if workflow_files:
        hold.append("touches .github/workflows — the App has no workflows permission; owner merges")
    if other:
        hold.append("touches non-dependency files: " + ", ".join(sorted(other)[:5]))
    if not files:
        hold.append("no changed files reported")

    bodies = [c.get("messageBody", "") for c in pr.get("commits") or []]
    v.level = semver_level(v.title, bodies)
    if v.level is None:
        hold.append("semver level unknown (no Dependabot update-type, no parsable versions)")
    elif v.level not in policy.semver:
        hold.append(f"{v.level} bump — policy allows {'/'.join(policy.semver)} only")

    if policy.require_checks:
        state, names = check_state(pr.get("statusCheckRollup") or [])
        if state == "none":
            hold.append("no CI checks — nothing verifies this bump")
        elif state == "fail":
            hold.append("CI failing: " + ", ".join(names[:5]))
        elif state == "pending":
            wait.append("CI still running: " + ", ".join(names[:5]))

    mergeable = (pr.get("mergeable") or "").upper()
    if mergeable == "CONFLICTING":
        hold.append("merge conflict — needs `@dependabot rebase` or recreate")
    elif mergeable != "MERGEABLE":
        wait.append(f"mergeability {mergeable or 'UNKNOWN'} (GitHub still computing)")

    age_h = v.age_hours = (now - _parse_ts(pr["createdAt"])).total_seconds() / 3600
    if age_h < policy.min_pr_age_hours:
        wait.append(f"cooling: {age_h:.0f}h old, needs {policy.min_pr_age_hours}h")

    if hold:
        v.action, v.reasons = "hold", hold + wait
    elif wait:
        v.action, v.reasons = "wait", wait
    else:
        v.action, v.reasons = "merge", [f"{v.level} bump, CI green, {age_h:.0f}h old"]
    return v


def plan(prs: List[Dict[str, Any]], policy: Policy,
         now: Optional[datetime] = None) -> List[Verdict]:
    """Evaluate a repo's PRs oldest-first and apply max_per_run."""
    now = now or datetime.now(timezone.utc)
    ordered = sorted(prs, key=lambda p: p["createdAt"])
    verdicts, budget = [], policy.max_per_run
    for pr in ordered:
        v = evaluate(pr, policy, now)
        if v.action == "merge":
            if budget <= 0:
                v.action = "defer"
                v.reasons = [f"eligible, but max_per_run={policy.max_per_run} reached"]
            else:
                budget -= 1
        verdicts.append(v)
    return verdicts

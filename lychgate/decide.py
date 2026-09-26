"""The T1 merge decision — pure, deterministic, no LLM, no network.

Every Dependabot PR gets exactly one verdict:

* ``merge``  — every gate passed
* ``defer``  — would merge, but this run's max_per_run is spent
* ``wait``   — nothing is wrong yet (cooling, CI still running,
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

FILES_PAGE = 100
_LEVELS = {"patch": 0, "minor": 1, "major": 2}
_META_RE = re.compile(r"update-type:\s*version-update:semver-(patch|minor|major)")
# Versions need a dot, so SHA pins ("from 6c977a6 to 02cb101") never parse.
_TITLE_RE = re.compile(r"\bfrom\s+v?(\d+\.[\w.\-+]*)\s+to\s+v?(\d+\.[\w.\-+]*)", re.I)
_BUMP_RE = re.compile(r"\bbump\s+(\S+)\s+from\s+(\S+)\s+to\s+(\S+)", re.I)
_ECOSYSTEMS = {"github_actions": "github-actions", "npm_and_yarn": "npm",
               "go_modules": "gomod", "gradle": "gradle", "maven": "maven",
               "cargo": "cargo", "pip": "pip", "uv": "uv", "bundler": "bundler",
               "swift": "swift", "pub": "pub", "composer": "composer"}
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
    # True when the only blockers are owner-judgment calls (workflows, semver,
    # tier) — CI green, mergeable, nothing broken. The digest then offers a
    # copy-paste merge command instead of just a reason.
    owner_can_merge: bool = False
    # Facts the advisor (advise.py) uses to explain red CI; not merge inputs.
    ecosystem: Optional[str] = None
    dep: Optional[str] = None
    target: Optional[str] = None
    base_ref: Optional[str] = None
    failing_checks: List[str] = field(default_factory=list)
    red_causes: List[Tuple[str, str]] = field(default_factory=list)


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
    for m in _TITLE_RE.finditer(title or ""):
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
    """Collapse statusCheckRollup into ('none'|'noop'|'pending'|'fail'|'pass', names).

    'noop' means every check was skipped or neutral: nothing actually ran,
    so nothing verified the bump. 'pass' needs at least one real SUCCESS.
    """
    if not rollup:
        return "none", []
    failing, pending, succeeded = [], [], 0
    for c in rollup:
        name = c.get("name") or c.get("context") or "?"
        if c.get("__typename") == "StatusContext":
            state = (c.get("state") or "").upper()
            if state in _PENDING_STATES:
                pending.append(name)
            elif state == "SUCCESS":
                succeeded += 1
            else:
                failing.append(name)
            continue
        conclusion = (c.get("conclusion") or "").upper()
        if (c.get("status") or "").upper() != "COMPLETED":
            pending.append(name)
        elif conclusion not in _OK_CONCLUSIONS:
            failing.append(name)
        elif conclusion == "SUCCESS":
            succeeded += 1
    if failing:
        return "fail", failing
    if pending:
        return "pending", pending
    if not succeeded:
        return "noop", []
    return "pass", []


def ecosystem_of(head_ref: str) -> Optional[str]:
    """Dependabot branches are dependabot/<ecosystem>/...; None otherwise."""
    parts = (head_ref or "").split("/")
    if len(parts) < 3 or parts[0] != "dependabot":
        return None
    return _ECOSYSTEMS.get(parts[1], parts[1])


def bump_of(title: str) -> Tuple[Optional[str], Optional[str]]:
    """(dependency, target version) from 'bump X from A to B', else (None, None)."""
    m = _BUMP_RE.search(title or "")
    return (m.group(1), m.group(3)) if m else (None, None)


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def evaluate(pr: Dict[str, Any], policy: Policy, now: datetime) -> Verdict:
    v = Verdict(
        repo=policy.repo, number=pr["number"], title=pr.get("title", ""),
        url=pr.get("url", ""), head_sha=pr.get("headRefOid", ""), action="hold",
        ecosystem=ecosystem_of(pr.get("headRefName", "")),
        base_ref=pr.get("baseRefName") or None,
    )
    v.dep, v.target = bump_of(v.title)
    login = (pr.get("author") or {}).get("login", "")
    if login not in DEPENDABOT_LOGINS:
        v.action, v.reasons = "skip", [f"author {login!r} is not Dependabot — never auto-merged"]
        return v
    if pr.get("isDraft"):
        v.action, v.reasons = "skip", ["draft PR"]
        return v

    judgment: List[str] = []  # the owner may reasonably merge anyway
    broken: List[str] = []    # something is actually wrong
    wait: List[str] = []      # re-check next run
    cooling = False

    if not policy.may_merge:
        judgment.append(f"policy: tier {policy.tier}, status {policy.status} — report only")
    if not v.head_sha:
        broken.append("no head commit SHA reported")

    raw_files = pr.get("files") or []
    files = [f["path"] for f in raw_files]
    workflow_files = [p for p in files if p.startswith(".github/workflows/")]
    other = [p for p in files if p not in workflow_files and not is_dependency_file(p)]
    if len(raw_files) >= FILES_PAGE:
        # gh returns at most one page of files; the rest could hide anything.
        broken.append(f"{len(raw_files)}+ changed files — list may be truncated")
    if workflow_files:
        judgment.append("touches .github/workflows — the App has no workflows permission; owner merges")
    if other:
        broken.append("touches non-dependency files: " + ", ".join(sorted(other)[:5]))
    if not files:
        broken.append("no changed files reported")

    commits = pr.get("commits") or []
    foreign = sorted({a.get("login") or a.get("email") or "?"
                      for c in commits for a in c.get("authors") or []
                      if (a.get("login") or "") not in DEPENDABOT_LOGINS})
    if not commits:
        broken.append("no commits reported — cannot check authorship")
    elif any(not c.get("authors") for c in commits):
        broken.append("a commit has no reported author — cannot check authorship")
    if foreign:
        broken.append("branch has non-Dependabot commits by " + ", ".join(foreign[:3]))

    v.level = semver_level(v.title, [c.get("messageBody", "") for c in commits])
    if v.level is None:
        judgment.append("semver level unknown (no Dependabot update-type, no parsable versions)")
    elif v.level not in policy.semver:
        judgment.append(f"{v.level} bump — policy allows {'/'.join(policy.semver)} only")

    if policy.require_checks:
        state, names = check_state(pr.get("statusCheckRollup") or [])
        if state == "none":
            broken.append("no CI checks — nothing verifies this bump")
        elif state == "noop":
            broken.append("no check actually ran (all skipped/neutral) — nothing verifies this bump")
        elif state == "fail":
            v.failing_checks = names
            broken.append("CI failing: " + ", ".join(names[:5]))
        elif state == "pending":
            wait.append("CI still running: " + ", ".join(names[:5]))

    mergeable = (pr.get("mergeable") or "").upper()
    merge_state = (pr.get("mergeStateStatus") or "").upper()
    if mergeable == "CONFLICTING" or merge_state == "DIRTY":
        broken.append("merge conflict — needs `@dependabot rebase` or recreate")
    elif mergeable != "MERGEABLE":
        wait.append(f"mergeability {mergeable or 'UNKNOWN'} (GitHub still computing)")
    elif merge_state == "BLOCKED":
        broken.append("blocked by branch protection (required review or check)")
    elif merge_state == "BEHIND":
        broken.append("branch is behind base — comment `@dependabot rebase`")

    # Cooling runs from the newest content, not PR creation: Dependabot
    # rewrites PRs in place when a newer version ships.
    created = _parse_ts(pr["createdAt"])
    v.age_hours = (now - created).total_seconds() / 3600
    head_times = [_parse_ts(c["committedDate"]) for c in commits if c.get("committedDate")]
    newest = max([created] + head_times)
    age_h = (now - newest).total_seconds() / 3600
    if age_h < policy.min_pr_age_hours:
        cooling = True
        what = ("head rewritten" if (newest - created).total_seconds() > 60 else "opened")
        wait.append(f"cooling: {what} {age_h:.0f}h ago, needs {policy.min_pr_age_hours}h")

    hold = judgment + broken
    if hold:
        v.action, v.reasons = "hold", hold + wait
        v.owner_can_merge = not broken and len(wait) == int(cooling)
    elif wait:
        v.action, v.reasons = "wait", wait
    else:
        v.action, v.reasons = "merge", [f"{v.level} bump, CI green, head {age_h:.0f}h old"]
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

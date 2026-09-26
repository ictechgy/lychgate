"""lychgate run [--apply] — evaluate every registered repo, optionally
merge what passed, append to the ledger, write the digest.

Dry-run is the default. Only `--apply` ever calls a mutating API, and the
only mutation in tier 1 is squash-merging a Dependabot PR at the exact
head commit that was evaluated.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import __version__
from .config import ConfigError, load_registry, resolve
from . import advise
from .decide import plan
from .github import PR_PAGE, GitHub, GitHubError
from .report import render, waiting_issues

STEWARD_PATHS = (".github/steward.yml", "steward.yml")
DEPENDABOT_PATHS = (".github/dependabot.yml", ".github/dependabot.yaml")


def _first_file(gh: GitHub, repo: str, paths: tuple) -> Optional[str]:
    for path in paths:
        text = gh.file(repo, path)
        if text is not None:
            return text
    return None


def _advise(gh: GitHub, repo: str, verdicts: list, now: datetime) -> List[str]:
    """Advice for repos lychgate acts on. Advice is never a merge input, so
    nothing that goes wrong here may stop the run or hide the digest."""
    notes: List[str] = []

    def has_file(path: str) -> bool:
        try:
            return gh.file(repo, path) is not None
        except GitHubError as e:
            notes.append(f"could not check `{path}`: {e}")
            return False

    try:
        try:
            updates, problem = advise.parse_dependabot(_first_file(gh, repo, DEPENDABOT_PATHS))
        except GitHubError as e:
            updates, problem = [], f"could not read `.github/dependabot.yml`: {e}"
        structural = advise.structural_causes(updates, has_file)
        red = [v for v in verdicts if v.failing_checks]
        if red:
            bases = {}
            for ref in {v.base_ref for v in red if v.base_ref}:
                try:
                    bases[ref] = gh.base_checks(repo, ref)
                except GitHubError:
                    bases[ref] = None
            advise.classify_red(verdicts, bases, now, structural)
        return advise.lint_dependabot(updates, problem, verdicts, structural) + notes
    except Exception as e:  # noqa: BLE001 — see docstring
        return notes + [f"advisor failed ({type(e).__name__}) — advice skipped this run"]


def _ledger_append(path: str, entry: Dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def run(args: argparse.Namespace, gh: Optional[GitHub] = None,
        now: Optional[datetime] = None) -> int:
    gh = gh or GitHub()
    now = now or datetime.now(timezone.utc)
    with open(args.registry, encoding="utf-8") as f:
        registry = load_registry(f.read())

    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    results: List[Dict[str, Any]] = []
    exit_code = 0

    for entry in registry["repos"]:
        repo = entry["repo"]
        if args.only and repo not in args.only:
            continue
        result: Dict[str, Any] = {"verdicts": [], "issues": [], "alerts": None,
                                  "advice": [], "warnings": []}
        try:
            policy = resolve(registry, entry, _first_file(gh, repo, STEWARD_PATHS))
            result["policy"] = policy
            if not policy.enabled:
                result["note"] = "Disabled by the owner (steward.enabled: false)."
                results.append(result)
                continue
            prs = gh.dependabot_prs(repo)
            if len(prs) > PR_PAGE:
                prs = prs[:PR_PAGE]
                result["warnings"].append(
                    f"{PR_PAGE}+ open Dependabot PRs — only the newest {PR_PAGE} were evaluated; "
                    f"group updates in `.github/dependabot.yml` to cut the pile")
            if not policy.status_explicit:
                result["warnings"].append(
                    f"status `{policy.status}` is the built-in default — confirm it with "
                    f"`steward: {{status: ...}}` in registry.yml or the repo's steward.yml")
            result["verdicts"] = plan(prs, policy, now)
            owner = repo.split("/")[0]
            result["issues"] = waiting_issues(gh.open_issues(repo), owner,
                                              policy.respond_after_days, now)
            result["alerts"] = gh.alert_summary(repo)
            if policy.may_merge:
                result["advice"] = _advise(gh, repo, result["verdicts"], now)
                if result["alerts"].get("state") == "disabled":
                    result["warnings"].append(
                        "Dependabot alerts are disabled — nobody is told about vulnerable "
                        "dependencies here. Enable them in Settings → Advanced Security "
                        "(lychgate cannot: that needs administration)")
        except (ConfigError, GitHubError) as e:
            result.setdefault("policy", resolve({}, {"repo": repo}, None))
            result["error"] = str(e)
            exit_code = 1
            results.append(result)
            continue

        for v in result["verdicts"]:
            if v.action != "merge" or not args.apply:
                continue
            entry_log = {"ts": now.isoformat(), "run": run_id, "repo": repo,
                         "pr": v.number, "sha": v.head_sha, "level": v.level,
                         "title": v.title}
            # Also printed, so the Actions log backs up the ledger if the
            # later commit/push step fails.
            print(json.dumps({**entry_log, "action": "merging"}), file=sys.stderr)
            try:
                gh.merge(repo, v.number, v.head_sha)
                _ledger_append(args.ledger, {**entry_log, "action": "merged",
                                             "reasons": v.reasons})
            except GitHubError as e:
                v.action, v.reasons = "error", [f"merge failed: {e}"]
                _ledger_append(args.ledger, {**entry_log, "action": "merge_failed",
                                             "reasons": v.reasons})
        results.append(result)

    text = render(results, applied=args.apply, now=now)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return exit_code


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="lychgate",
                                 description="Deterministic custodian for repos you stopped tending.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="evaluate registered repos (dry-run unless --apply)")
    r.add_argument("--registry", default="registry.yml")
    r.add_argument("--ledger", default="ledger.jsonl")
    r.add_argument("--report", help="write the markdown digest here instead of stdout")
    r.add_argument("--only", action="append", help="limit to owner/name (repeatable)")
    r.add_argument("--apply", action="store_true", help="actually merge eligible PRs")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except (ConfigError, OSError) as e:
        print(f"lychgate: {e}", file=sys.stderr)
        return 2

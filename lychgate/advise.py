"""Explain held PRs and the Dependabot setup behind them — advice only.

Nothing here feeds a merge decision. Every label comes from fixed rules over
GitHub metadata (check names and results on the PR and on its base branch,
PR titles and branches, dependabot.yml). No workflow logs are read, so no
log text from any repo can reach the public digest, and every suggested
command is a fixed template.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import yamlio
from .decide import Verdict

STALE_DAYS = 30
_GROUP_RE_PREFIX = "bump the "


def _ts(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _names(names: List[str], limit: int = 3) -> str:
    shown = ", ".join(f"`{n}`" for n in names[:limit])
    return shown + (f" (+{len(names) - limit})" if len(names) > limit else "")


def _prs(vs: List[Verdict]) -> str:
    return ", ".join(f"#{v.number}" for v in sorted(vs, key=lambda v: v.number))


def classify_red(verdicts: List[Verdict], base: Optional[Dict[str, Dict[str, str]]],
                 now: datetime) -> None:
    """Attach (label, explanation) causes to every PR whose CI is failing.

    Labels, in order, and all that apply:
      base-broken          the same check fails on the base branch too
      sibling-split        parts of one upstream bumped to one version in
                           separate PRs, all red (e.g. codeql init/analyze)
      repo-rejects-<eco>   the same check fails on 2+ independent <eco> PRs
                           (a split sibling group counts once) but passes on
                           base: the repo's own CI refuses this kind of bump
      base-stale           those checks have not passed on base for 30+ days
                           (or never ran there), so "base is green" is old news
      bump-failure         none of the above: probably this bump itself
    """
    red = [v for v in verdicts if v.failing_checks]
    if not red:
        return

    siblings: Dict[Tuple[Any, ...], List[Verdict]] = defaultdict(list)
    for v in red:
        if v.dep and v.target and v.dep.count("/") >= 2:
            siblings[(v.ecosystem, v.dep.rsplit("/", 1)[0], v.target)].append(v)
    sibling_of = {v.number: (key, vs) for key, vs in siblings.items() if len(vs) >= 2
                  for v in vs}

    failing_by_job: Dict[Tuple[Optional[str], str], List[Verdict]] = defaultdict(list)
    for v in red:
        for name in v.failing_checks:
            failing_by_job[(v.ecosystem, name)].append(v)

    def independent_failures(eco: Optional[str], name: str) -> int:
        # A split sibling group counts once: its members fail together
        # because of the split, not because the repo rejects the bump.
        units = {sibling_of[p.number][0] if p.number in sibling_of else p.number
                 for p in failing_by_job[(eco, name)]}
        return len(units)

    base_ref = next((v.base_ref for v in red if v.base_ref), "the base branch")

    def base_state(name: str) -> Optional[str]:
        return (base or {}).get(name, {}).get("state")

    for v in red:
        causes: List[Tuple[str, str]] = []
        if base is not None:
            broken = [n for n in v.failing_checks if base_state(n) == "fail"]
            if broken:
                causes.append(("base-broken",
                               f"{_names(broken)} fail on `{base_ref}` too — fix `{base_ref}` first: "
                               f"`gh run list -R {v.repo} --branch {base_ref} --limit 5`"))

        if v.number in sibling_of:
            (eco, prefix, target), vs = sibling_of[v.number]
            group = prefix.rsplit("/", 1)[-1]
            causes.append(("sibling-split",
                           f"`{prefix}` parts bumped to {target} in separate PRs ({_prs(vs)}) — "
                           f"each is red until the others land. Group them in `.github/dependabot.yml` "
                           f"under the {eco or 'same'} entry: "
                           f"`groups: {{{group}: {{patterns: [\"{prefix}*\"]}}}}`"))

        if base is not None and v.ecosystem:
            rejects = [n for n in v.failing_checks if base_state(n) == "pass"
                       and independent_failures(v.ecosystem, n) >= 2]
            if rejects:
                others = {p.number: p for n in rejects for p in failing_by_job[(v.ecosystem, n)]}
                causes.append((f"repo-rejects-{v.ecosystem}",
                               f"{_names(rejects)} fail on every {v.ecosystem} PR "
                               f"({_prs(list(others.values()))}) but pass on `{base_ref}` — the repo's "
                               f"own CI refuses these bumps. Fix it once in the repo, or stop the "
                               f"stream (`ignore` it in `.github/dependabot.yml`)"))

        if base is not None:
            stale, never = [], []
            for n in v.failing_checks:
                info = (base or {}).get(n)
                if not info or info["state"] == "skipped":
                    never.append(n)
                elif info["state"] == "pass":
                    at = _ts(info["at"])
                    if at and (now - at).days >= STALE_DAYS:
                        stale.append((n, (now - at).days))
            if stale or never:
                bits = []
                if stale:
                    oldest = max(d for _, d in stale)
                    bits.append(f"{_names([n for n, _ in stale])} last passed on `{base_ref}` "
                                f"{oldest}d ago")
                if never:
                    bits.append(f"{_names(never)} never ran on `{base_ref}`'s head")
                causes.append(("base-stale",
                               "; ".join(bits) + f" — re-run CI on `{base_ref}` before blaming "
                               f"the bump: `gh run list -R {v.repo} --branch {base_ref} --limit 5`"))

        if not causes:
            if base is None:
                causes.append(("unclassified", "base-branch checks unavailable this run"))
            else:
                causes.append(("bump-failure", "checks pass on the base branch and nothing "
                               "structural explains it — likely this bump itself; see the PR's checks"))
        v.red_causes = causes


def _group_of(title: str) -> Optional[str]:
    low = (title or "").lower()
    i = low.find(_GROUP_RE_PREFIX)
    if i < 0 or " group" not in low[i:]:
        return None
    rest = title[i + len(_GROUP_RE_PREFIX):]
    return rest.split(" group", 1)[0].strip() or None


def lint_dependabot(text: Optional[str], verdicts: List[Verdict],
                    has_file: Callable[[str], bool]) -> List[str]:
    """Evidence-backed suggestions for .github/dependabot.yml (read-only).

    Only rules that the repo's open PRs or files actually trigger fire, so a
    healthy repo gets no advice.
    """
    if text is None:
        return ["no `.github/dependabot.yml` — Dependabot opens no version-update PRs here, "
                "so lychgate has nothing to act on"]
    try:
        cfg = yamlio.parse(text) or {}
    except yamlio.YAMLError as e:
        return [f"`.github/dependabot.yml` uses YAML lychgate cannot parse ({e}) — lint skipped"]
    updates = [u for u in (cfg.get("updates") or []) if isinstance(u, dict)] \
        if isinstance(cfg, dict) else []
    by_eco = defaultdict(list)
    for u in updates:
        by_eco[str(u.get("package-ecosystem", ""))].append(u)

    advice: List[str] = []
    open_by_eco = defaultdict(list)
    for v in verdicts:
        open_by_eco[v.ecosystem].append(v)

    actions_prs = [v for v in open_by_eco.get("github-actions", []) if not _group_of(v.title)]
    ungrouped = [u for u in by_eco.get("github-actions", []) if not u.get("groups")]
    if ungrouped and len(actions_prs) >= 2:
        advice.append(f"github-actions updates arrive one PR per action ({_prs(actions_prs)}) — "
                      f"group them under the github-actions entry: "
                      f"`groups: {{actions: {{patterns: [\"*\"]}}}}`")

    if by_eco.get("gradle") and has_file("gradle/verification-metadata.xml"):
        advice.append("gradle dependency verification is on and Dependabot does not update "
                      "`gradle/verification-metadata.xml`, so every gradle bump fails CI here "
                      "and needs you: regenerate with "
                      "`./gradlew --write-verification-metadata sha256 help` on the PR branch, "
                      "or accept gradle bumps as always-manual")

    for v in verdicts:
        group = _group_of(v.title)
        if not group or v.level != "major":
            continue
        for u in updates:
            spec = (u.get("groups") or {}).get(group)
            if isinstance(spec, dict) and not spec.get("update-types"):
                advice.append(f"group `{group}` mixes major with minor/patch "
                              f"([#{v.number}]({v.url}) is major) — add "
                              f"`update-types: [minor, patch]` to it so safe updates arrive "
                              f"separately from majors")
                break
    return advice

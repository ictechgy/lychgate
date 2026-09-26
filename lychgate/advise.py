"""Explain held PRs and the Dependabot setup behind them — advice only.

Nothing here feeds a merge decision. Every label comes from fixed rules over
GitHub metadata (check results on the PR and on its base branch's head, PR
titles and branches, dependabot.yml, file existence). No workflow logs are
read, so no log text can reach the public digest, and every suggested
command is a fixed template.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import yamlio
from .decide import Verdict

STALE_DAYS = 30
Key = Tuple[str, str]  # (workflow, check name)
Base = Optional[Dict[Key, Dict[str, str]]]


def _ts(s: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _code(text: str) -> str:
    # Names come from repo config; keep a stray backtick from breaking markup.
    return "`" + text.replace("`", "'") + "`"


def _names(keys: List[Key], limit: int = 3) -> str:
    shown = ", ".join(_code(name) for _, name in keys[:limit])
    return shown + (f" (+{len(keys) - limit})" if len(keys) > limit else "")


def _prs(vs: List[Verdict]) -> str:
    return ", ".join(f"#{n}" for n in sorted({v.number for v in vs}))


# -- dependabot.yml ---------------------------------------------------------

def parse_dependabot(text: Optional[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """(update entries, problem). A problem string means: say so, lint nothing."""
    if text is None:
        return [], ("no `.github/dependabot.yml` — Dependabot opens no version-update PRs "
                    "here, so lychgate has nothing to act on")
    try:
        cfg = yamlio.parse(text)
    except yamlio.YAMLError as e:
        return [], f"`.github/dependabot.yml` uses YAML lychgate cannot parse ({e}) — lint skipped"
    raw = cfg.get("updates") if isinstance(cfg, dict) else None
    if not isinstance(raw, list):
        return [], "`.github/dependabot.yml` has an unrecognised shape (`updates` is not a list) — lint skipped"
    return [u for u in raw if isinstance(u, dict)], None


def _groups(entry: Dict[str, Any]) -> Dict[str, Any]:
    g = entry.get("groups")
    return g if isinstance(g, dict) else {}


def structural_causes(updates: List[Dict[str, Any]],
                      has_file: Callable[[str], bool]) -> Dict[str, Tuple[str, str]]:
    """Repo-level facts that make every PR of an ecosystem fail: {eco: (label, why)}."""
    causes: Dict[str, Tuple[str, str]] = {}
    if any(u.get("package-ecosystem") == "gradle" for u in updates) \
            and has_file("gradle/verification-metadata.xml"):
        causes["gradle"] = (
            "gradle-verification",
            "this repo verifies gradle dependencies (`gradle/verification-metadata.xml`) and "
            "Dependabot does not update that file, so gradle bumps that change dependencies fail "
            "CI here — regenerate it on the PR branch: "
            "`./gradlew --write-verification-metadata sha256 help`")
    return causes


def _group_of(title: str) -> Optional[str]:
    low = (title or "").lower()
    i = low.find("bump the ")
    if i < 0 or " group" not in low[i:]:
        return None
    return title[i + len("bump the "):].split(" group", 1)[0].strip() or None


def lint_dependabot(updates: List[Dict[str, Any]], problem: Optional[str],
                    verdicts: List[Verdict], structural: Dict[str, Tuple[str, str]]) -> List[str]:
    """Evidence-backed suggestions for .github/dependabot.yml (read-only).

    Only rules that the repo's open PRs or files actually trigger fire, so a
    healthy repo gets no advice.
    """
    if problem:
        return [problem]
    advice: List[str] = []

    actions_prs = [v for v in verdicts if v.ecosystem == "github-actions" and not _group_of(v.title)]
    actions_entries = [u for u in updates if u.get("package-ecosystem") == "github-actions"]
    if actions_entries and len(actions_prs) >= 2 and not any(_groups(u) for u in actions_entries):
        advice.append(f"github-actions updates arrive one PR per action ({_prs(actions_prs)}) — "
                      f"group them under the github-actions entry: "
                      f"`groups: {{actions: {{patterns: [\"*\"]}}}}`")

    if "gradle" in structural:
        open_gradle = [v for v in verdicts if v.ecosystem == "gradle"]
        if open_gradle:  # the PR lines already carry the full explanation
            advice.append(f"gradle bumps need you here until `gradle/verification-metadata.xml` "
                          f"is regenerated on each PR ({_prs(open_gradle)}) — or accept them as "
                          f"always-manual")
        else:
            advice.append(structural["gradle"][1] + ", or accept gradle bumps as always-manual")

    for v in verdicts:
        group = _group_of(v.title)
        if not group or v.level != "major":
            continue
        for u in updates:
            spec = _groups(u).get(group)
            if isinstance(spec, dict) and not spec.get("update-types"):
                advice.append(f"group {_code(group)} mixes major with minor/patch "
                              f"([#{v.number}]({v.url}) is major) — add "
                              f"`update-types: [minor, patch]` to it so safe updates arrive "
                              f"separately from majors")
                break
    return advice


# -- why is CI red? ---------------------------------------------------------

def classify_red(verdicts: List[Verdict], bases: Dict[Optional[str], Base], now: datetime,
                 structural: Optional[Dict[str, Tuple[str, str]]] = None) -> None:
    """Attach (label, explanation) causes to every PR whose CI is failing.

    Each PR is judged against its own base branch (`bases[v.base_ref]`).
    Labels, in order, all that apply:
      base-broken          the same check fails on the base head too
      sibling-split        parts of one upstream bumped to one version in
                           separate PRs, all red (e.g. codeql init/analyze)
      <structural>         a repo fact that fails every PR of this ecosystem
      repo-rejects-<eco>   the check fails on every <eco> PR that ran it (2+
                           independent ones) yet passed on base in the last
                           30 days: the repo's own CI refuses this kind of bump
      bump-failure         a failing check passed on base recently and nothing
                           above covers it: probably this bump itself
      base-stale           the check's last pass on base is 30+ days old
      no-base-signal       the check does not run on the base head (PR-only
                           or skipped): nothing to compare against
      unclassified         base result pending / unavailable
    """
    structural = structural or {}
    red = [v for v in verdicts if v.failing_checks]
    if not red:
        return

    siblings: Dict[Tuple[Any, ...], List[Verdict]] = defaultdict(list)
    for v in red:
        if v.dep and v.target and v.dep.count("/") >= 2:
            siblings[(v.base_ref, v.ecosystem, v.dep.rsplit("/", 1)[0], v.target)].append(v)
    sibling_of = {v.number: (key, vs) for key, vs in siblings.items() if len(vs) >= 2
                  for v in vs}

    failed_on: Dict[Tuple[Any, ...], List[Verdict]] = defaultdict(list)
    passed_on: Dict[Tuple[Any, ...], List[Verdict]] = defaultdict(list)
    for v in verdicts:
        for k in v.failing_checks:
            failed_on[(v.base_ref, v.ecosystem, k)].append(v)
        for k in v.passing_checks:
            passed_on[(v.base_ref, v.ecosystem, k)].append(v)

    def independent_failures(scope: Tuple[Any, ...]) -> int:
        # A split sibling group counts once: its members fail together
        # because of the split, not because the repo rejects the bump.
        return len({sibling_of[p.number][0] if p.number in sibling_of else p.number
                    for p in failed_on[scope]})

    for v in red:
        ref = v.base_ref or "the base branch"
        base = bases.get(v.base_ref) if v.base_ref else None
        causes: List[Tuple[str, str]] = []
        covered: set = set()

        def state(k: Key) -> Optional[str]:
            return (base or {}).get(k, {}).get("state")

        def age_days(k: Key) -> Optional[int]:
            at = _ts((base or {}).get(k, {}).get("at"))
            return (now - at).days if at else None

        def fresh_pass(k: Key) -> bool:
            d = age_days(k)
            return state(k) == "pass" and d is not None and d < STALE_DAYS

        if base:
            broken = [k for k in v.failing_checks if state(k) == "fail"]
            if broken:
                covered.update(broken)
                causes.append(("base-broken",
                               f"{_names(broken)} fail on `{ref}` too — fix `{ref}` first: "
                               f"`gh run list -R {v.repo} --branch {ref} --limit 5`"))

        if v.number in sibling_of:
            (_, eco, prefix, target), vs = sibling_of[v.number]
            # The split explains checks that fail only inside the group; a
            # check that also fails elsewhere still needs its own label.
            group = {p.number for p in vs}
            covered.update(k for k in v.failing_checks
                           if {p.number for p in failed_on[(v.base_ref, v.ecosystem, k)]} <= group)
            causes.append(("sibling-split",
                           f"{_code(prefix)} parts bumped to {target} in separate PRs ({_prs(vs)}) — "
                           f"each is red until the others land. Group them in "
                           f"`.github/dependabot.yml` under the {eco or 'same'} entry: "
                           f"`groups: {{{prefix.rsplit('/', 1)[-1]}: {{patterns: [\"{prefix}*\"]}}}}`"))

        if v.ecosystem in structural:
            covered.update(v.failing_checks)
            causes.append(structural[v.ecosystem])

        if base and v.ecosystem:
            rejects = [k for k in v.failing_checks if k not in covered and fresh_pass(k)
                       and independent_failures((v.base_ref, v.ecosystem, k)) >= 2
                       and not passed_on[(v.base_ref, v.ecosystem, k)]]
            if rejects:
                covered.update(rejects)
                who = {p.number: p for k in rejects for p in failed_on[(v.base_ref, v.ecosystem, k)]}
                causes.append((f"repo-rejects-{v.ecosystem}",
                               f"{_names(rejects)} fail on all {len(who)} {v.ecosystem} PRs that "
                               f"ran them ({_prs(list(who.values()))}) but passed on `{ref}` "
                               f"recently — the repo's own CI refuses these bumps. Fix it once in "
                               f"the repo, or stop the stream (`ignore` it in "
                               f"`.github/dependabot.yml`)"))

        if base:
            genuine = [k for k in v.failing_checks if k not in covered and fresh_pass(k)]
            if genuine:
                causes.append(("bump-failure",
                               f"{_names(genuine)} passed on `{ref}` recently and no known "
                               f"structural cause applies — likely this bump itself; see the PR's checks"))

            stale = [(k, age_days(k)) for k in v.failing_checks
                     if k not in covered and state(k) == "pass" and not fresh_pass(k)]
            if stale:
                ages = [d for _, d in stale if d is not None]
                when = f"{max(ages)}d ago" if ages else "at an unknown time"
                causes.append(("base-stale",
                               f"{_names([k for k, _ in stale])} last passed on `{ref}` {when} — "
                               f"re-run CI on `{ref}` before blaming the bump: "
                               f"`gh run list -R {v.repo} --branch {ref} --limit 5`"))

            unseen = [k for k in v.failing_checks if k not in covered and state(k) in (None, "skipped")]
            if unseen:
                causes.append(("no-base-signal",
                               f"{_names(unseen)} did not run on `{ref}`'s head (PR-only or "
                               f"skipped), so there is no base result to compare against"))

            pending = [k for k in v.failing_checks if k not in covered and state(k) == "pending"]
            if pending:
                causes.append(("unclassified",
                               f"{_names(pending)} still running on `{ref}` — re-check once they finish"))
        elif base == {}:
            if not causes:
                causes.append(("unclassified",
                               f"no check results on `{ref}`'s head (CI may run on pull requests "
                               f"only), so red CI cannot be compared with the base"))
        elif not causes:
            causes.append(("unclassified", f"`{ref}` checks unavailable this run"))
        v.red_causes = causes

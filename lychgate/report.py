"""Markdown digest: what was merged, what waits, what needs the owner."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import Policy
from .decide import Verdict

_ICON = {"merge": "✅", "defer": "⏭️", "wait": "⏳", "hold": "✋", "skip": "·", "error": "⚠️"}


def waiting_issues(issues: List[Dict[str, Any]], owner: str, days: int,
                   now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """External issues the owner has never answered, older than `days`."""
    now = now or datetime.now(timezone.utc)
    out = []
    for i in issues:
        author = (i.get("author") or {}).get("login", "")
        if author == owner or (i.get("author") or {}).get("is_bot"):
            continue
        if any((c.get("author") or {}).get("login") == owner for c in i.get("comments") or []):
            continue
        age = (now - datetime.fromisoformat(i["createdAt"].replace("Z", "+00:00"))).days
        if age >= days:
            out.append({**i, "age_days": age})
    return out


def _acting(r: Dict[str, Any]) -> bool:
    return "error" not in r and "note" not in r and r["policy"].may_merge


def _observed_summary(verdicts: List[Verdict], p: Policy) -> str:
    by_level: Dict[str, int] = {}
    for v in verdicts:
        key = v.level or "unknown"
        by_level[key] = by_level.get(key, 0) + 1
    mix = ", ".join(f"{by_level[k]} {k}" for k in ("patch", "minor", "major", "unknown")
                    if k in by_level)
    oldest = max(verdicts, key=lambda v: v.age_hours)
    return (f"{len(verdicts)} open Dependabot PR(s) ({mix}); oldest "
            f"[#{oldest.number}]({oldest.url}) is {oldest.age_hours / 24:.0f}d old — "
            f"report only (tier {p.tier}, {p.status})")


def _alerts_clause(alerts: Optional[Dict[str, Any]], repo: str) -> str:
    if not alerts:
        return ""
    state = alerts.get("state")
    if state == "disabled":
        return ", alerts disabled"
    if state != "count":
        return ", alerts unknown"
    total = alerts["total"]
    if not total:
        return ", 0 open alerts"
    return (f", [{total} open alert(s)](https://github.com/{repo}/security/dependabot) "
            f"({alerts['crit_high']} critical/high, {alerts['no_patch']} without a patch)")


def render(results: List[Dict[str, Any]], applied: bool,
           now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    mode = "apply" if applied else "dry-run"
    lines = [f"# Lychgate digest — {now:%Y-%m-%d} ({mode})", ""]
    counts: Dict[str, int] = {}
    observed = 0
    for r in results:
        if not _acting(r):
            observed += len(r["verdicts"])
            continue
        for v in r["verdicts"]:
            counts[v.action] = counts.get(v.action, 0) + 1
    errors = sum(1 for r in results if r.get("error")) + counts.get("error", 0)
    merged_word = "merged" if applied else "would merge"
    lines.append(
        f"{counts.get('merge', 0)} {merged_word} · {counts.get('wait', 0)} waiting · "
        f"{counts.get('hold', 0)} need you · "
        f"{sum(len(r['issues']) for r in results)} unanswered issue(s)"
        + (f" · {observed} observed only" if observed else "")
        + (f" · {errors} error(s)" if errors else ""))
    lines.append("")
    for r in results:
        p: Policy = r["policy"]
        head = f"## {p.repo} — tier {p.tier}, {p.status}" + _alerts_clause(r.get("alerts"), p.repo)
        lines += [head, ""]
        if r.get("error"):
            lines += [f"⚠️ {r['error']}", ""]
            continue
        if r.get("note"):
            lines += [r["note"], ""]
            continue
        for w in r.get("warnings", []):
            lines.append(f"- ⚠️ {w}")
        if not r["verdicts"] and not r["issues"] and not r.get("advice"):
            if r.get("warnings"):
                lines.append("")  # else markdown folds the next line into the last bullet
            if p.status == "complete":
                # Nothing pending is not the same as verified: no smoke check
                # has installed or run this repo yet.
                lines += ["Nothing pending. Still installable? Not verified — no smoke check yet.", ""]
            else:
                lines += ["Nothing pending.", ""]
            continue
        if not _acting(r) and r["verdicts"]:
            lines.append("- 👁️ " + _observed_summary(r["verdicts"], p))
            r = {**r, "verdicts": []}
        explained: Dict[Tuple[str, str], int] = {}
        for v in r["verdicts"]:
            label = v.action if applied or v.action != "merge" else "would merge"
            lvl = f" [{v.level}]" if v.level else ""
            lines.append(f"- {_ICON.get(v.action, '?')} **{label}** [#{v.number}]({v.url}) {v.title}{lvl}")
            for reason in v.reasons:
                lines.append(f"  - {reason}")
            for cause, why in v.red_causes:
                first = explained.setdefault((cause, why), v.number)
                shown = why if first == v.number else f"same as #{first}"
                lines.append(f"  - why red: **{cause}** — {shown}")
            if v.owner_can_merge:
                # Pinned: if Dependabot rewrites the PR before you paste this,
                # GitHub refuses instead of merging something unreviewed.
                lines.append(f"  - your call — CI green, mergeable at head {v.head_sha[:7]}: "
                             f"`gh pr merge {v.number} -R {v.repo} --squash "
                             f"--match-head-commit {v.head_sha}`")
        for a in r.get("advice", []):
            lines.append(f"- 💡 {a}")
        for i in r["issues"]:
            lines.append(f"- 💬 unanswered {i['age_days']}d: [#{i['number']}]({i['url']}) "
                         f"{i['title']} — by @{i['author']['login']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

"""Registry + per-repo steward.yml -> one resolved Policy per repo.

Precedence (later wins): built-in defaults < registry `defaults` <
registry repo entry < the target repo's own steward.yml. The steward.yml
override is the owner's consent living next to the code it governs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import yamlio

STATUSES = ("active", "complete", "sunset-candidate")
SEMVER_LEVELS = ("patch", "minor", "major")

DEFAULTS: Dict[str, Any] = {
    "steward": {"enabled": True, "status": "complete", "tier": 0},
    "dependencies": {
        "automerge": {
            "semver": ["patch", "minor"],
            "require_checks": True,
            "min_pr_age_hours": 72,
            "max_per_run": 3,
        },
    },
    "issues": {"respond_after_days": 14},
}


class ConfigError(ValueError):
    pass


@dataclass
class Policy:
    repo: str
    enabled: bool
    status: str
    tier: int
    semver: List[str]
    require_checks: bool
    min_pr_age_hours: int
    max_per_run: int
    respond_after_days: int
    source: List[str] = field(default_factory=list)

    @property
    def may_merge(self) -> bool:
        return self.enabled and self.tier >= 1 and self.status != "sunset-candidate"


def deep_merge(base: Dict[str, Any], over: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_registry(text: str) -> Dict[str, Any]:
    data = yamlio.parse(text) or {}
    if not isinstance(data, dict) or not isinstance(data.get("repos"), list):
        raise ConfigError("registry needs a top-level `repos:` list")
    for entry in data["repos"]:
        if not isinstance(entry, dict) or "repo" not in entry:
            raise ConfigError(f"registry entry without `repo:`: {entry!r}")
        if "/" not in str(entry["repo"]):
            raise ConfigError(f"repo must be owner/name: {entry['repo']!r}")
    return data


def resolve(registry: Dict[str, Any], entry: Dict[str, Any],
            steward_yml: Optional[str]) -> Policy:
    layers = ["defaults"]
    merged = deep_merge(DEFAULTS, registry.get("defaults"))
    entry_cfg = {k: v for k, v in entry.items() if k != "repo"}
    if entry_cfg:
        merged = deep_merge(merged, entry_cfg)
        layers.append("registry")
    if steward_yml:
        try:
            own = yamlio.parse(steward_yml) or {}
        except yamlio.YAMLError as e:
            raise ConfigError(f"{entry['repo']}: steward.yml {e}") from e
        merged = deep_merge(merged, own)
        layers.append("steward.yml")

    st, am = merged["steward"], merged["dependencies"]["automerge"]
    policy = Policy(
        repo=entry["repo"],
        enabled=bool(st["enabled"]),
        status=st["status"],
        tier=int(st["tier"]),
        semver=list(am["semver"]),
        require_checks=bool(am["require_checks"]),
        min_pr_age_hours=int(am["min_pr_age_hours"]),
        max_per_run=int(am["max_per_run"]),
        respond_after_days=int(merged["issues"]["respond_after_days"]),
        source=layers,
    )
    if policy.status not in STATUSES:
        raise ConfigError(f"{policy.repo}: status must be one of {STATUSES}")
    if policy.tier not in (0, 1, 2):
        raise ConfigError(f"{policy.repo}: tier must be 0, 1 or 2")
    bad = [s for s in policy.semver if s not in SEMVER_LEVELS]
    if bad:
        raise ConfigError(f"{policy.repo}: unknown semver level(s) {bad}")
    if "major" in policy.semver:
        raise ConfigError(f"{policy.repo}: major bumps are never auto-merged")
    return policy

# lychgate

A deterministic custodian for repos you stopped tending.

*A lychgate is the roofed gate at a churchyard entrance, where a coffin
rests until the priest arrives. Here, PRs that pass fixed rules go
through the gate, and everything else waits there for its owner.*

Dependabot keeps opening PRs on repos you no longer visit; nobody merges
them. Lychgate runs from one always-active **control repo**, looks at
every repo you register, merges the Dependabot PRs that are provably
boring, and writes a digest of everything that needs you.

Tier 1 has **no LLM**. Every verdict comes from fixed rules over GitHub
metadata, so nothing written in a PR or issue can talk it into a merge.

## What it merges

A PR is merged only when **all** of these hold:

- author is Dependabot (human PRs are never touched)
- semver level is patch or minor (from Dependabot's `update-type`
  metadata and the title; `0.x` minor bumps count as major; unknown means hold)
- only dependency manifests and lockfiles changed (no source, no workflows)
- CI exists and is green (no CI means hold)
- GitHub reports it mergeable
- it is at least `min_pr_age_hours` old (default 72h cooling)
- this run has not spent `max_per_run` merges yet

It merges with `--match-head-commit`, so if Dependabot pushes after the
evaluation, GitHub refuses the merge.

Everything else is reported with the exact reason: **hold** (needs you),
**wait** (re-checked next run), or **observe** (a tier-0 or sunset repo, summarised).

## What it cannot do

The tier-1 GitHub App gets `contents` and `pull_requests` write, and read
access to `issues`, `checks`, `commit statuses` and `dependabot alerts`.
It gets **no** `administration` and **no** `workflows` permission, so it
has no way to archive, delete, change settings, or edit CI, whatever the
code says. That also means Dependabot's `github-actions` updates always
go to you. (Tier 2 will use a separate App with `issues` write and no merge rights.)

## Tiers and status

| tier | does | | status | meaning |
|---|---|---|---|---|
| 0 | observe, digest | | `active` | you still work on it |
| 1 | + merge safe Dependabot PRs | | `complete` | feature-done, keep it alive |
| 2 | *(week 2: draft issue replies)* | | `sunset-candidate` | report only, never merge |

Config precedence: built-in defaults < `registry.yml` defaults < registry
entry < the repo's own `.github/steward.yml` (see `steward.example.yml`).
The owner can always revoke from inside the repo with `steward.enabled: false`.

## Run locally (dry-run)

```bash
python3 -m lychgate run            # uses your `gh` login, read-only
python3 -m lychgate run --only ictechgy/kartograph
python3 -m unittest discover -s tests -t .
```

Only `--apply` ever calls a mutating API.

## Set up the control repo

1. Push this repo to GitHub (e.g. `ictechgy/lychgate`).
2. Create a GitHub App with the permissions above and install it on the repos in `registry.yml`.
3. In the control repo, set variable `GK_APP_CLIENT_ID` and secret `GK_APP_PRIVATE_KEY`.
4. Let it run in dry-run for a few days and read the Actions summaries.
5. When the verdicts look right, set variable `GK_APPLY=1`.

Every merge is appended to `ledger.jsonl`, and every run's digest is
committed to `digests/`. Those commits also keep the control repo active,
which stops GitHub from disabling its cron after 60 idle days.

Requires Python ≥ 3.9 and `gh`. No other dependencies. `yamlio.py` is
vendored from [riskgate](https://github.com/ictechgy/riskgate) (MIT).

## Threat model (v0.1)

Tier 1 trusts **people with write access to the target repo**. It checks
file paths, authors, CI and semver, but not diff contents. Someone with
write access who adds a malicious line to a Dependabot branch is caught
by the non-Dependabot-commit gate. Someone who changes a manifest through
some other route is not. That is fine for your own repos. Adopting
strangers' repos will need a diff-content layer first.

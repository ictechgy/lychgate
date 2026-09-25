from datetime import datetime, timezone

from lychgate.config import resolve

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)

META_PATCH = (
    "Bumps [qs](https://github.com/ljharb/qs) from 6.5.2 to 6.5.3.\n\n---\n"
    "updated-dependencies:\n- dependency-name: qs\n  dependency-type: indirect\n"
    "  update-type: version-update:semver-patch\n...\n\n"
    "Signed-off-by: dependabot[bot] <support@github.com>"
)

GREEN = [
    {"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
    {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SKIPPED"},
]


def policy(**steward):
    entry = {"repo": "me/tool", "steward": {"status": "complete", "tier": 1, **steward}}
    return resolve({}, entry, None)


def pr(number=1, **over):
    base = {
        "number": number,
        "title": "Bump qs from 6.5.2 to 6.5.3",
        "url": f"https://github.com/me/tool/pull/{number}",
        "author": {"login": "app/dependabot", "is_bot": True},
        "createdAt": "2026-09-20T00:00:00Z",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "headRefOid": f"sha{number}",
        "files": [{"path": "package.json"}, {"path": "package-lock.json"}],
        "commits": [{"messageBody": META_PATCH}],
        "statusCheckRollup": GREEN,
    }
    base.update(over)
    return base

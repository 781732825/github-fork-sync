#!/usr/bin/env python3
"""Sync every fork owned by a GitHub account with its upstream.

For each fork the script:

1. reads the fork and its upstream through the GitHub REST API,
2. applies safety guards that skip a fork whose upstream is gone, whose
   tracked branch disappeared, or whose upstream content was wiped,
3. fast-forwards the fork with ``POST /repos/{owner}/{repo}/merge-upstream``.

Only fast-forwards are used, so a fork with local commits is never rewritten.

Environment:
    GH_SYNC_TOKEN   required: token for the account being synced (``repo``
                    scope, plus ``workflow`` when forks carry workflow files).
                    The built-in ``GITHUB_TOKEN`` cannot substitute for it,
                    because it only reaches a single repository.
    GH_SYNC_OWNER   optional account login; defaults to the token's own user.
    GH_SYNC_WORKERS optional parallel request count, default 6.
    GH_SYNC_DRY_RUN  when true, report what would be synced without pushing.
    REPORT_PATH     optional file to append the Markdown report to.

Exit codes: 0 all good, 1 at least one failure, 2 configuration error.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

API = "https://api.github.com"
USER_AGENT = "github-fork-sync"
MAX_WORKERS = int(os.environ.get("GH_SYNC_WORKERS", "6"))
DRY_RUN = os.environ.get("GH_SYNC_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}

# --- Guard thresholds -------------------------------------------------------
# A fork is only checked for shrinkage when it holds at least this many entries;
# below it the two trees are too small for a ratio to mean anything.
TINY_FORK_ENTRIES = 20
# An upstream holding less than this share of the fork's entries has collapsed.
SHRINK_RATIO = 0.5
# Feeding this many entries or fewer is always treated as a stub, whatever the
# fork size, because that is the "wiped down to one README" shape.
STUB_ENTRIES = 1


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}" if status else str(message))
        self.status = status
        self.message = message


def api(method: str, path: str, body: dict | None = None, retries: int = 4):
    """Call the GitHub REST API and return (status, parsed_json_or_None)."""
    token = os.environ["GH_SYNC_TOKEN"]
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode() if body is not None else None
    last: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("User-Agent", USER_AGENT)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"message": raw.decode("utf-8", "replace")}
            # Any 4xx other than rate limiting is a definitive answer.
            if error.code < 500 and error.code != 429:
                return error.code, payload
            last = ApiError(error.code, str(payload.get("message", "")))
        except Exception as error:  # network hiccup, TLS reset, timeout
            last = error
        time.sleep(1.5 * (attempt + 1))
    raise ApiError(0, f"request failed after {retries} attempts: {last}")


def list_repos() -> list[dict]:
    repos: list[dict] = []
    page = 1
    while True:
        status, payload = api(
            "GET",
            f"/user/repos?affiliation=owner&per_page=100&page={page}&sort=full_name",
        )
        if status != 200:
            raise ApiError(status, str(payload))
        repos.extend(payload)
        if len(payload) < 100:
            return repos
        page += 1


def resolve_login() -> str:
    status, payload = api("GET", "/user")
    if status == 403:
        raise ApiError(
            403,
            "the token cannot read the account it belongs to. The Actions "
            "GITHUB_TOKEN only reaches this one repository; set GH_SYNC_TOKEN "
            "to a personal access token instead",
        )
    if status != 200:
        raise ApiError(status, str(payload))
    return payload["login"]


def entry_count(repo: str, ref: str) -> int | None:
    """Entries (files + directories) reachable from ref, None if unreadable."""
    status, payload = api("GET", f"/repos/{repo}/git/trees/{ref}?recursive=1")
    if status != 200:
        return None
    return len(payload.get("tree", []))


def branch_entry_count(repo: str, branch: str) -> int | None:
    status, payload = api("GET", f"/repos/{repo}/branches/{branch}")
    if status != 200:
        return None
    return entry_count(repo, payload["commit"]["sha"])


def primary_language(repo: str) -> str | None:
    """Language with the most bytes in repo, None when unreadable."""
    status, payload = api("GET", f"/repos/{repo}/languages")
    if status != 200 or not isinstance(payload, dict) or not payload:
        return None
    return max(payload.items(), key=lambda item: item[1])[0]


@dataclass
class Result:
    name: str
    action: str  # synced | up-to-date | skipped | failed
    reason: str = ""
    upstream: str = ""
    commits: int = 0
    evidence: dict = field(default_factory=dict)


def check_and_sync(repo: dict, owner: str) -> Result:
    name = repo["full_name"].split("/", 1)[1]
    fork_branch = repo["default_branch"]

    # Repositories you own are their own upstream: nothing to sync from.
    if not repo.get("fork"):
        return Result(name, "skipped", "not a fork (own repository)")

    status, detail = api("GET", f"/repos/{owner}/{name}")
    if status != 200:
        return Result(name, "failed", f"cannot read repository (HTTP {status})")
    parent = detail.get("parent")
    if not parent:
        return Result(name, "skipped", "the API records no upstream for this fork")
    upstream = parent["full_name"]
    upstream_default = parent["default_branch"]

    # Guard 1: is the upstream reachable at all?
    status, upstream_repo = api("GET", f"/repos/{upstream}")
    if status != 200:
        return Result(
            name,
            "skipped",
            f"upstream unreachable (HTTP {status}): deleted or made private",
            upstream=upstream,
        )

    # Guard 2: does the upstream still have the branch this fork tracks?
    status, upstream_branch_info = api("GET", f"/repos/{upstream}/branches/{fork_branch}")
    if status != 200:
        return Result(
            name,
            "skipped",
            f"upstream no longer has branch '{fork_branch}' "
            f"(its default is '{upstream_default}')",
            upstream=upstream,
        )

    # Guard 3: was the upstream wiped or replaced by a stub?
    upstream_entries = entry_count(upstream, upstream_branch_info["commit"]["sha"])
    fork_entries = branch_entry_count(f"{owner}/{name}", fork_branch)
    if upstream_entries is None:
        return Result(
            name,
            "skipped",
            "cannot read the upstream file list, refusing to sync blind",
            upstream=upstream,
        )
    evidence = {
        "upstream_entries": upstream_entries,
        "fork_entries": fork_entries,
        "upstream_branch": upstream_default,
    }
    wiped = upstream_entries <= STUB_ENTRIES
    if not wiped and fork_entries and fork_entries >= TINY_FORK_ENTRIES:
        wiped = upstream_entries < fork_entries * SHRINK_RATIO
    if wiped:
        return Result(
            name,
            "skipped",
            f"upstream looks wiped: {upstream_entries} entries against "
            f"{fork_entries} in the fork",
            upstream=upstream,
            evidence=evidence,
        )

    # A changed primary language cannot block a sync on its own (GitHub derives
    # it heuristically and projects do shift), but it is worth reporting.
    fork_language = primary_language(f"{owner}/{name}")
    upstream_language = primary_language(upstream)
    evidence.update({"fork_language": fork_language, "upstream_language": upstream_language})
    language_note = ""
    if fork_language and upstream_language and fork_language != upstream_language:
        language_note = (
            f"  [check: primary language differs, fork {fork_language} vs "
            f"upstream {upstream_language}]"
        )

    # Real comparison against the branch the fork tracks.
    status, comparison = api(
        "GET", f"/repos/{upstream}/compare/{fork_branch}...{owner}:{fork_branch}"
    )
    if status != 200:
        return Result(
            name,
            "skipped",
            f"branches share no common ancestor (HTTP {status}), "
            f"a fast-forward is impossible",
            upstream=upstream,
            evidence=evidence,
        )
    state = comparison["status"]
    ahead, behind = comparison["ahead_by"], comparison["behind_by"]
    evidence.update({"status": state, "ahead": ahead, "behind": behind})

    if state == "identical":
        return Result(
            name, "up-to-date", "already identical to upstream" + language_note,
            upstream=upstream, evidence=evidence,
        )
    if behind == 0:
        return Result(
            name,
            "skipped",
            f"fork is ahead by {ahead} commit(s), upstream has nothing new"
            + language_note,
            upstream=upstream,
            evidence=evidence,
        )
    if ahead > 0:
        return Result(
            name,
            "skipped",
            f"history diverged (ahead {ahead}, behind {behind}), "
            f"a fast-forward would drop local commits" + language_note,
            upstream=upstream,
            evidence=evidence,
        )

    # Guard 4: fast-forward only.
    if DRY_RUN:
        return Result(
            name,
            "would-sync",
            f"dry run: would fast-forward {behind} commit(s)" + language_note,
            upstream=upstream,
            commits=behind,
            evidence=evidence,
        )
    status, merge = api(
        "POST", f"/repos/{owner}/{name}/merge-upstream", {"branch": fork_branch}
    )
    if status != 200:
        message = merge.get("message") if isinstance(merge, dict) else str(merge)
        return Result(
            name,
            "failed",
            f"merge-upstream refused (HTTP {status}): {message}",
            upstream=upstream,
            evidence=evidence,
        )
    return Result(
        name, "synced", merge.get("message", "synced") + language_note,
        upstream=upstream, commits=behind, evidence=evidence,
    )


def run_one(repo: dict, owner: str) -> Result:
    name = repo["full_name"].split("/", 1)[1]
    try:
        return check_and_sync(repo, owner)
    except ApiError as error:
        return Result(name, "failed", str(error))
    except Exception as error:  # one bad repo must not abort the whole run
        return Result(name, "failed", f"{type(error).__name__}: {error}")


def render(results: list[Result], owner: str) -> str:
    order = {"synced": 0, "would-sync": 1, "failed": 2, "skipped": 3, "up-to-date": 4}
    results = sorted(results, key=lambda item: (order.get(item.action, 9), item.name.lower()))
    counts: dict[str, int] = {}
    for result in results:
        counts[result.action] = counts.get(result.action, 0) + 1

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Fork sync report - {owner}",
        "",
        f"Run at {now}.",
        "",
        f"- synced: **{counts.get('synced', 0)}**",
        f"- would sync (dry run): **{counts.get('would-sync', 0)}**",
        f"- up to date: **{counts.get('up-to-date', 0)}**",
        f"- skipped: **{counts.get('skipped', 0)}**",
        f"- failed: **{counts.get('failed', 0)}**",
        "",
        "| repository | action | upstream | detail |",
        "| --- | --- | --- | --- |",
    ]
    for result in results:
        detail = result.reason.replace("|", "\\|")
        lines.append(
            f"| `{result.name}` | {result.action} | `{result.upstream or '-'}` | {detail} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    token = os.environ.get("GH_SYNC_TOKEN", "").strip()
    if not token:
        print(
            "error: GH_SYNC_TOKEN is empty.\n"
            "Set it to a personal access token with 'repo' scope (and 'workflow'\n"
            "when forks contain workflow files). In GitHub Actions, store it as\n"
            "the SYNC_TOKEN repository secret.",
            file=sys.stderr,
        )
        return 2
    os.environ["GH_SYNC_TOKEN"] = token

    try:
        owner = os.environ.get("GH_SYNC_OWNER") or resolve_login()
        repos = list_repos()
    except ApiError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    forks = [repo for repo in repos if repo.get("fork")]
    print(
        f"{owner}: {len(repos)} repositories, {len(forks)} forks, "
        f"workers={MAX_WORKERS}"
    )

    with ThreadPoolExecutor(MAX_WORKERS) as pool:
        results = list(pool.map(lambda repo: run_one(repo, owner), forks))

    report = render(results, owner)
    print()
    print(report)

    report_path = os.environ.get("REPORT_PATH")
    if report_path:
        with open(report_path, "a", encoding="utf-8") as handle:
            handle.write(report)

    failures = [result for result in results if result.action == "failed"]
    if failures:
        print(f"{len(failures)} repository(ies) failed:", file=sys.stderr)
        for result in failures:
            print(f"  {result.name}: {result.reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

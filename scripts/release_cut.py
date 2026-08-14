#!/usr/bin/env python3
"""Cut a dated release: bump the version, tag the commit, write a digest.

Versioning scheme (starting 2026-06-01): ``v<major>.<minor>.<patch>-<YYYYMMDD>``.
The date suffix makes every tag self-dating, so a day with no release simply has
no tag (no need to bump on idle days). Three modes:

  - ``daily``   -> patch bump (v0.8.0 -> v0.8.1). One cut per active day. The
    tag's annotated message carries that day's PR summary, so the daily record
    lives in git (queryable via ``git tag -n`` / ``git show``), no file churn.
    Multiple cuts on the same day get an HHMM suffix (v0.8.1-20260601-2230);
    the same HHMM minute is rejected to avoid collisions.
  - ``weekly``  -> minor bump, patch reset (v0.8.4 -> v0.9.0). Once a week
    (Mondays). Additionally writes a full digest draft to the gitignored
    ``outputs/release-digests/``; synthesize its Highlights section and fold the
    result into ``CHANGELOG.md`` — the digest file itself is scratch, not a
    tracked artifact.
  - ``catchup`` -> heal a gap in the cadence. Walks every day after the latest
    dated tag up to and including today, cutting the tag each day should have
    had (Mondays weekly, everything else daily), anchored at the last main
    commit of that UTC day. A window with no merged PRs cuts nothing.

History v0.1-v0.7 (two-segment milestone tags) is frozen as the retroactive
milestone review; this dated three-segment scheme seeds at ``v0.8.0`` (the first
cut when no prior dated tag exists).

Usage:
    .venv/bin/python scripts/release_cut.py daily              # patch bump, tag HEAD
    .venv/bin/python scripts/release_cut.py weekly             # minor bump + weekly digest
    .venv/bin/python scripts/release_cut.py catchup            # backfill every missed day
    .venv/bin/python scripts/release_cut.py daily --push       # also push the tag(s)
    .venv/bin/python scripts/release_cut.py daily --release    # tag, push, and create a GitHub Release

Stable-channel gate (L4, #1185): a dated tag may only cut a commit that has
passed the staging deploy+smoke gate — the rolling ``latest`` tag must exist and
point exactly at the commit being tagged (daily/weekly cut HEAD, so this means
HEAD must be green on staging). ``catchup`` backfills historical days whose
commits cannot equal the rolling tag, so it only requires the pipeline to be
alive (``latest`` exists).

Without ``--push`` / ``--release`` it tags locally; review, then push. ``--release``
implies ``--push`` and additionally creates a GitHub Release for each tag via
``gh release create``, using the tag's annotated message as the release body.
A ``.github/workflows/release.yml`` workflow also creates releases on any dated
tag push, so ``--release`` is optional when CI is reachable. Weekly digest drafts are
written to the gitignored scratch dir either way — they are raw material, not a
tracked artifact. Wire the cadence from a long-running agent via the watcher SDK, e.g.
    ava.watcher.cron("0 18 * * 2-5", "run scripts/release_cut.py daily --release", name="release-cut-daily")
    ava.watcher.cron("0 18 * * 1",   "run scripts/release_cut.py weekly --release", name="release-cut-weekly")
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

# Dated three-segment tag: v<major>.<minor>.<patch>-<YYYYMMDD>[-HHMM]
from shared.release_tags import _TAG, pick_latest_tag

_SEED = (0, 8, 0)  # first dated release when no prior dated tag exists

_PREFIX = re.compile(r"^(?P<type>\w+)(?:\((?P<scope>[^)]+)\))?!?:")
_TYPE_ORDER = ["feat", "fix", "refactor", "perf", "test", "docs", "ci", "chore"]
_CHURN_EXCLUDE = re.compile(r"\.lock$|lock\.json$|lock\.yaml$|\.svg$")


def _run(cmd: list[str]) -> str:
    # cmd is always an internal hardcoded git/gh argv, never user input.
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout  # noqa: S603


def _git_quiet(*args: str) -> subprocess.CompletedProcess[str]:
    """git without check: the caller interprets the return code."""
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)  # noqa: S603


def _latest_staging_sha() -> str | None:
    """Resolve the `latest` channel tag — the newest main commit that passed
    the staging deploy+smoke gate (L4, #1185). Local ref first; on a missing
    local ref, fetch it from origin once. None = no commit has gone green."""
    r = _git_quiet("rev-parse", "-q", "--verify", "refs/tags/latest")
    if r.returncode == 0:
        return r.stdout.strip()
    _git_quiet("fetch", "origin", "refs/tags/latest")
    r = _git_quiet("rev-parse", "-q", "--verify", "refs/tags/latest")
    return r.stdout.strip() if r.returncode == 0 else None


def _staging_gate(target: str) -> None:
    """Stable-channel gate: refuse to cut a dated tag unless `target` already
    passed the staging deploy+smoke gate — the rolling `latest` tag must exist
    and point exactly at the commit being tagged."""
    latest = _latest_staging_sha()
    if latest is None:
        raise SystemExit(
            "staging gate: refs/tags/latest does not exist — no commit has passed the "
            "staging deploy+smoke gate yet; cut after a green staging deploy"
        )
    if latest != target:
        raise SystemExit(
            f"staging gate: {target[:10]} has not passed the staging gate "
            f"(latest={latest[:10]}); deploy to staging first"
        )
    print(f"staging gate ok: {target[:10]} == latest")


def _latest_dated() -> tuple[str, tuple[int, int, int], date, str | None] | None:
    """Highest dated version: (tag_name, version, calendar_day, hhmm_or_None).

    Selection rule lives in `shared.release_tags.pick_latest_tag` (the update
    track resolves the same "newest release"): highest version wins; on equal
    versions the later HHMM wins; a tag without HHMM is earliest on its day.
    """
    tag = pick_latest_tag(_run(["git", "tag", "--list"]).splitlines())
    if tag is None:
        return None
    return (tag.name, tag.version, tag.day, tag.hhmm)


def _bump(mode: str, prev: tuple[int, int, int] | None) -> tuple[int, int, int]:
    if prev is None:
        return _SEED  # seed the scheme; the first cut is the baseline
    major, minor, patch = prev
    if mode == "daily":
        return major, minor, patch + 1
    return major, minor + 1, 0  # weekly: minor bump, reset patch


def _catchup_days(last_tagged: date, today: date) -> list[tuple[date, str]]:
    """Every day after the latest tag through today, with its cadence mode."""
    return [
        (day, "weekly" if day.weekday() == 0 else "daily")
        for n in range(1, (today - last_tagged).days + 1)
        for day in [last_tagged + timedelta(days=n)]
    ]


def _day_target(day: date) -> str:
    """Last first-parent main commit at or before the end of `day` (UTC)."""
    out = _run(
        [
            "git",
            "log",
            "--first-parent",
            "-1",
            f"--until={day} 23:59:59 +0000",
            "--format=%H",
            "HEAD",
        ]
    )
    sha = out.strip()
    if not sha:
        raise SystemExit(f"no commit at or before {day}; cannot anchor a tag there")
    return sha


def _gh_pr_command(prev_tag: str | None) -> list[str]:
    """Build the ``gh pr list`` argv for PRs merged since the previous tag."""
    gh_cmd = [
        "gh",
        "pr",
        "list",
        "--state",
        "merged",
        "--base",
        "main",
        "--json",
        "number,title,mergedAt,mergeCommit",
    ]
    if prev_tag is None:
        return [*gh_cmd, "--limit", "1000"]  # one-time bootstrap: no tag yet to narrow the window
    m = _TAG.match(prev_tag)
    if m is None:
        raise SystemExit(f"prev_tag {prev_tag!r} is not a dated release tag")
    since = date(int(m[4][:4]), int(m[4][4:6]), int(m[4][6:8])) - timedelta(days=1)
    return [*gh_cmd, "--limit", "500", "--search", f"merged:>={since}"]


def _merge_commit_prs(gh_out: str, shas: set[str]) -> dict[int, dict[str, Any]]:
    """PRs whose post-rebase ``mergeCommit.oid`` actually sits in the range."""
    prs: dict[int, dict[str, Any]] = {}
    for entry in json.loads(gh_out):
        merge_sha = (entry.get("mergeCommit") or {}).get("oid")
        if merge_sha in shas:
            prs[entry["number"]] = {
                "number": entry["number"],
                "title": entry["title"],
                "mergedAt": entry["mergedAt"],
            }
    return prs


def _subject_suffix_prs(log_out: str, prs: dict[int, dict[str, Any]]) -> None:
    """Union in PRs gh missed via the legacy ``(#N)$`` subject-suffix match."""
    for line in log_out.strip().split("\n"):
        if not line:
            continue
        _sha, date_str, subject = line.split("\x00", 2)
        m = re.search(r"\(#(\d+)\)$", subject)
        if m and int(m.group(1)) not in prs:
            prs[int(m.group(1))] = {
                "number": int(m.group(1)),
                "title": subject,
                "mergedAt": date_str,
            }


def _merged_prs(prev_tag: str | None, target: str) -> list[dict[str, Any]]:
    """PRs that actually landed in `prev_tag..target`, not just gh's merge log.

    This repo rebase-merges (linear history, no merge commits), so a merged
    PR's commits get rewritten onto main with no ``(#N)`` suffix on the
    subject — commit-subject parsing alone can't find them. Primary source is
    ``gh pr list --state merged``, intersected against the actual commit SHAs
    in ``git log <prev_tag>..<target>`` via ``mergeCommit.oid`` (the post-rebase
    commit gh recorded as landing on main): this keeps the original
    git-log-range guarantee that a PR only counts if it's truly in this range,
    not merely merged by gh's global clock (it could already be in a prior
    release tag). The legacy ``(#N)$`` subject-suffix match is kept as a
    fallback union, for squash/merge-commit-style history gh's search window
    might miss.
    """
    range_spec = target if prev_tag is None else f"{prev_tag}..{target}"
    shas = set(_run(["git", "log", range_spec, "--format=%H"]).split())
    if not shas:
        return []

    try:
        gh_out = _run(_gh_pr_command(prev_tag))
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise SystemExit(f"`gh pr list` failed ({e}); cannot detect merged PRs without gh") from e

    prs = _merge_commit_prs(gh_out, shas)
    log_out = _run(["git", "log", range_spec, "--format=%H%x00%aI%x00%s"])
    _subject_suffix_prs(log_out, prs)
    return list(prs.values())


def _churn(prev_tag: str | None, target: str) -> tuple[int, int]:
    """Lines added/deleted between prev_tag and target, excluding lockfiles."""
    range_spec = target if prev_tag is None else f"{prev_tag}..{target}"
    out = _run(
        [
            "git",
            "log",
            range_spec,
            "--numstat",
            "--pretty=format:",
        ]
    )
    add = dele = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or parts[0] == "-":
            continue
        if _CHURN_EXCLUDE.search(parts[2]):
            continue
        add += int(parts[0])
        dele += int(parts[1])
    return add, dele


def _cluster(prs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pr in prs:
        m = _PREFIX.match(pr["title"])
        groups[m.group("type") if m else "other"].append(pr)
    return groups


def _ordered_types(groups: dict[str, list[dict[str, Any]]]) -> list[str]:
    head = [t for t in _TYPE_ORDER if t in groups]
    return head + sorted(t for t in groups if t not in _TYPE_ORDER)


def _tag_message(tag: str, prs: list[dict[str, Any]], churn: tuple[int, int]) -> str:
    groups = _cluster(prs)
    counts = ", ".join(f"{t}:{len(groups[t])}" for t in _ordered_types(groups))
    lines = [f"{tag}: {len(prs)} PRs (+{churn[0]}/-{churn[1]})  {counts}", ""]
    for t in _ordered_types(groups):
        for pr in sorted(groups[t], key=lambda p: p["number"]):
            lines.append(f"- #{pr['number']} {pr['title']}")
    return "\n".join(lines)


def _weekly_md(
    tag: str, since: str, until: str, prs: list[dict[str, Any]], churn: tuple[int, int]
) -> str:
    groups = _cluster(prs)
    lines = [
        f"# {tag} — weekly digest ({since} -> {until})",
        "",
        f"**{len(prs)} PRs merged** · +{churn[0]:,} / -{churn[1]:,} lines "
        f"(net {churn[0] - churn[1]:+,}, lockfiles excluded)",
        "",
        "## Highlights (fill in — the synthesis step)",
        "",
        "> Pick the key nodes + any direction shift this week. Raw material below.",
        "",
        "## Merged PRs by area",
        "",
    ]
    for t in _ordered_types(groups):
        items = sorted(groups[t], key=lambda p: p["number"])
        lines.append(f"### {t} ({len(items)})")
        lines += [f"- #{pr['number']} {pr['title']}" for pr in items]
        lines.append("")
    return "\n".join(lines)


def _cut(
    mode: str,
    day: date,
    target: str,
    version: tuple[int, int, int],
    prev_tag: str | None = None,
    hhmm: str | None = None,
) -> str | None:
    """Tag `target` as `day`'s release; returns the tag, or None on an idle window."""
    since = str(day if mode == "daily" else day - timedelta(days=7))
    until = str(day)
    prs = _merged_prs(prev_tag, target)
    if not prs:
        return None  # idle window: the scheme leaves no tag
    churn = _churn(prev_tag, target)
    major, minor, patch = version
    tag = f"v{major}.{minor}.{patch}-{day.strftime('%Y%m%d')}"
    if hhmm:
        tag += f"-{hhmm}"
    _run(["git", "tag", "-a", tag, target, "-m", _tag_message(tag, prs, churn)])
    written = ""
    if mode == "weekly":
        out_dir = Path(__file__).resolve().parent.parent / "outputs" / "release-digests"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{tag}.md"
        out_path.write_text(_weekly_md(tag, since, until, prs, churn), encoding="utf-8")
        written = f", wrote {out_path}"
    print(f"cut {tag} @ {target[:10]} ({len(prs)} PRs, +{churn[0]}/-{churn[1]}){written}")
    return tag


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["daily", "weekly", "catchup"])
    ap.add_argument("--push", action="store_true", help="push the tags after creating them")
    ap.add_argument(
        "--release",
        action="store_true",
        help="create a GitHub Release for each tag (implies --push)",
    )
    args = ap.parse_args()
    args.push = args.push or args.release  # release implies push — gh needs the tag on the remote
    return args


def _plan_daily(
    latest: tuple[str, tuple[int, int, int], date, str | None] | None,
    now: datetime,
    today: date,
) -> tuple[tuple[int, int, int], str | None, str | None]:
    """Version/HHMM/prev-tag for today's daily cut.

    Same day as the latest tag: keep the patch, append HHMM (rejecting the
    same minute as a collision). New day (or seed): bump the patch, no HHMM
    for the first cut of the day.
    """
    prev_tag = latest[0] if latest else None
    if latest is not None and latest[2] == today:
        current_hhmm = now.strftime("%H%M")
        if latest[3] == current_hhmm:
            raise SystemExit(
                f"release {today.strftime('%Y%m%d')}{current_hhmm} already cut; wait one minute"
            )
        return latest[1], current_hhmm, prev_tag
    return _bump("daily", latest[1] if latest else None), None, prev_tag


def _cut_daily(
    latest: tuple[str, tuple[int, int, int], date, str | None] | None,
    now: datetime,
    today: date,
) -> list[str]:
    version, hhmm, prev_tag = _plan_daily(latest, now, today)
    tag = _cut("daily", today, "HEAD", version, prev_tag=prev_tag, hhmm=hhmm)
    return [tag] if tag else []


def _cut_weekly(
    latest: tuple[str, tuple[int, int, int], date, str | None] | None,
    today: date,
) -> list[str]:
    prev_tag = latest[0] if latest else None
    version = _bump("weekly", latest[1] if latest else None)
    tag = _cut("weekly", today, "HEAD", version, prev_tag=prev_tag)
    return [tag] if tag else []


def _cut_catchup(
    latest: tuple[str, tuple[int, int, int], date, str | None] | None,
    today: date,
) -> list[str]:
    """Backfill every missed day after the latest tag, anchoring each cut."""
    if latest is None:
        raise SystemExit("catchup extends an existing dated tag; seed with daily first")
    tag_name, version, last_tagged, _hhmm = latest
    created: list[str] = []
    for day, mode in _catchup_days(last_tagged, today):
        tag = _cut(mode, day, _day_target(day), _bump(mode, version), prev_tag=tag_name)
        if tag:
            version = _bump(mode, version)
            tag_name = tag  # this tag becomes the anchor for the next day
            created.append(tag)
    return created


def _push_tags(created: list[str]) -> None:
    _run(["git", "push", "origin", *created])
    print(f"pushed {len(created)} tag(s)")


def _create_releases(created: list[str]) -> None:
    for tag in created:
        _run(["gh", "release", "create", tag, "--title", tag, "--notes-from-tag"])
        print(f"  release created for {tag}")


def _finish(created: list[str], *, push: bool, release: bool) -> None:
    if not created:
        print("nothing to cut (no merged PRs in the window)")
        return
    if push:
        _push_tags(created)
        if release:
            _create_releases(created)
    else:
        print(f"{len(created)} tag(s) created [not pushed]")


def main() -> None:
    args = _parse_args()

    # UTC calendar date is the release stamp — the whole cut (PR membership via
    # gh `merged:`, churn window, anchor commit) is UTC so the day's boundaries
    # are one consistent thing, not a gh-UTC / git-local split.
    now = datetime.now(UTC)
    today = now.date()
    latest = _latest_dated()

    if args.mode == "catchup":
        # Catchup anchors historical days; those commits can never equal the
        # rolling `latest`, so the gate is the weaker "pipeline alive" check
        # (the tag must exist). The strong equality check applies to cuts of
        # current HEAD (daily/weekly).
        if _latest_staging_sha() is None:
            raise SystemExit(
                "staging gate: refs/tags/latest does not exist — no commit has passed the "
                "staging deploy+smoke gate; cannot cut a stable channel"
            )
        created = _cut_catchup(latest, today)
    else:
        _staging_gate(_run(["git", "rev-parse", "HEAD"]).strip())
        if args.mode == "daily":
            created = _cut_daily(latest, now, today)
        else:
            created = _cut_weekly(latest, today)
    _finish(created, push=args.push, release=args.release)


if __name__ == "__main__":
    main()

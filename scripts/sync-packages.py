#!/usr/bin/env python

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ROOT_DIR = Path(__file__).resolve().parent.parent
PACKAGES_DIR = ROOT_DIR / "packages"
DEFAULT_MAX_PACKAGES = 10
SEARCH_QUERY = "format_version path:/ filename:tooth.json"
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
LOG_FORMAT = "%(levelname)s %(message)s"


logger = logging.getLogger(__name__)


class CommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManifestRecord:
    repo: str
    tooth: str
    version: str
    content: bytes


@dataclass(frozen=True)
class PackageCandidate:
    tooth: str
    manifests: tuple[ManifestRecord, ...]
    already_present: bool


@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    state: str
    merged_at: str | None
    url: str


@dataclass
class Summary:
    repos_discovered: int = 0
    missing_packages_found: int = 0
    created: int = 0
    updated: int = 0
    skipped_closed: int = 0
    skipped_no_change: int = 0
    failed_packages: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover missing lip package manifests and open/update package PRs."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and log planned changes without pushing branches or opening PRs.",
    )
    parser.add_argument(
        "--max-packages",
        type=int,
        default=DEFAULT_MAX_PACKAGES,
        help=f"Maximum number of package PRs to create or update in one run (default: {DEFAULT_MAX_PACKAGES}).",
    )
    args = parser.parse_args()
    if args.max_packages < 1:
        parser.error("--max-packages must be at least 1")
    return args


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"Required command not found: {name}")


def run_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    if check and completed.returncode != 0:
        command = " ".join(args)
        stderr = completed.stderr.strip()
        raise CommandError(f"Command failed ({command}): {stderr}")
    return completed


def retry_call(
    label: str,
    func: Any,
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    retryable: Callable[[Exception], bool] | None = None,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if retryable is not None and not retryable(exc):
                break
            if attempt == attempts:
                break
            delay = base_delay * attempt
            logger.warning(
                "%s failed on attempt %s/%s: %s", label, attempt, attempts, exc
            )
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def package_manifest_path(root: Path, tooth: str, version: str) -> Path:
    return root / "packages" / f"{tooth}@{version}" / "tooth.json"


def semver_key(version: str) -> tuple[Any, ...]:
    match = SEMVER_RE.fullmatch(version)
    if match is None:
        raise ValueError(f"Invalid semver: {version}")

    prerelease = match.group(4)
    prerelease_tokens: list[tuple[int, Any]] = []
    if prerelease:
        for token in prerelease.split("."):
            if token.isdigit():
                prerelease_tokens.append((0, int(token)))
            else:
                prerelease_tokens.append((1, token))

    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        0 if prerelease else 1,
        prerelease_tokens,
        version,
    )


def list_existing_tooths() -> set[str]:
    tooths: set[str] = set()
    for path in PACKAGES_DIR.rglob("tooth.json"):
        rel_parent = path.relative_to(PACKAGES_DIR).parent.as_posix()
        if "@" not in rel_parent:
            continue
        tooths.add(rel_parent.rsplit("@", 1)[0])
    return tooths


def gh_api_json(path: str, *, params: dict[str, str] | None = None) -> Any:
    args = ["gh", "api", path, "-X", "GET"]
    for key, value in (params or {}).items():
        args.extend(["-f", f"{key}={value}"])
    completed = run_command(args)
    return json.loads(completed.stdout)


def discover_repositories() -> list[str]:
    repos: set[str] = set()
    page = 1
    total_count: int | None = None
    incomplete_results = False

    while True:
        data = retry_call(
            f"search page {page}",
            lambda: gh_api_json(
                "search/code",
                params={"q": SEARCH_QUERY, "per_page": "100", "page": str(page)},
            ),
        )
        if total_count is None:
            total_count = int(data.get("total_count", 0))
            incomplete_results = bool(data.get("incomplete_results", False))

        items = data.get("items", [])
        if not items:
            break

        for item in items:
            repository = item.get("repository") or {}
            full_name = repository.get("full_name")
            if isinstance(full_name, str) and full_name:
                repos.add(full_name)

        if len(items) < 100:
            break
        page += 1

    repo_list = sorted(repos)
    logger.info(
        "Discovered %s unique repositories from %s search hits",
        len(repo_list),
        total_count if total_count is not None else len(repo_list),
    )
    if incomplete_results:
        logger.warning("GitHub search reported incomplete results")
    return repo_list


def list_repo_versions(repo: str) -> list[str]:
    completed = retry_call(
        f"list tags for {repo}",
        lambda: run_command(
            ["git", "ls-remote", "-t", "--refs", f"https://github.com/{repo}.git"]
        ),
    )
    versions: set[str] = set()
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ref = parts[1]
        if not ref.startswith("refs/tags/v"):
            continue
        version = ref.removeprefix("refs/tags/v")
        if SEMVER_RE.fullmatch(version) is None:
            continue
        versions.add(version)
    ordered = sorted(versions, key=semver_key)
    logger.info("Found %s semver tags for github.com/%s", len(ordered), repo)
    return ordered


def migrate_manifest(raw_content: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="lipr-migrate-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        legacy_path = temp_dir / "legacy.json"
        migrated_path = temp_dir / "migrated.json"
        legacy_path.write_bytes(raw_content)
        npx_env = os.environ.copy()
        npx_env.setdefault(
            "NPM_CONFIG_CACHE", str(Path(tempfile.gettempdir()) / "lipr-npm-cache")
        )
        run_command(
            [
                "npx",
                "-y",
                "@futrime/lip",
                "migrate",
                str(legacy_path),
                str(migrated_path),
            ],
            env=npx_env,
        )
        return migrated_path.read_bytes()


def fetch_manifest(repo: str, version: str) -> ManifestRecord:
    ref = f"v{version}"
    data = retry_call(
        f"fetch manifest {repo}@{ref}",
        lambda: gh_api_json(
            f"repos/{repo}/contents/tooth.json",
            params={"ref": ref},
        ),
        retryable=lambda exc: "Not Found" not in str(exc),
    )
    encoded_content = data.get("content")
    if not isinstance(encoded_content, str) or not encoded_content:
        raise ValueError(f"Missing manifest content for {repo}@{ref}")

    raw_manifest = base64.b64decode(encoded_content, validate=False)
    migrated_manifest = migrate_manifest(raw_manifest)
    manifest_json = json.loads(migrated_manifest)

    tooth = manifest_json.get("tooth")
    manifest_version = manifest_json.get("version")
    if not isinstance(tooth, str) or not tooth:
        raise ValueError(f"Manifest tooth is missing for {repo}@{ref}")
    if not isinstance(manifest_version, str) or not manifest_version:
        raise ValueError(f"Manifest version is missing for {repo}@{ref}")
    if manifest_version != version:
        raise ValueError(
            f"Manifest version mismatch for {repo}@{ref}: expected {version}, got {manifest_version}"
        )

    return ManifestRecord(
        repo=repo,
        tooth=tooth,
        version=manifest_version,
        content=migrated_manifest,
    )


def collect_missing_packages(
    existing_tooths: set[str],
) -> tuple[list[PackageCandidate], int]:
    repositories = discover_repositories()
    manifests_by_tooth: dict[str, dict[str, ManifestRecord]] = {}

    for repo in repositories:
        try:
            versions = list_repo_versions(repo)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to list versions for github.com/%s: %s", repo, exc)
            continue

        for version in versions:
            try:
                manifest = fetch_manifest(repo, version)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Skipping github.com/%s@%s: %s",
                    repo,
                    version,
                    exc,
                )
                continue

            destination = package_manifest_path(
                ROOT_DIR, manifest.tooth, manifest.version
            )
            if destination.exists():
                continue

            versions_for_tooth = manifests_by_tooth.setdefault(manifest.tooth, {})
            existing_manifest = versions_for_tooth.get(manifest.version)
            if existing_manifest is None:
                versions_for_tooth[manifest.version] = manifest
                continue

            if existing_manifest.content != manifest.content:
                logger.warning(
                    "Conflicting manifests for %s@%s from github.com/%s and github.com/%s; keeping the first copy",
                    manifest.tooth,
                    manifest.version,
                    existing_manifest.repo,
                    manifest.repo,
                )

    candidates: list[PackageCandidate] = []
    for tooth, manifests in manifests_by_tooth.items():
        ordered_manifests = tuple(
            sorted(
                manifests.values(), key=lambda manifest: semver_key(manifest.version)
            )
        )
        candidates.append(
            PackageCandidate(
                tooth=tooth,
                manifests=ordered_manifests,
                already_present=tooth in existing_tooths,
            )
        )

    candidates.sort(
        key=lambda candidate: (0 if candidate.already_present else 1, candidate.tooth)
    )
    return candidates, len(repositories)


def infer_repository_name() -> str:
    env_repo = os.environ.get("GITHUB_REPOSITORY")
    if env_repo:
        return env_repo

    completed = run_command(
        ["git", "config", "--get", "remote.origin.url"], cwd=ROOT_DIR
    )
    remote = completed.stdout.strip()
    ssh_match = re.fullmatch(r"git@github\.com:(.+?)(?:\.git)?", remote)
    if ssh_match:
        return ssh_match.group(1)
    https_match = re.fullmatch(r"https://github\.com/(.+?)(?:\.git)?", remote)
    if https_match:
        return https_match.group(1)
    raise SystemExit(
        f"Unable to infer GitHub repository from remote.origin.url: {remote}"
    )


def list_pull_requests(repo: str, branch: str) -> list[PullRequestInfo]:
    completed = run_command(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--head",
            branch,
            "--json",
            "number,state,mergedAt,url",
        ],
        cwd=ROOT_DIR,
    )
    items = json.loads(completed.stdout)
    return [
        PullRequestInfo(
            number=int(item["number"]),
            state=item["state"],
            merged_at=item.get("mergedAt"),
            url=item.get("url", ""),
        )
        for item in items
    ]


def remote_branch_exists(branch: str) -> bool:
    completed = run_command(
        ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
        cwd=ROOT_DIR,
    )
    return bool(completed.stdout.strip())


def build_pr_body(candidate: PackageCandidate) -> str:
    versions = "\n".join(f"- `{manifest.version}`" for manifest in candidate.manifests)
    repos = "\n".join(
        f"- `github.com/{manifest.repo}`"
        for manifest in {
            manifest.repo: manifest for manifest in candidate.manifests
        }.values()
    )
    return (
        f"## Summary\n"
        f"Add missing manifests for `{candidate.tooth}`.\n\n"
        f"## Versions\n"
        f"{versions}\n\n"
        f"## Source Repositories\n"
        f"{repos}\n"
    )


def stage_package_manifests(
    worktree_dir: Path, candidate: PackageCandidate
) -> list[Path]:
    changed_paths: list[Path] = []
    for manifest in candidate.manifests:
        destination = package_manifest_path(
            worktree_dir, manifest.tooth, manifest.version
        )
        existing_bytes = destination.read_bytes() if destination.exists() else None
        if existing_bytes == manifest.content:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(manifest.content)
        changed_paths.append(destination.relative_to(worktree_dir))
    return changed_paths


def commit_and_push(
    branch: str,
    candidate: PackageCandidate,
    *,
    base_ref: str,
    push_branch: bool,
    repo: str,
    open_pr: PullRequestInfo | None,
    dry_run: bool,
) -> str:
    title = f"feat: add {candidate.tooth} package"
    body = build_pr_body(candidate)

    if dry_run:
        action = "update" if open_pr is not None else "create"
        logger.info(
            "[dry-run] Would %s PR for %s on branch %s with versions: %s",
            action,
            candidate.tooth,
            branch,
            ", ".join(manifest.version for manifest in candidate.manifests),
        )
        return action

    worktree_dir = Path(tempfile.mkdtemp(prefix="lipr-worktree-"))
    try:
        run_command(
            ["git", "worktree", "add", "--detach", str(worktree_dir), base_ref],
            cwd=ROOT_DIR,
        )
        run_command(["git", "switch", "-C", branch], cwd=worktree_dir)

        changed_paths = stage_package_manifests(worktree_dir, candidate)
        if not changed_paths:
            logger.info("No new manifest changes for %s", candidate.tooth)
            return "noop"

        run_command(
            ["git", "add", "--", *[path.as_posix() for path in changed_paths]],
            cwd=worktree_dir,
        )
        commit_env = os.environ.copy()
        commit_env.update(
            {
                "GIT_AUTHOR_NAME": BOT_NAME,
                "GIT_AUTHOR_EMAIL": BOT_EMAIL,
                "GIT_COMMITTER_NAME": BOT_NAME,
                "GIT_COMMITTER_EMAIL": BOT_EMAIL,
            }
        )
        run_command(["git", "commit", "-m", title], cwd=worktree_dir, env=commit_env)

        if push_branch:
            run_command(
                [
                    "git",
                    "push",
                    "--force-with-lease",
                    "origin",
                    f"HEAD:refs/heads/{branch}",
                ],
                cwd=worktree_dir,
            )

        if open_pr is None:
            run_command(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    repo,
                    "--base",
                    "main",
                    "--head",
                    branch,
                    "--title",
                    title,
                    "--body",
                    body,
                ],
                cwd=worktree_dir,
            )
            logger.info("Created PR for %s", candidate.tooth)
            return "create"

        run_command(
            [
                "gh",
                "pr",
                "edit",
                str(open_pr.number),
                "--repo",
                repo,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=worktree_dir,
        )
        logger.info("Updated PR #%s for %s", open_pr.number, candidate.tooth)
        return "update"
    finally:
        run_command(
            ["git", "worktree", "remove", "--force", str(worktree_dir)],
            cwd=ROOT_DIR,
            check=False,
        )
        shutil.rmtree(worktree_dir, ignore_errors=True)


def sync_package(candidate: PackageCandidate, *, repo: str, dry_run: bool) -> str:
    branch = f"package/{candidate.tooth}"
    prs = list_pull_requests(repo, branch)

    closed_unmerged = next(
        (pr for pr in prs if pr.state == "CLOSED" and pr.merged_at is None),
        None,
    )
    if closed_unmerged is not None:
        logger.info(
            "Skipping %s because closed PR #%s already exists for branch %s",
            candidate.tooth,
            closed_unmerged.number,
            branch,
        )
        return "skip_closed"

    open_pr = next((pr for pr in prs if pr.state == "OPEN"), None)
    branch_exists = remote_branch_exists(branch) if not dry_run else open_pr is not None

    run_command(["git", "fetch", "origin", "main"], cwd=ROOT_DIR)
    base_ref = "origin/main"
    push_branch = True

    if open_pr is not None and branch_exists:
        run_command(["git", "fetch", "origin", branch], cwd=ROOT_DIR)
        base_ref = f"origin/{branch}"

    if open_pr is None and branch_exists:
        base_ref = "origin/main"

    return commit_and_push(
        branch,
        candidate,
        base_ref=base_ref,
        push_branch=push_branch,
        repo=repo,
        open_pr=open_pr,
        dry_run=dry_run,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    args = parse_args()

    require_command("gh")
    require_command("git")
    require_command("npx")

    target_repo = infer_repository_name()
    existing_tooths = list_existing_tooths()
    candidates, repo_count = collect_missing_packages(existing_tooths)

    summary = Summary(
        repos_discovered=repo_count, missing_packages_found=len(candidates)
    )
    logger.info("Found %s packages with missing versions", len(candidates))

    acted = 0
    for candidate in candidates:
        if acted >= args.max_packages:
            break

        try:
            outcome = sync_package(candidate, repo=target_repo, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001
            summary.failed_packages += 1
            logger.exception("Failed to sync %s: %s", candidate.tooth, exc)
            continue

        if outcome == "skip_closed":
            summary.skipped_closed += 1
            continue
        if outcome == "noop":
            summary.skipped_no_change += 1
            continue
        if outcome == "create":
            summary.created += 1
            acted += 1
            continue
        if outcome == "update":
            summary.updated += 1
            acted += 1
            continue

        logger.warning("Unhandled sync outcome for %s: %s", candidate.tooth, outcome)

    logger.info(
        "Summary: repos=%s missing_packages=%s created=%s updated=%s skipped_closed=%s skipped_no_change=%s failed=%s",
        summary.repos_discovered,
        summary.missing_packages_found,
        summary.created,
        summary.updated,
        summary.skipped_closed,
        summary.skipped_no_change,
        summary.failed_packages,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

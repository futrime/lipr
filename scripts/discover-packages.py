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
DEFAULT_MAX_PACKAGES = 256
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
class RepoVersionCandidate:
    repo: str
    version: str


@dataclass(frozen=True)
class ManifestCandidate:
    branch: str
    manifest: ManifestRecord


@dataclass
class Summary:
    repos_discovered: int = 0
    missing_versions_found: int = 0
    created: int = 0
    skipped_existing_local: int = 0
    skipped_existing_pr: int = 0
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


def is_github_search_rate_limited(exc: Exception) -> bool:
    message = str(exc).lower()
    return "search/code" in message and (
        "429" in message or "rate limit" in message or "secondary rate limit" in message
    )


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


def list_existing_package_keys() -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for path in PACKAGES_DIR.rglob("tooth.json"):
        rel_parent = path.relative_to(PACKAGES_DIR).parent.as_posix()
        if "@" not in rel_parent:
            continue
        package_id, version = rel_parent.rsplit("@", 1)
        keys.add((package_id, version))
    return keys


def package_id_from_repo(repo: str) -> str:
    return f"github.com/{repo}"


def branch_name_for_package(package_id: str, version: str) -> str:
    return f"package/{package_id}@{version}"


def gh_api_json(path: str, *, params: dict[str, str] | None = None) -> Any:
    args = ["gh", "api", path, "-X", "GET"]
    for key, value in (params or {}).items():
        args.extend(["-f", f"{key}={value}"])
    completed = run_command(args)
    return json.loads(completed.stdout)


def gh_search_code_json(*, params: dict[str, str]) -> Any:
    while True:
        try:
            return gh_api_json("search/code", params=params)
        except Exception as exc:  # noqa: BLE001
            if not is_github_search_rate_limited(exc):
                raise
            logger.warning(
                "GitHub search is rate limited for query %s; retrying in 60 seconds",
                params.get("q", ""),
            )
            time.sleep(60)


def discover_repositories() -> list[str]:
    repos: set[str] = set()
    page = 1
    total_count: int | None = None
    incomplete_results = False

    while True:
        data = retry_call(
            f"search page {page}",
            lambda: gh_search_code_json(
                params={"q": SEARCH_QUERY, "per_page": "100", "page": str(page)}
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


def list_pr_history_branches(repo: str) -> set[str]:
    completed = run_command(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--limit",
            "1000",
            "--json",
            "headRefName",
        ],
        cwd=ROOT_DIR,
    )
    items = json.loads(completed.stdout)
    return {
        head_ref
        for item in items
        if isinstance((head_ref := item.get("headRefName")), str) and head_ref
    }


def build_pr_body(candidate: ManifestCandidate) -> str:
    manifest = candidate.manifest
    return (
        f"## Summary\n"
        f"Add missing manifest for `{manifest.tooth}@{manifest.version}`.\n\n"
        f"## Source Repository\n"
        f"- `github.com/{manifest.repo}`\n"
    )


def stage_package_manifest(
    worktree_dir: Path, candidate: ManifestCandidate
) -> Path | None:
    manifest = candidate.manifest
    destination = package_manifest_path(worktree_dir, manifest.tooth, manifest.version)
    existing_bytes = destination.read_bytes() if destination.exists() else None
    if existing_bytes == manifest.content:
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(manifest.content)
    return destination.relative_to(worktree_dir)


def commit_and_push(
    candidate: ManifestCandidate,
    *,
    base_ref: str,
    repo: str,
    dry_run: bool,
) -> str:
    manifest = candidate.manifest
    branch = candidate.branch
    title = f"feat: add {manifest.tooth}@{manifest.version} package"
    body = build_pr_body(candidate)

    if dry_run:
        logger.info(
            "[dry-run] Would create PR for %s@%s on branch %s",
            manifest.tooth,
            manifest.version,
            branch,
        )
        return "create"

    worktree_dir = Path(tempfile.mkdtemp(prefix="lipr-worktree-"))
    try:
        run_command(
            ["git", "worktree", "add", "--detach", str(worktree_dir), base_ref],
            cwd=ROOT_DIR,
        )
        run_command(["git", "switch", "-C", branch], cwd=worktree_dir)

        changed_path = stage_package_manifest(worktree_dir, candidate)
        if changed_path is None:
            logger.info(
                "No new manifest changes for %s@%s", manifest.tooth, manifest.version
            )
            return "noop"

        run_command(
            ["git", "add", "--", changed_path.as_posix()],
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
        logger.info("Created PR for %s@%s", manifest.tooth, manifest.version)
        return "create"
    finally:
        run_command(
            ["git", "worktree", "remove", "--force", str(worktree_dir)],
            cwd=ROOT_DIR,
            check=False,
        )
        shutil.rmtree(worktree_dir, ignore_errors=True)


def discover_package(candidate: ManifestCandidate, *, repo: str, dry_run: bool) -> str:
    run_command(["git", "fetch", "origin", "main"], cwd=ROOT_DIR)

    return commit_and_push(
        candidate,
        base_ref="origin/main",
        repo=repo,
        dry_run=dry_run,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    args = parse_args()

    require_command("gh")
    require_command("git")
    require_command("npx")

    target_repo = infer_repository_name()
    existing_package_keys = list_existing_package_keys()
    repositories = discover_repositories()
    pr_history_branches = list_pr_history_branches(target_repo)

    summary = Summary(repos_discovered=len(repositories))

    acted = 0
    for repo in repositories:
        if acted >= args.max_packages:
            break
        try:
            versions = list_repo_versions(repo)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to list versions for github.com/%s: %s", repo, exc)
            continue

        for version in versions:
            if acted >= args.max_packages:
                break

            candidate = RepoVersionCandidate(repo=repo, version=version)
            package_id = package_id_from_repo(candidate.repo)
            package_key = (package_id, candidate.version)
            branch = branch_name_for_package(package_id, candidate.version)

            if package_key in existing_package_keys:
                summary.skipped_existing_local += 1
                continue

            if branch in pr_history_branches:
                summary.skipped_existing_pr += 1
                continue

            try:
                manifest = fetch_manifest(candidate.repo, candidate.version)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Skipping github.com/%s@%s: %s",
                    candidate.repo,
                    candidate.version,
                    exc,
                )
                continue

            manifest_key = (manifest.tooth, manifest.version)
            if manifest_key in existing_package_keys:
                summary.skipped_existing_local += 1
                continue

            manifest_branch = branch_name_for_package(manifest.tooth, manifest.version)
            if manifest_branch in pr_history_branches:
                summary.skipped_existing_pr += 1
                continue

            summary.missing_versions_found += 1
            prepared_candidate = ManifestCandidate(branch=branch, manifest=manifest)

            try:
                outcome = discover_package(
                    prepared_candidate,
                    repo=target_repo,
                    dry_run=args.dry_run,
                )
            except Exception as exc:  # noqa: BLE001
                summary.failed_packages += 1
                logger.exception(
                    "Failed to sync %s@%s: %s", manifest.tooth, manifest.version, exc
                )
                continue

            if outcome == "noop":
                continue
            if outcome == "create":
                summary.created += 1
                acted += 1
                existing_package_keys.add(package_key)
                existing_package_keys.add(manifest_key)
                pr_history_branches.add(branch)
                pr_history_branches.add(manifest_branch)
                continue

            logger.warning(
                "Unhandled sync outcome for %s@%s: %s",
                manifest.tooth,
                manifest.version,
                outcome,
            )

    logger.info(
        "Summary: repos=%s missing_versions=%s created=%s skipped_existing_local=%s skipped_existing_pr=%s failed=%s",
        summary.repos_discovered,
        summary.missing_versions_found,
        summary.created,
        summary.skipped_existing_local,
        summary.skipped_existing_pr,
        summary.failed_packages,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

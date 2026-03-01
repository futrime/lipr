import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Final

import semver
from httpx import URL, Client
from tenacity import retry, stop_after_attempt, wait_exponential

from entities import (
    Index,
    IndexForLeviLauncher,
    IndexPackage,
    IndexPackageForLeviLauncher,
    IndexVariant,
    IndexVariantForLeviLauncher,
    IndexVersionForLeviLauncher,
    Manifest,
)

logger = logging.getLogger(__name__)

_BASE_DIR: Final = Path("./workspace/lipr/github.com")


@dataclass(frozen=True)
class _RepoInfo:
    stargazer_count: int
    updated_at: str


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    _cleanup()

    with Client() as client:
        packages: dict[str, IndexPackage] = {}
        packages_for_levilauncher: dict[str, IndexPackageForLeviLauncher] = {}

        for repo in _discover_repos():
            try:
                repo_info = _fetch_repo_info(repo)
                head_manifest = _fetch_manifest(repo, "HEAD", client=client)
                versions = _fetch_versions(repo)

                manifests: list[Manifest] = []
                for ver in versions:
                    try:
                        manifest = _download_and_save_version_manifest(
                            repo, ver, client=client
                        )

                        manifests.append(manifest)

                    except Exception as e:
                        logger.exception(
                            f"Failed to process version '{ver}' of repository 'github.com/{repo}'",
                            exc_info=e,
                        )

                variants = set(v.label for m in manifests for v in m.variants)
                packages[repo] = IndexPackage(
                    info=head_manifest.info,
                    stargazer_count=repo_info.stargazer_count,
                    updated_at=repo_info.updated_at,
                    variants={
                        var: IndexVariant(
                            versions=[
                                m.version
                                for m in manifests
                                if any(v.label == var for v in m.variants)
                            ]
                        )
                        for var in variants
                    },
                )
                packages_for_levilauncher[repo] = IndexPackageForLeviLauncher(
                    info=head_manifest.info,
                    stargazer_count=repo_info.stargazer_count,
                    updated_at=repo_info.updated_at,
                    variants={
                        var: IndexVariantForLeviLauncher(
                            versions={
                                m.version: IndexVersionForLeviLauncher(
                                    dependencies={
                                        k: v
                                        for variant in m.variants
                                        if variant.label == var
                                        for k, v in variant.dependencies.items()
                                    },
                                )
                                for m in manifests
                                if any(v.label == var for v in m.variants)
                            }
                        )
                        for var in variants
                    },
                )

            except Exception as e:
                logger.exception(
                    f"Failed to process repository 'github.com/{repo}'", exc_info=e
                )

    index = Index(packages=packages)

    _save_index(index)

    index_for_levilauncher = IndexForLeviLauncher(packages=packages_for_levilauncher)

    _save_index_for_levilauncher(index_for_levilauncher)


def _cleanup() -> None:
    if _BASE_DIR.exists():
        shutil.rmtree(_BASE_DIR)

    _BASE_DIR.mkdir(parents=True, exist_ok=True)

    logger.info(f"Cleaned up base directory '{_BASE_DIR}'")


@retry(
    reraise=True,
    stop=stop_after_attempt(10),
    wait=wait_exponential(max=60),
)
def _discover_repos() -> list[str]:
    completed_process = subprocess.run(
        [
            "gh",
            "search",
            "code",
            "format_version",
            "path:/",
            "filename:tooth.json",
            "--limit=1000",
            "--json",
            "repository",
        ],
        capture_output=True,
        check=True,
        text=True,
    )

    results: list[dict[str, dict[str, str]]] = json.loads(completed_process.stdout)

    logger.info(f"Discovered {len(results)} repositories")

    return [r["repository"]["nameWithOwner"] for r in results]


@retry(
    reraise=True,
    stop=stop_after_attempt(10),
    wait=wait_exponential(max=60),
)
def _fetch_repo_info(repo: str) -> _RepoInfo:
    completed_process = subprocess.run(
        [
            "gh",
            "repo",
            "view",
            repo,
            "--json=stargazerCount,updatedAt",
        ],
        capture_output=True,
        check=True,
        text=True,
    )

    result: dict[str, Any] = json.loads(completed_process.stdout)

    logger.info(f"Fetched repository info for 'github.com/{repo}'")

    return _RepoInfo(
        stargazer_count=result["stargazerCount"],
        updated_at=result["updatedAt"],
    )


def _fetch_versions(repo: str) -> list[str]:
    url = f"https://github.com/{repo}.git"

    completed_process = subprocess.run(
        ["git", "ls-remote", "-t", "--refs", url],
        capture_output=True,
        check=True,
        text=True,
    )

    versions = []
    for line in completed_process.stdout.splitlines():
        ref = line.split()[1]

        if not ref.startswith("refs/tags/v"):
            continue

        ver = ref.removeprefix("refs/tags/v")

        if not semver.Version.is_valid(ver):
            continue

        versions.append(ver)

    logger.info(f"Found {len(versions)} versions for 'github.com/{repo}'")

    return versions


def _download_and_save_version_manifest(
    repo: str, version: str, *, client: Client
) -> Manifest:
    manifest = _fetch_manifest(repo, f"v{version}", client=client)

    path = _BASE_DIR / f"{repo}@{version}" / "tooth.json"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(manifest.model_dump_json().encode("utf-8"))

    logger.info(f"Downloaded and saved manifest for 'github.com/{repo}@{version}'")

    return manifest


def _fetch_manifest(repo: str, ref: str, *, client: Client) -> Manifest:
    url = URL(f"https://raw.githubusercontent.com/{repo}/{ref}/tooth.json")

    response = client.get(url)
    response.raise_for_status()

    # Always migrate manifest to ensure it's in correct format.
    with TemporaryDirectory() as temp_dir:
        legacy_path = Path(temp_dir) / "legacy"
        legacy_path.write_bytes(response.content)

        current_path = Path(temp_dir) / "current"

        subprocess.run(
            ["lip", "migrate", str(legacy_path), str(current_path)], check=True
        )

        content = current_path.read_bytes()

    manifest = Manifest.model_validate_json(content)

    logger.info(f"Fetched manifest for 'github.com/{repo}@{ref}'")

    return manifest


def _save_index(index: Index) -> None:
    path = _BASE_DIR / "index.json"
    path.write_bytes(index.model_dump_json().encode("utf-8"))

    logger.info(f"Saved index to '{path}'")


def _save_index_for_levilauncher(index: IndexForLeviLauncher) -> None:
    path = _BASE_DIR / "levilauncher.json"
    path.write_bytes(index.model_dump_json().encode("utf-8"))

    logger.info(f"Saved index for LeviLauncher to '{path}'")


if __name__ == "__main__":
    main()

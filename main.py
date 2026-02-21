import logging
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from git.cmd import Git
from github import Github
from github.Auth import Token
from github.GithubException import GithubException
from github.Repository import Repository
from httpx import URL, Client
from pydantic_extra_types.semantic_version import SemanticVersion

from entities import (
    PackageIndex,
    PackageIndexPackage,
    PackageManifest,
    PackageManifestVariantLabel,
)

BASE_DIR: Final = Path("./workspace/lipr/github.com")


def download_manifest(
    repo: str, version: SemanticVersion | None, *, client: Client
) -> PackageManifest:
    if version is None:
        url = URL(f"https://raw.githubusercontent.com/{repo}/HEAD/tooth.json")
    else:
        url = URL(f"https://raw.githubusercontent.com/{repo}/v{version}/tooth.json")

    response = client.get(url)
    response.raise_for_status()

    # Migrate manifest.
    with TemporaryDirectory() as tmp_dir:
        tmp_path_1 = Path(tmp_dir) / "old"
        tmp_path_1.write_bytes(response.content)

        tmp_path_2 = Path(tmp_dir) / "new"

        subprocess.run(["lip", "migrate", str(tmp_path_1), str(tmp_path_2)], check=True)

        content = tmp_path_2.read_bytes()

    manifest = PackageManifest.model_validate_json(content)

    logging.info(
        f"Fetched manifest for github.com/{repo}" + (f"@{version}" if version else "")
    )

    if version is not None:
        path = BASE_DIR / repo / "@v" / str(version) / "tooth.json"
        path.parent.mkdir(parents=True, exist_ok=True)

        path.write_bytes(content)

        logging.info(f"Saved manifest file at {path}")

    return manifest


def fetch_versions(repo: str, *, git: Git) -> list[SemanticVersion]:
    url = URL(f"https://github.com/{repo}.git")

    result: str = git.ls_remote("-t", "--refs", url)

    versions = []
    for line in result.splitlines():
        ref = line.split()[1]

        if not ref.startswith("refs/tags/v"):
            continue

        ver_str = ref.removeprefix("refs/tags/v")

        try:
            ver = SemanticVersion.parse(ver_str)
        except ValueError:
            continue

        versions.append(ver)

    versions.sort()

    logging.info(f"Fetched {len(versions)} versions for github.com/{repo}")

    return versions


def save_index_file(index: PackageIndex) -> None:
    path = Path("./workspace/lipr/index.json")
    path.parent.mkdir(parents=True, exist_ok=True)

    content = index.model_dump_json(ensure_ascii=False, indent=2)

    path.write_text(content, encoding="utf-8")

    logging.info(f"Saved index file at {path}")


def search_repositories() -> Iterator[Repository]:
    if (token := os.getenv("GITHUB_TOKEN")) is None:
        raise RuntimeError("GITHUB_TOKEN environment variable is not set")

    with Github(auth=Token(token), per_page=100) as github:
        pagination = github.search_code("", filename="tooth.json", path="/")

        page_idx = 0
        while True:
            try:
                page = pagination.get_page(page_idx)

                if not page:
                    break

                yield from (item.repository for item in page)

                page_idx += 1

            except GithubException as ex:
                if ex.status != 429:
                    raise

                sleep_time = (
                    github.get_rate_limit().resources.search.reset
                    - datetime.now(timezone.utc)
                ).total_seconds() + 1

                logging.warning(
                    f"Exceeded GitHub API rate limit. Waiting {sleep_time} seconds for reset..."
                )

                time.sleep(sleep_time)

    logging.info(f"Found {pagination.totalCount} repositories")


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    # Clean up workspace.
    if BASE_DIR.exists():
        shutil.rmtree(BASE_DIR)
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    git = Git()

    index = PackageIndex(packages={})

    with Client() as client:
        for repo in search_repositories():
            try:
                head_mft = download_manifest(
                    repo.full_name, version=None, client=client
                )
            except Exception as ex:
                logging.error(
                    f"Failed to fetch manifest for github.com/{repo.full_name}: {ex}"
                )
                continue

            try:
                updated = repo.get_latest_release().published_at
            except Exception as ex:
                logging.error(
                    f"Failed to fetch latest release for github.com/{repo.full_name}: {ex}"
                )
                continue

            try:
                versions = fetch_versions(repo.full_name, git=git)
            except Exception as ex:
                logging.error(
                    f"Failed to fetch versions for github.com/{repo.full_name}: {ex}"
                )
                continue

            index_pkg_versions: dict[
                SemanticVersion, list[PackageManifestVariantLabel]
            ] = {}

            for ver in versions:
                try:
                    manifest = download_manifest(repo.full_name, ver, client=client)
                except Exception as ex:
                    logging.error(
                        f"Failed to fetch manifest for github.com/{repo.full_name}@{ver}: {ex}"
                    )
                    continue

                if len(manifest.variants) == 0:
                    logging.warning(
                        f"No variants found in manifest for github.com/{repo.full_name}@{ver}. Skipping..."
                    )
                    continue

                index_pkg_versions[ver] = [
                    variant.label for variant in manifest.variants
                ]

            if len(index_pkg_versions) == 0:
                logging.warning(
                    f"No valid versions found for github.com/{repo.full_name}. Skipping..."
                )
                continue

            index.packages[f"github.com/{repo.full_name}"] = PackageIndexPackage(
                info=head_mft.info,
                updated_at=updated,
                stars=repo.stargazers_count,
                versions=index_pkg_versions,
            )

    save_index_file(index)


if __name__ == "__main__":
    main()

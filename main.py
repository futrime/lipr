import logging
import os
import shutil
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from git.cmd import Git
from github import Github
from github.Auth import Token
from github.GithubException import GithubException
from httpx import URL, Client
from pydantic import BaseModel, Field, RootModel
from semver import Version

BASE_DIR: Final = Path("./workspace/lipr/github.com")


class PackageInfo(BaseModel):
    name: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    avatar_url: str = ""


class PackageIndexEntry(BaseModel):
    info: PackageInfo
    versions: list[Version] = Field(default_factory=list)


class PackageIndex(RootModel):
    root: dict[str, PackageIndexEntry] = Field(default_factory=dict)


class PackageManifest(BaseModel):
    format_version: int
    format_uuid: str
    tooth: str
    version: Version
    info: PackageInfo = Field(default_factory=PackageInfo)


def fetch_manifest(
    repo: str, version: Version | None, *, client: Client
) -> PackageManifest:
    if version is None:
        url = URL(f"https://raw.githubusercontent.com/{repo}/HEAD/tooth.json")
    else:
        url = URL(f"https://raw.githubusercontent.com/{repo}/v{version}/tooth.json")

    response = client.get(url)

    response.raise_for_status()

    manifest = PackageManifest.model_validate_json(response.content)

    logging.info(
        f"Fetched manifest for github.com/{repo}" + (f"@{version}" if version else "")
    )

    return manifest


def fetch_versions(repo: str, *, git: Git) -> list[Version]:
    url = URL(f"https://github.com/{repo}.git")

    result: str = git.ls_remote("-t", "--refs", url)

    versions = []
    for line in result.splitlines():
        ref = line.split()[1]

        if not ref.startswith("refs/tags/v"):
            continue

        ver_str = ref.removeprefix("refs/tags/v")

        try:
            ver = Version.parse(ver_str)
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


def save_manifest_file(manifest: PackageManifest, repo: str, version: Version) -> None:
    path = BASE_DIR / repo / str(version) / "tooth.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    content = manifest.model_dump_json(ensure_ascii=False, indent=2)

    path.write_text(content, encoding="utf-8")

    logging.info(f"Saved manifest file for github.com/{repo}@{version} at {path}")


def search_repositories() -> Iterator[str]:
    if (token := os.getenv("GITHUB_TOKEN")) is None:
        raise RuntimeError("GITHUB_TOKEN environment variable is not set")

    with Github(auth=Token(token), per_page=100) as github:
        pagination = github.search_code(
            "289f771f-2c9a-4d73-9f3f-8492495a924d", filename="tooth.json", path="/"
        )

        page_idx = 0
        while True:
            try:
                page = pagination.get_page(page_idx)

                if not page:
                    break

                yield from (item.repository.full_name for item in page)

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

    infos: dict[str, PackageInfo] = {}
    versions: dict[str, list[Version]] = {}

    with Client() as client:
        for repo in search_repositories():
            try:
                manifest = fetch_manifest(repo, version=None, client=client)
            except Exception as ex:
                logging.error(f"Failed to fetch manifest for github.com/{repo}: {ex}")
                continue

            infos[repo] = manifest.info

            for ver in fetch_versions(repo, git=git):
                try:
                    manifest = fetch_manifest(repo, ver, client=client)
                except Exception as ex:
                    logging.error(
                        f"Failed to fetch manifest for github.com/{repo}@{ver}: {ex}"
                    )
                    continue

                save_manifest_file(manifest, repo, ver)

                versions.setdefault(repo, []).append(ver)

    index = PackageIndex(
        {
            repo: PackageIndexEntry(info=infos[repo], versions=vers)
            for repo, vers in versions.items()
        }
    )

    save_index_file(index)


if __name__ == "__main__":
    main()

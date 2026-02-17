import asyncio
from datetime import datetime
import json
import logging
import os
import shutil
from asyncio import Semaphore
from pathlib import Path
from typing import Final

import aiofiles
import aiofiles.os
from git.cmd import Git
from github import Github, RateLimitExceededException
from github.Auth import Token
from httpx import URL, AsyncClient
from pydantic import BaseModel, Field
from semver import VersionInfo

BASE_DIR: Final = Path("./workspace/lipr/github.com")
MAX_CONCURRENCY: Final = 16
UUID: Final = "289f771f-2c9a-4d73-9f3f-8492495a924d"


class PackageManifestInfo(BaseModel):
    name: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    avatar_url: str = ""


class PackageManifest(BaseModel):
    format_version: int
    format_uuid: str
    tooth: str
    version: str
    info: PackageManifestInfo = Field(default_factory=PackageManifestInfo)


async def fetch_manifest(
    repository: str, ref: str, *, client: AsyncClient, semaphore: Semaphore
) -> PackageManifest | None:
    try:
        manifest_url = URL(
            f"https://raw.githubusercontent.com/{repository}/{ref}/tooth.json"
        )

        async with semaphore:
            response = await client.get(manifest_url)

        response.raise_for_status()

        manifest = PackageManifest.model_validate_json(response.content)

        logging.info(f"Fetched manifest for {repository}@{ref}")

        return manifest

    except Exception as e:
        logging.error(f"Cannot fetch manifest for {repository}@{ref}: {e}")

        return None


async def fetch_versions(repository: str, *, semaphore: Semaphore) -> list[VersionInfo]:
    try:
        git = Git()

        repository_url = URL(f"https://github.com/{repository}.git")

        async with semaphore:
            result: str = await asyncio.to_thread(
                git.ls_remote, "-t", "--refs", repository_url
            )

        versions = []
        for line in result.splitlines():
            ref = line.split()[1]

            if not ref.startswith("refs/tags/v"):
                continue

            version_str = ref.removeprefix("refs/tags/v")

            try:
                versions.append(VersionInfo.parse(version_str))

            except ValueError:
                continue

        sorted_versions = sorted(versions)

        logging.info(f"Fetched versions for {repository}")

        return sorted_versions

    except Exception as e:
        logging.error(f"Cannot fetch versions for {repository}: {e}")

        return []


async def generate_index_file(
    repositories: list[str],
    versions: list[list[VersionInfo]],
    *,
    client: AsyncClient,
    semaphore: Semaphore,
) -> None:
    async with asyncio.TaskGroup() as tg:
        tasks = [
            tg.create_task(
                fetch_manifest(repo, "HEAD", client=client, semaphore=semaphore)
            )
            for repo in repositories
        ]

    manifests = [t.result() for t in tasks]

    index = {
        f"github.com/{repo}": {
            "info": mft.info.model_dump(),
            "versions": [str(ver) for ver in vers],
        }
        for repo, vers, mft in zip(repositories, versions, manifests)
        if mft is not None
    }

    content = json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True)

    index_path = BASE_DIR / "index.json"
    await aiofiles.os.makedirs(index_path.parent, exist_ok=True)
    async with aiofiles.open(index_path, "w", encoding="utf-8") as f:
        await f.write(content)

    logging.info("Generated index file")


async def save_manifest(
    repository: str, version: VersionInfo, *, client: AsyncClient, semaphore: Semaphore
) -> None:
    manifest = await fetch_manifest(
        repository, f"v{version}", client=client, semaphore=semaphore
    )

    if manifest is None:
        return

    content = manifest.model_dump_json(ensure_ascii=False, indent=2)

    path = BASE_DIR / repository / str(version) / "tooth.json"
    await aiofiles.os.makedirs(path.parent, exist_ok=True)
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(content)

    logging.info(f"Saved manifest for {repository}@{version}")


async def search_repositories(
    *,
    semaphore: Semaphore,
) -> list[str]:
    if (token := os.getenv("GITHUB_TOKEN")) is None:
        raise RuntimeError("GITHUB_TOKEN environment variable is not set")

    github = Github(auth=Token(token), per_page=100)
    pagination = github.search_code(UUID, filename="tooth.json", path="/")

    page_idx = 0
    results = []
    while True:
        try:
            async with semaphore:
                page = await asyncio.to_thread(pagination.get_page, page_idx)

            if not page:
                break

            results.extend(item.repository.full_name for item in page)

            page_idx += 1

            await asyncio.sleep(3)

        except RateLimitExceededException:
            logging.warning("Exceeded GitHub API rate limit. Waiting for reset...")

            reset_time = github.get_rate_limit().resources.search.reset
            sleep_time = (reset_time - datetime.now()).total_seconds() + 1
            await asyncio.sleep(sleep_time)

    return results


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    if BASE_DIR.exists():
        shutil.rmtree(BASE_DIR)
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    semaphore = Semaphore(MAX_CONCURRENCY)

    async with AsyncClient() as client:
        repositories = await search_repositories(
            semaphore=semaphore,
        )

        async with asyncio.TaskGroup() as tg:
            tasks = [
                tg.create_task(fetch_versions(repo, semaphore=semaphore))
                for repo in repositories
            ]
        versions = [t.result() for t in tasks]

        await generate_index_file(
            repositories,
            versions,
            client=client,
            semaphore=semaphore,
        )

        await asyncio.gather(
            *[
                save_manifest(
                    repo,
                    v,
                    client=client,
                    semaphore=semaphore,
                )
                for repo, vers in zip(
                    repositories,
                    versions,
                )
                for v in vers
            ]
        )


if __name__ == "__main__":
    asyncio.run(main())

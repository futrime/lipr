import json
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Final, NamedTuple

from httpx import URL, Client
from pydantic import ValidationError
from pydantic_extra_types.semantic_version import SemanticVersion

from entities import (
    PackageIndex,
    PackageIndexPackage,
    PackageManifest,
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

    content = response.content

    try:
        manifest = PackageManifest.model_validate_json(content)

    except ValidationError:
        logging.warning("Manifest validation failed. Attempting migration...")

        # Migrate manifest.
        with TemporaryDirectory() as tmp_dir:
            tmp_path_1 = Path(tmp_dir) / "old"
            tmp_path_1.write_bytes(content)

            tmp_path_2 = Path(tmp_dir) / "new"

            subprocess.run(
                ["lip", "migrate", str(tmp_path_1), str(tmp_path_2)], check=True
            )

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


def fetch_repos() -> list[str]:
    stdout = subprocess.run(
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
    ).stdout

    results: list[dict[str, dict[str, str]]] = json.loads(stdout)

    logging.info(f"Found {len(results)} repositories")

    return [r["repository"]["nameWithOwner"] for r in results]


class RepoDetails(NamedTuple):
    stars: int
    updated_at: datetime


def fetch_repo_details(repo: str) -> RepoDetails:
    stdout = subprocess.run(
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
    ).stdout

    result: dict[str, Any] = json.loads(stdout)

    return RepoDetails(
        stars=result["stargazerCount"],
        updated_at=datetime.fromisoformat(result["updatedAt"]),
    )


def fetch_versions(repo: str) -> list[SemanticVersion]:
    url = f"https://github.com/{repo}.git"

    stdout = subprocess.run(
        ["git", "ls-remote", "-t", "--refs", url],
        capture_output=True,
        check=True,
        text=True,
    ).stdout

    versions = []
    for line in stdout.splitlines():
        ref = line.split()[1]

        # Skip refs that is not a version tag.
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

    content = index.model_dump_json(ensure_ascii=False, exclude_unset=True, indent=2)

    path.write_text(content, encoding="utf-8")

    logging.info(f"Saved index file at {path}")


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    # Clean up workspace.
    if BASE_DIR.exists():
        shutil.rmtree(BASE_DIR)
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    index = PackageIndex(packages={})

    with Client() as client:
        for repo in fetch_repos():
            try:
                head_manifest = download_manifest(
                    repo, version=None, client=client
                )
            except Exception as ex:
                logging.error(
                    f"Failed to fetch manifest for github.com/{repo}: {ex}"
                )
                continue

            try:
                repo_details = fetch_repo_details(repo)
            except Exception as ex:
                logging.error(
                    f"Failed to fetch repo details for github.com/{repo}: {ex}"
                )
                continue

            package = PackageIndexPackage(
                info=head_manifest.info,
                stars=repo_details.stars,
                updated_at=repo_details.updated_at,
                versions={},
            )

            try:
                versions = fetch_versions(repo)
            except Exception as ex:
                logging.error(
                    f"Failed to fetch versions for github.com/{repo}: {ex}"
                )
                continue

            for ver in versions:
                try:
                    manifest = download_manifest(repo, ver, client=client)
                except Exception as ex:
                    logging.error(
                        f"Failed to fetch manifest for github.com/{repo}@{ver}: {ex}"
                    )
                    continue

                if len(manifest.variants) == 0:
                    logging.warning(
                        f"No variants found in manifest for github.com/{repo}@{ver}. Skipping..."
                    )
                    continue

                package.versions[ver] = [variant.label for variant in manifest.variants]

            if len(package.versions) == 0:
                logging.warning(
                    f"No valid versions found for github.com/{repo}. Skipping..."
                )
                continue

            index.packages[f"github.com/{repo}"] = package

    save_index_file(index)


if __name__ == "__main__":
    main()

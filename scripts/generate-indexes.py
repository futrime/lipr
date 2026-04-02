#!/usr/bin/env python3

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
PACKAGES_DIR = ROOT_DIR / "packages"
INDEX_FILE = ROOT_DIR / "index.json"
LEVILAUNCHER_FILE = ROOT_DIR / "levilauncher.json"
VERSION_PART_RE = re.compile(r"[0-9]+|[A-Za-z]+")
VERSION_RE = re.compile(r"^(?P<core>[^-]+)(?:-(?P<pre>.*))?$")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_text(f"{serialized}\n", encoding="utf-8")


def default_if_none(value: Any, default: Any) -> Any:
    return default if value is None else value


def normalize_info(info: Any) -> dict[str, Any]:
    if not isinstance(info, dict):
        info = {}
    tags = info.get("tags")
    if not isinstance(tags, list):
        tags = []
    return {
        "name": default_if_none(info.get("name"), ""),
        "description": default_if_none(info.get("description"), ""),
        "tags": tags,
        "avatar_url": default_if_none(info.get("avatar_url"), ""),
    }


def normalize_deps(deps: Any) -> dict[str, Any]:
    if not isinstance(deps, dict):
        return {}
    return {key: deps[key] for key in sorted(key for key in deps if key)}


def version_token(token: str) -> tuple[int, Any]:
    if token.isdigit():
        return (0, int(token))
    return (1, token)


def version_key(version: str) -> tuple[Any, ...]:
    match = VERSION_RE.match(version)
    if match is None:
        core = version
        pre = ""
    else:
        core = match.group("core")
        pre = match.group("pre") or ""

    core_tokens = [version_token(token) for token in VERSION_PART_RE.findall(core)]
    pre_tokens = [version_token(token) for token in VERSION_PART_RE.findall(pre)]
    return (core_tokens, 1 if not pre else 0, pre_tokens, version)


def package_meta(
    existing_index: dict[str, Any], existing_levilauncher: dict[str, Any], name: str
) -> dict[str, Any]:
    index_packages = existing_index.get("packages")
    levi_packages = existing_levilauncher.get("packages")
    index_meta = index_packages.get(name, {}) if isinstance(index_packages, dict) else {}
    levi_meta = levi_packages.get(name, {}) if isinstance(levi_packages, dict) else {}
    return {
        "stargazer_count": index_meta.get(
            "stargazer_count", levi_meta.get("stargazer_count", 0)
        ),
        "updated_at": index_meta.get("updated_at", levi_meta.get("updated_at", "")),
    }


def root_template(existing: dict[str, Any]) -> dict[str, Any]:
    return {
        "format_version": existing.get("format_version", 3),
        "format_uuid": existing.get(
            "format_uuid", "289f771f-2c9a-4d73-9f3f-8492495a924d"
        ),
    }


def ordered_variants(
    variants: dict[str, dict[str, Any]], for_levilauncher: bool
) -> dict[str, dict[str, Any]]:
    ordered: dict[str, dict[str, Any]] = {}
    for label, variant in sorted(
        variants.items(), key=lambda item: (0 if item[0] == "" else 1, item[0])
    ):
        versions = variant.get("versions", {})
        sorted_versions = sorted(versions, key=version_key)
        if for_levilauncher:
            version_map = {
                version: {
                    "dependencies": normalize_deps(
                        versions[version].get("dependencies", {})
                    )
                }
                for version in sorted_versions
            }
        else:
            version_map = sorted_versions
        ordered[label] = {"versions": version_map}
    return ordered


def aggregate_packages(
    tooths: list[dict[str, Any]],
    existing_index: dict[str, Any],
    existing_levilauncher: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    packages: dict[str, dict[str, Any]] = {}

    for tooth in tooths:
        name = tooth["tooth"]
        version = tooth["version"]
        package = packages.setdefault(
            name,
            {
                **package_meta(existing_index, existing_levilauncher, name),
                "info": normalize_info(tooth.get("info", {})),
                "variants": {},
            },
        )
        package["info"] = normalize_info(tooth.get("info", {}))

        variants = tooth.get("variants") or []
        for variant in variants:
            label = variant.get("label")
            if label is None:
                label = ""
            variant_entry = package["variants"].setdefault(label, {"versions": {}})
            version_entry = variant_entry["versions"].setdefault(
                version, {"dependencies": {}}
            )
            version_entry["dependencies"].update(
                normalize_deps(variant.get("dependencies", {}))
            )

    return packages


def build_outputs(
    tooths: list[dict[str, Any]],
    existing_index: dict[str, Any],
    existing_levilauncher: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    packages = aggregate_packages(tooths, existing_index, existing_levilauncher)
    sorted_package_names = sorted(packages)

    index_root = root_template(existing_index or existing_levilauncher)
    levi_root = root_template(existing_levilauncher or existing_index)

    index_packages = {
        name: {
            "stargazer_count": packages[name]["stargazer_count"],
            "updated_at": packages[name]["updated_at"],
            "info": packages[name]["info"],
            "variants": ordered_variants(packages[name]["variants"], False),
        }
        for name in sorted_package_names
    }
    levi_packages = {
        name: {
            "stargazer_count": packages[name]["stargazer_count"],
            "updated_at": packages[name]["updated_at"],
            "info": packages[name]["info"],
            "variants": ordered_variants(packages[name]["variants"], True),
        }
        for name in sorted_package_names
    }

    return (
        {**index_root, "packages": index_packages},
        {**levi_root, "packages": levi_packages},
    )


def main() -> int:
    tooth_paths = sorted(PACKAGES_DIR.rglob("tooth.json"), key=lambda path: path.as_posix())
    if not tooth_paths:
        print(f"No tooth.json files found under {PACKAGES_DIR}", file=sys.stderr)
        return 1

    tooths = [load_json(path) for path in tooth_paths]
    existing_index = load_json(INDEX_FILE)
    existing_levilauncher = load_json(LEVILAUNCHER_FILE)
    index_data, levi_data = build_outputs(tooths, existing_index, existing_levilauncher)

    write_json(INDEX_FILE, index_data)
    write_json(LEVILAUNCHER_FILE, levi_data)

    print(
        f"Generated index.json and levilauncher.json from {len(tooth_paths)} tooth files."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

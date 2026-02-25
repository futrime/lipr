from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints
from pydantic_extra_types.semantic_version import SemanticVersion

type FormatUUID = Literal["289f771f-2c9a-4d73-9f3f-8492495a924d"]
type FormatVersion = Literal[3]
type PackageManifestVariantLabel = Annotated[
    str, StringConstraints(pattern=r"^([a-z0-9_]+(/[a-z0-9_]+)?)?$")
]
type PackageManifestInfoTag = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9-]+(:[a-z0-9-]+)?$")
]


class PackageManifestInfo(BaseModel):
    name: str = ""
    description: str = ""
    tags: list[PackageManifestInfoTag] = []
    avatar_url: str = ""


class PackageManifestVariant(BaseModel):
    label: PackageManifestVariantLabel = ""


class PackageManifest(BaseModel):
    format_version: FormatVersion
    format_uuid: FormatUUID
    tooth: str
    version: SemanticVersion
    info: PackageManifestInfo = PackageManifestInfo()
    variants: list[PackageManifestVariant] = []


class PackageIndexPackage(BaseModel):
    info: PackageManifestInfo = PackageManifestInfo()
    updated_at: datetime
    stars: int
    versions: dict[SemanticVersion, list[PackageManifestVariantLabel]]


class PackageIndex(BaseModel):
    format_version: FormatVersion = 3
    format_uuid: FormatUUID = "289f771f-2c9a-4d73-9f3f-8492495a924d"
    packages: dict[str, PackageIndexPackage]

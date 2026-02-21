from datetime import datetime
from typing import Annotated, Final

from pydantic import BaseModel, StringConstraints

FORMAT_UUID: Final = "289f771f-2c9a-4d73-9f3f-8492495a924d"
FORMAT_VERSION: Final = 3


type Tag = Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]+(:[a-z0-9-]+)?$")]


class PackageManifestInfo(BaseModel):
    name: str = ""
    description: str = ""
    tags: list[Tag] = []
    avatar_url: str = ""


class PackageManifestVariant(BaseModel):
    label: str = ""


class PackageManifest(BaseModel):
    tooth: str
    version: str
    info: PackageManifestInfo = PackageManifestInfo()
    variants: list[PackageManifestVariant] = []


class PackageIndexPackage(BaseModel):
    info: PackageManifestInfo = PackageManifestInfo()
    updated_at: datetime
    stars: int
    versions: dict[str, list[str]]


class PackageIndex(BaseModel):
    format_version: int = FORMAT_VERSION
    format_uuid: str = FORMAT_UUID
    packages: dict[str, PackageIndexPackage]

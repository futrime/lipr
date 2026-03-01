from pydantic import BaseModel, Field


class ManifestVariant(BaseModel):
    label: str = ""
    dependencies: dict[str, str] = Field(default_factory=dict)


class Manifest(BaseModel):
    format_version: int
    format_uuid: str
    tooth: str
    version: str
    info: dict = Field(default_factory=dict)
    variants: list[ManifestVariant] = Field(default_factory=list)


class IndexVariant(BaseModel):
    versions: list[str]


class IndexPackage(BaseModel):
    info: dict
    stargazer_count: int
    updated_at: str
    variants: dict[str, IndexVariant]


class Index(BaseModel):
    format_version: int = 3
    format_uuid: str = "289f771f-2c9a-4d73-9f3f-8492495a924d"
    packages: dict[str, IndexPackage]


class IndexVersionForLeviLauncher(BaseModel):
    dependencies: dict[str, str]


class IndexVariantForLeviLauncher(BaseModel):
    versions: dict[str, IndexVersionForLeviLauncher]


class IndexPackageForLeviLauncher(BaseModel):
    info: dict
    stargazer_count: int
    updated_at: str
    variants: dict[str, IndexVariantForLeviLauncher]


class IndexForLeviLauncher(BaseModel):
    format_version: int = 3
    format_uuid: str = "289f771f-2c9a-4d73-9f3f-8492495a924d"
    packages: dict[str, IndexPackageForLeviLauncher]

"""Immutable values returned by the manifest, policy, and STS layers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

PolicyMode = Literal["per-object-version", "per-object", "mixed", "per-prefix"]


@dataclass(frozen=True, slots=True)
class PinnedManifestURI:
    """A fully resolved Quilt package revision."""

    bucket: str
    package_name: str
    top_hash: str
    original: str

    @property
    def registry(self) -> str:
        """Return the registry URI accepted by ``quilt3.Package.browse``."""
        return f"s3://{self.bucket}"

    @property
    def manifest_arn(self) -> str:
        """Return the exact content-addressed Quilt manifest resource."""
        return f"arn:aws:s3:::{self.bucket}/.quilt/packages/{self.top_hash}"


@dataclass(frozen=True, slots=True)
class ManifestObject:
    """One logical Quilt entry resolved to an S3 object."""

    logical_key: str
    bucket: str
    key: str
    version_id: str | None = None
    hash_type: str | None = None
    hash_value: str | None = None

    @property
    def arn(self) -> str:
        """Return the IAM resource ARN for this exact S3 key."""
        return f"arn:aws:s3:::{self.bucket}/{self.key}"


WriteSelectorKind = Literal["package", "subtree", "object"]


@dataclass(frozen=True, slots=True)
class OutputSelector:
    """One normalized logical write grant inside an output Quilt package."""

    bucket: str
    package_name: str
    path: str
    kind: WriteSelectorKind
    original: str
    description: str

    @property
    def data_resource(self) -> str:
        """Return the exact IAM data resource represented by this selector."""
        root = f"arn:aws:s3:::{self.bucket}/{self.package_name}/"
        if self.kind == "package":
            return f"{root}*"
        if self.kind == "subtree":
            return f"{root}{self.path}*"
        return f"{root}{self.path}"

    def admits(self, logical_key: str) -> bool:
        """Return whether this selector admits a handler-produced logical key."""
        if self.kind == "package":
            return True
        if self.kind == "subtree":
            return logical_key.startswith(self.path)
        return logical_key == self.path


@dataclass(frozen=True, slots=True)
class OutputPackage:
    """The bounded namespace in which a descendant Quilt package may be written."""

    name: str
    bucket: str
    data_write_resources: tuple[str, ...] = ()

    @property
    def bucket_arn(self) -> str:
        """Return the output bucket ARN used for versioning checks."""
        return f"arn:aws:s3:::{self.bucket}"

    @property
    def data_prefix(self) -> str:
        """Return the deterministic data prefix used by Quilt's default push layout."""
        return f"{self.name}/"

    @property
    def resources(self) -> tuple[str, ...]:
        """Return the complete bounded write set for package data and control objects."""
        root = f"arn:aws:s3:::{self.bucket}/"
        data = self.data_write_resources or (f"{root}{self.data_prefix}*",)
        return (
            *data,
            f"{root}.quilt/packages/*",
            f"{root}.quilt/named_packages/{self.name}/*",
        )

    @property
    def write_resources(self) -> tuple[str, ...]:
        """Return the write resources retained for API compatibility."""
        return self.resources

    @property
    def read_resources(self) -> tuple[str, str]:
        """Return Quilt workflow and exact output-pointer read resources."""
        root = f"arn:aws:s3:::{self.bucket}/.quilt/"
        return (
            f"{root}workflows/*",
            f"{root}named_packages/{self.name}/latest",
        )


@dataclass(frozen=True, slots=True)
class CompiledPolicy:
    """A deterministic inline session policy plus a non-secret authority report."""

    document: Mapping[str, Any]
    policy_json: str
    mode: PolicyMode
    manifest: PinnedManifestURI
    readable_selectors: tuple[str, ...]
    selected_objects: tuple[ManifestObject, ...]
    output_package: OutputPackage
    warnings: tuple[str, ...] = ()
    bedrock_model_arn: str | None = None
    writable_selectors: tuple[OutputSelector, ...] = ()

    @property
    def size(self) -> int:
        """Return the plaintext character count enforced by STS."""
        return len(self.policy_json)

    def report(self) -> dict[str, Any]:
        """Return serializable, non-secret compilation evidence."""
        return {
            "manifest_uri": self.manifest.original,
            "top_hash": self.manifest.top_hash,
            "mode": self.mode,
            "policy_size": self.size,
            "readable_selectors": list(self.readable_selectors),
            "selected_objects": [
                {
                    "logical_key": item.logical_key,
                    "bucket": item.bucket,
                    "key": item.key,
                    "version_id": item.version_id,
                }
                for item in self.selected_objects
            ],
            "writable_package": f"{self.output_package.name}@{self.output_package.bucket}",
            "writable_selectors": [
                {
                    "uri": selector.original,
                    "description": selector.description,
                    "kind": selector.kind,
                    "path": selector.path,
                }
                for selector in self.writable_selectors
            ],
            "bedrock_model_arn": self.bedrock_model_arn,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ScopedCredentials:
    """Temporary credentials minted for one Scene.

    Secret fields are deliberately excluded from ``repr``. Callers should pass them
    directly to an SDK or isolated worker and must not write them to transcripts.
    """

    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str = field(repr=False)
    expiration: datetime
    assumed_role_arn: str
    assumed_role_id: str
    packed_policy_size: int
    compilation: CompiledPolicy

    def boto3_kwargs(self) -> dict[str, str]:
        """Return keyword arguments suitable for a scoped ``boto3.Session``."""
        return {
            "aws_access_key_id": self.access_key_id,
            "aws_secret_access_key": self.secret_access_key,
            "aws_session_token": self.session_token,
        }

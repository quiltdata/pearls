from __future__ import annotations

from collections.abc import Sequence

import pytest

from raja_sts import ManifestObject
from raja_sts.models import PinnedManifestURI

TOP_HASH = "a" * 64
MANIFEST_URI = f"quilt+s3://manifest-bucket#package=project%2Fscene-input&top_hash={TOP_HASH}"


@pytest.fixture
def manifest_objects() -> tuple[ManifestObject, ...]:
    return (
        ManifestObject(
            logical_key="input-content/data.csv",
            bucket="input-bucket",
            key="sealed/data.csv",
            version_id="version-data",
            hash_type="SHA256",
            hash_value="1" * 64,
        ),
        ManifestObject(
            logical_key="input-content/notes.txt",
            bucket="input-bucket",
            key="sealed/notes.txt",
            version_id="version-notes",
            hash_type="SHA256",
            hash_value="2" * 64,
        ),
        ManifestObject(
            logical_key="input-control/worker.json",
            bucket="control-bucket",
            key="scenes/worker.json",
            version_id="version-worker",
            hash_type="SHA256",
            hash_value="3" * 64,
        ),
        ManifestObject(
            logical_key="input-control/supervisor.json",
            bucket="control-bucket",
            key="scenes/supervisor.json",
            version_id="version-supervisor",
            hash_type="SHA256",
            hash_value="4" * 64,
        ),
        ManifestObject(
            logical_key="rubric/secret.json",
            bucket="control-bucket",
            key="rubric/secret.json",
            version_id="version-secret",
            hash_type="SHA256",
            hash_value="5" * 64,
        ),
    )


@pytest.fixture
def manifest_loader(manifest_objects: Sequence[ManifestObject]):
    def load(manifest: PinnedManifestURI) -> Sequence[ManifestObject]:
        assert manifest.bucket == "manifest-bucket"
        assert manifest.package_name == "project/scene-input"
        assert manifest.top_hash == TOP_HASH
        return manifest_objects

    return load


@pytest.fixture
def scope_spec() -> dict[str, object]:
    return {
        "readable": ["input-control/worker.json", "input-content/"],
        "writable_package": "project/scene-output@output-bucket",
        "require_version_ids": True,
    }

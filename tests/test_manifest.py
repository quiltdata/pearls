from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
import quilt3
from conftest import MANIFEST_URI, TOP_HASH

from pearls import InvalidManifestURI, InvalidScopeError, ManifestLoadError
from pearls.manifest import (
    load_quilt_manifest,
    parse_manifest_uri,
    parse_output_package,
    parse_output_selectors,
)
from pearls.models import OutputPackage, PinnedManifestURI


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "s3://bucket#package=a%2Fb&top_hash=" + TOP_HASH,
        "quilt+s3://ab#package=a%2Fb&top_hash=" + TOP_HASH,
        "quilt+s3://valid-bucket/path#package=a%2Fb&top_hash=" + TOP_HASH,
        "quilt+s3://valid-bucket?package=a%2Fb#top_hash=" + TOP_HASH,
        "quilt+s3://valid-bucket#package=a%2Fb",
        "quilt+s3://valid-bucket#package=a%2Fb&top_hash=short",
        "quilt+s3://valid-bucket#package=..%2Fb&top_hash=" + TOP_HASH,
        "quilt+s3://valid-bucket#package=a%2Fb&top_hash=" + TOP_HASH + "&extra=1",
        "quilt+s3://user@valid-bucket#package=a%2Fb&top_hash=" + TOP_HASH,
        "quilt+s3://valid-bucket:not-a-port#package=a%2Fb&top_hash=" + TOP_HASH,
        "quilt+s3://valid-bucket#package",
    ],
)
def test_parse_manifest_uri_rejects_floating_or_ambiguous_references(uri: str) -> None:
    with pytest.raises(InvalidManifestURI):
        parse_manifest_uri(uri)


def test_parse_manifest_uri_returns_a_pinned_revision() -> None:
    result = parse_manifest_uri(MANIFEST_URI)
    assert result == PinnedManifestURI(
        bucket="manifest-bucket",
        package_name="project/scene-input",
        top_hash=TOP_HASH,
        original=MANIFEST_URI,
    )
    assert result.registry == "s3://manifest-bucket"
    assert result.manifest_arn == (f"arn:aws:s3:::manifest-bucket/.quilt/packages/{TOP_HASH}")


@pytest.mark.parametrize(
    "value",
    [None, "missing-at", "two@at@signs", "../escape@valid-bucket", "name@Bad_Bucket"],
)
def test_parse_output_package_rejects_unsafe_names(value: object) -> None:
    with pytest.raises(InvalidScopeError):
        parse_output_package(value)


def test_parse_output_package_derives_quilt_namespaces() -> None:
    output = parse_output_package("project/output@output-bucket")
    assert output.data_prefix == "project/output/"
    assert output.write_resources == (
        "arn:aws:s3:::output-bucket/project/output/*",
        "arn:aws:s3:::output-bucket/.quilt/packages/*",
        "arn:aws:s3:::output-bucket/.quilt/named_packages/project/output/*",
    )
    assert output.resources == output.write_resources
    assert output.read_resources == (
        "arn:aws:s3:::output-bucket/.quilt/workflows/*",
        "arn:aws:s3:::output-bucket/.quilt/named_packages/project/output/latest",
    )
    revision_output = OutputPackage("project/output", "output-bucket", revision_id="scene-123")
    assert revision_output.read_resources == (
        "arn:aws:s3:::output-bucket/.quilt/workflows/*",
        "arn:aws:s3:::output-bucket/.quilt/named_packages/project/output/latest",
        "arn:aws:s3:::output-bucket/.quilt/named_packages/project/output/scene-123",
    )


@dataclass
class FakePhysicalKey:
    bucket: str | None
    path: str
    version_id: str | None


@dataclass
class FakeEntry:
    physical_key: FakePhysicalKey
    hash: dict[str, str] | None = None


class FakePackage:
    def __init__(self, top_hash: str, entries: list[tuple[str, FakeEntry]]) -> None:
        self.top_hash = top_hash
        self._entries = entries

    def walk(self) -> Any:
        return iter(self._entries)


def test_load_quilt_manifest_resolves_s3_version_and_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    package = FakePackage(
        TOP_HASH,
        [
            (
                "data/file.csv",
                FakeEntry(
                    FakePhysicalKey("data-bucket", "prefix/file.csv", "version-1"),
                    {"type": "SHA256", "value": "b" * 64},
                ),
            )
        ],
    )
    calls: list[tuple[str, str | None, str | None]] = []

    def browse(name: str, registry: str | None = None, top_hash: str | None = None) -> FakePackage:
        calls.append((name, registry, top_hash))
        return package

    monkeypatch.setattr(quilt3.Package, "browse", browse)
    result = load_quilt_manifest(parse_manifest_uri(MANIFEST_URI))
    assert calls == [("project/scene-input", "s3://manifest-bucket", TOP_HASH)]
    assert result[0].logical_key == "data/file.csv"
    assert result[0].arn == "arn:aws:s3:::data-bucket/prefix/file.csv"
    assert result[0].version_id == "version-1"
    assert result[0].hash_value == "b" * 64


@pytest.mark.parametrize(
    ("package", "message"),
    [
        (FakePackage("b" * 64, []), "does not match"),
        (FakePackage(TOP_HASH, []), "contains no objects"),
        (
            FakePackage(TOP_HASH, [("local", FakeEntry(FakePhysicalKey(None, "/tmp/a", None)))]),
            "not backed by S3",
        ),
        (
            FakePackage(
                TOP_HASH, [("wild", FakeEntry(FakePhysicalKey("valid-bucket", "a/*", None)))]
            ),
            "widen",
        ),
        (
            FakePackage(
                TOP_HASH,
                [
                    (
                        "variable",
                        FakeEntry(FakePhysicalKey("valid-bucket", "${aws:userid}/secret", None)),
                    )
                ],
            ),
            "widen",
        ),
        (
            FakePackage(
                TOP_HASH,
                [
                    (
                        "version-variable",
                        FakeEntry(FakePhysicalKey("valid-bucket", "a", "${aws:userid}")),
                    )
                ],
            ),
            "invalid version",
        ),
        (
            FakePackage(
                TOP_HASH, [("slash", FakeEntry(FakePhysicalKey("valid-bucket", "/a", None)))]
            ),
            "invalid S3 key",
        ),
        (
            FakePackage(
                TOP_HASH, [("version", FakeEntry(FakePhysicalKey("valid-bucket", "a", "")))]
            ),
            "invalid version",
        ),
    ],
)
def test_load_quilt_manifest_fails_closed(
    monkeypatch: pytest.MonkeyPatch, package: FakePackage, message: str
) -> None:
    monkeypatch.setattr(quilt3.Package, "browse", lambda *args, **kwargs: package)
    with pytest.raises(ManifestLoadError, match=message):
        load_quilt_manifest(parse_manifest_uri(MANIFEST_URI))


def test_load_quilt_manifest_wraps_sdk_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(quilt3.Package, "browse", fail)
    with pytest.raises(ManifestLoadError, match="network unavailable"):
        load_quilt_manifest(parse_manifest_uri(MANIFEST_URI))


def test_null_s3_version_is_treated_as_unversioned(monkeypatch: pytest.MonkeyPatch) -> None:
    package = FakePackage(
        TOP_HASH,
        [("data", FakeEntry(FakePhysicalKey("valid-bucket", "data", "null")))],
    )
    monkeypatch.setattr(quilt3.Package, "browse", lambda *args, **kwargs: package)
    assert load_quilt_manifest(parse_manifest_uri(MANIFEST_URI))[0].version_id is None


def test_parse_output_selectors_supports_package_subtree_and_object_shapes() -> None:
    whole = parse_output_selectors(
        {"quilt+s3://output-bucket#package=project%2Foutput": "all generated files"}
    )
    assert [(item.kind, item.path) for item in whole] == [("package", "")]
    assert whole[0].data_resource == "arn:aws:s3:::output-bucket/project/output/*"
    assert whole[0].admits("anything.txt")

    narrowed = parse_output_selectors(
        {
            "quilt+s3://output-bucket/results/#package=project%2Foutput": "result tree",
            "quilt+s3://output-bucket/summary.json#package=project%2Foutput": "summary",
        }
    )
    assert [(item.kind, item.path) for item in narrowed] == [
        ("subtree", "results/"),
        ("object", "summary.json"),
    ]
    assert narrowed[0].admits("results/a.txt")
    assert not narrowed[0].admits("result/a.txt")
    assert narrowed[1].data_resource.endswith("/project/output/summary.json")


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        {1: "description"},
        {"quilt+s3://output-bucket#package=project%2Foutput": ""},
        {"s3://output-bucket#package=project%2Foutput": "description"},
        {"quilt+s3://ab#package=project%2Foutput": "description"},
        {"quilt+s3://user@output-bucket#package=project%2Foutput": "description"},
        {"quilt+s3://output-bucket?x=1#package=project%2Foutput": "description"},
        {"quilt+s3://output-bucket#package=project%2Foutput&top_hash=" + TOP_HASH: "description"},
        {"quilt+s3://output-bucket/%ZZ#package=project%2Foutput": "description"},
        {"quilt+s3://output-bucket/../escape#package=project%2Foutput": "description"},
        {"quilt+s3://output-bucket/run/#package=project%2Foutput": "description"},
        {"quilt+s3://output-bucket/run/scene.json#package=project%2Foutput": "description"},
        {
            "quilt+s3://output-bucket#package=project%2Foutput": "all",
            "quilt+s3://output-bucket/results/#package=project%2Foutput": "nested",
        },
        {
            "quilt+s3://output-bucket/results/#package=project%2Foutput": "parent",
            "quilt+s3://output-bucket/results/a.txt#package=project%2Foutput": "child",
        },
        {
            "quilt+s3://output-bucket/results/a/#package=project%2Foutput": "child first",
            "quilt+s3://output-bucket/results/#package=project%2Foutput": "parent second",
        },
        {
            "quilt+s3://output-bucket/a#package=project%2Foutput": "one",
            "quilt+s3://other-bucket/b#package=project%2Foutput": "two",
        },
    ],
)
def test_parse_output_selectors_rejects_unsafe_or_ambiguous_grants(value: object) -> None:
    with pytest.raises(InvalidScopeError):
        parse_output_selectors(value)

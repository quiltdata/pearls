from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from authoring_support import (
    AUTHOR_DESTINATION,
    AUTHOR_MESSAGE,
    AUTHOR_METADATA,
    RUNTIME_REQUIREMENTS_JSON,
    AuthoringS3,
    Stored,
    sha256,
    write_valid_genesis,
)

from raja_sts import (
    RUNTIME_REQUIREMENTS_MAX_BYTES,
    RUNTIME_REQUIREMENTS_SCHEMA,
    GenesisAuthoringError,
    HandlerContractError,
    InputIntegrityError,
    InvalidManifestURI,
    ManifestLoadError,
    PublicationConflictError,
    PublicationError,
    RuntimeRequirementsError,
    WorkflowContractError,
    format_manifest_uri,
    freeze_genesis_directory,
    generate_genesis_revision_id,
    load_quilt_manifest_from_s3,
    parse_manifest_uri,
    parse_seal_input_uri,
    prepare_genesis_authoring,
    publish_genesis_authoring,
)
from raja_sts.authoring import DEFAULT_GENESIS_WORKFLOW


def _prepare(directory: Path, s3: AuthoringS3, **kwargs: object):
    arguments = {
        "destination": AUTHOR_DESTINATION,
        "user_meta": AUTHOR_METADATA,
        "message": AUTHOR_MESSAGE,
        "s3_client": s3,
        "revision_id": "genesis-20260820T120000Z-0123456789abcdef",
    }
    arguments.update(kwargs)
    return prepare_genesis_authoring(directory, **arguments)


def test_canonical_manifest_uri_percent_encodes_package_names_and_round_trips() -> None:
    uri = format_manifest_uri(
        bucket="output-bucket",
        package_name="project/genesis-one",
        top_hash="A" * 64,
    )

    assert uri == ("quilt+s3://output-bucket#package=project%2Fgenesis-one&top_hash=" + "a" * 64)
    parsed = parse_manifest_uri(uri)
    assert parsed.package_name == "project/genesis-one"
    assert parsed.top_hash == "a" * 64
    assert parsed.original == uri


def test_strict_seal_input_uri_maps_one_source_path_under_input() -> None:
    top_hash = "b" * 64
    value = (
        "quilt+s3://foreign-registry#package=foreign%2Fsource"
        f"&top_hash={top_hash}&path=evidence%2Fresult.json"
    )

    parsed = parse_seal_input_uri(value)

    assert parsed.manifest.package_name == "foreign/source"
    assert parsed.manifest.top_hash == top_hash
    assert parsed.logical_key == "evidence/result.json"
    assert parsed.destination_logical_key == "input/evidence/result.json"


def test_strict_seal_input_uri_preserves_percent_encoded_path_spaces() -> None:
    parsed = parse_seal_input_uri(
        "quilt+s3://foreign-registry#package=foreign%2Fsource"
        f"&top_hash={'b' * 64}&path=evidence%2Fresult%20one.json"
    )

    assert parsed.logical_key == "evidence/result one.json"
    assert parsed.destination_logical_key == "input/evidence/result one.json"


@pytest.mark.parametrize("padding", [" ", "  "])
def test_strict_seal_input_uri_rejects_raw_surrounding_whitespace(padding: str) -> None:
    uri = (
        "quilt+s3://foreign-registry#package=foreign%2Fsource"
        f"&top_hash={'b' * 64}&path=evidence.txt"
    )

    with pytest.raises(InvalidManifestURI, match="surrounding whitespace"):
        parse_seal_input_uri(padding + uri)
    with pytest.raises(InvalidManifestURI, match="surrounding whitespace"):
        parse_seal_input_uri(uri + padding)


@pytest.mark.parametrize(
    "fragment",
    [
        "package=foreign%2Fsource&path=a.txt",
        "package=foreign%2Fsource&top_hash={hash}",
        "package=foreign%2Fsource&top_hash={hash}&path=",
        "package=foreign%2Fsource&top_hash={hash}&path=dir%2F",
        "package=foreign%2Fsource&top_hash={hash}&path=%2Fabsolute",
        "package=foreign%2Fsource&top_hash={hash}&path=a%2F..%2Fb",
        "package=foreign%2Fsource&top_hash={hash}&path=a&path=b",
        "package=foreign%2Fsource&top_hash={hash}&path=a&extra=x",
        "package=foreign%2Fsource&top_hash=short&path=a",
        "package=foreign%2Fsource&top_hash=latest&path=a",
        "package=foreign%2Fsource&top_hash={hash}&path=bad%ZZ",
    ],
    ids=[
        "floating",
        "missing-path",
        "empty-path",
        "directory-path",
        "absolute-path",
        "parent-path",
        "duplicate-field",
        "extra-field",
        "hash-prefix",
        "alias",
        "malformed-encoding",
    ],
)
def test_strict_seal_input_uri_rejects_ambiguous_or_unsafe_forms(fragment: str) -> None:
    uri = "quilt+s3://foreign-registry#" + fragment.format(hash="b" * 64)

    with pytest.raises(InvalidManifestURI):
        parse_seal_input_uri(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "s3://foreign-registry#package=p&top_hash={hash}&path=a",
        "quilt+s3://user@foreign-registry#package=p&top_hash={hash}&path=a",
        "quilt+s3://foreign-registry/object#package=p&top_hash={hash}&path=a",
        "quilt+s3://foreign-registry?x=1#package=p&top_hash={hash}&path=a",
    ],
)
def test_strict_seal_input_uri_rejects_wrong_authority_or_uri_location(uri: str) -> None:
    with pytest.raises(InvalidManifestURI):
        parse_seal_input_uri(uri.format(hash="b" * 64))


def test_local_freeze_is_sorted_immutable_and_reads_nested_regular_files(tmp_path: Path) -> None:
    write_valid_genesis(tmp_path)
    (tmp_path / "z.txt").write_bytes(b"last")
    (tmp_path / "a.txt").write_bytes(b"first")

    frozen = freeze_genesis_directory(tmp_path)

    assert list(frozen) == sorted(frozen)
    assert frozen["protocol/handler.py"]
    assert frozen["a.txt"] == b"first"
    with pytest.raises(TypeError):
        frozen["new.txt"] = b"no"  # type: ignore[index]


@pytest.mark.parametrize("name", ["bad*name", "bad?name", "bad\\name"])
def test_local_freeze_rejects_unsafe_relative_paths(tmp_path: Path, name: str) -> None:
    write_valid_genesis(tmp_path)
    (tmp_path / name).write_bytes(b"unsafe")

    with pytest.raises(GenesisAuthoringError, match="unsafe local logical key"):
        freeze_genesis_directory(tmp_path)


def test_local_freeze_rejects_symlinks_and_non_regular_files(tmp_path: Path) -> None:
    write_valid_genesis(tmp_path)
    target = tmp_path / "target.txt"
    target.write_bytes(b"target")
    (tmp_path / "linked.txt").symlink_to(target)

    with pytest.raises(GenesisAuthoringError, match="must not be a symlink"):
        freeze_genesis_directory(tmp_path)

    (tmp_path / "linked.txt").unlink()
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(GenesisAuthoringError, match="not a regular file"):
        freeze_genesis_directory(tmp_path)


def test_local_freeze_rejects_unreadable_files_even_for_privileged_test_users(
    tmp_path: Path,
) -> None:
    write_valid_genesis(tmp_path)
    path = tmp_path / "input" / "local.txt"
    path.chmod(0)
    try:
        with pytest.raises(GenesisAuthoringError, match="not readable"):
            freeze_genesis_directory(tmp_path)
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize(
    ("mutation", "error_type", "message"),
    [
        (lambda root: (root / "README.md").unlink(), ManifestLoadError, "README.md"),
        (
            lambda root: (root / "README.md").write_bytes(b"\xff"),
            ManifestLoadError,
            "UTF-8",
        ),
        (
            lambda root: (root / "outputs.json").write_text("[]"),
            Exception,
            "URI-to-description object",
        ),
        (
            lambda root: (root / "protocol" / "requirements.json").unlink(),
            ManifestLoadError,
            "protocol/requirements.json",
        ),
        (
            lambda root: (root / "protocol" / "requirements.json").write_text("{}"),
            RuntimeRequirementsError,
            "missing required field",
        ),
        (
            lambda root: (root / "protocol" / "requirements.json").write_bytes(
                b" " * (RUNTIME_REQUIREMENTS_MAX_BYTES + 1)
            ),
            RuntimeRequirementsError,
            "exceeds",
        ),
        (
            lambda root: (root / "protocol" / "handler.py").unlink(),
            ManifestLoadError,
            "protocol/handler.py",
        ),
        (
            lambda root: (root / "protocol" / "handler.py").write_text("answer = 1"),
            HandlerContractError,
            "does not bind",
        ),
        (
            lambda root: (root / "input" / "local.txt").unlink(),
            ManifestLoadError,
            "input/",
        ),
    ],
    ids=[
        "readme-missing",
        "readme-utf8",
        "outputs-object",
        "requirements-missing",
        "requirements-malformed",
        "requirements-oversized",
        "handler-missing",
        "handler",
        "input",
    ],
)
def test_preparation_validates_the_execution_genesis_layout(
    tmp_path: Path,
    mutation: object,
    error_type: type[Exception],
    message: str,
) -> None:
    write_valid_genesis(tmp_path)
    assert callable(mutation)
    mutation(tmp_path)  # type: ignore[operator]
    s3 = AuthoringS3()

    with pytest.raises(error_type, match=message):
        _prepare(tmp_path, s3)

    assert s3.put_requests == []


def test_authoring_validates_requirement_syntax_not_local_compatibility(
    tmp_path: Path,
) -> None:
    write_valid_genesis(tmp_path)
    declaration = {
        "$schema": RUNTIME_REQUIREMENTS_SCHEMA,
        "python": "<0",
        "imports": [
            {
                "module": "definitely_not_installed_for_authoring",
                "requirement": "definitely-not-installed-for-authoring==999",
            }
        ],
    }
    (tmp_path / "protocol" / "requirements.json").write_text(
        json.dumps(declaration),
        encoding="utf-8",
    )
    s3 = AuthoringS3()

    plan = _prepare(tmp_path, s3)

    assert plan.objects["protocol/requirements.json"] == json.dumps(declaration).encode()
    assert s3.put_requests == []


def test_handler_selection_is_bound_when_frozen_objects_are_identical(tmp_path: Path) -> None:
    write_valid_genesis(tmp_path)
    default_handler = tmp_path / "protocol" / "handler.py"
    supervisor_handler = tmp_path / "protocol" / "supervisor.py"
    supervisor_handler.write_bytes(default_handler.read_bytes())
    s3 = AuthoringS3()
    default_plan = _prepare(tmp_path, s3)
    custom_plan = _prepare(tmp_path, s3, handler_key="protocol/supervisor.py")

    assert default_plan.objects == custom_plan.objects
    assert default_plan.declared_intent != custom_plan.declared_intent

    publish_genesis_authoring(default_plan, s3_client=s3, timestamp=generate_time())
    write_count = len(s3.put_requests)

    with pytest.raises(PublicationConflictError, match="different publication intent"):
        publish_genesis_authoring(custom_plan, s3_client=s3, timestamp=generate_time())
    assert len(s3.put_requests) == write_count

    with pytest.raises(PublicationConflictError, match="different publication intent"):
        _prepare(tmp_path, s3, handler_key="protocol/supervisor.py")
    assert len(s3.put_requests) == write_count


def test_preparation_accepts_and_records_a_custom_handler_key(tmp_path: Path) -> None:
    handler_key = "protocol/supervisor.py"
    write_valid_genesis(tmp_path, handler_key=handler_key)
    s3 = AuthoringS3()

    plan = _prepare(tmp_path, s3, handler_key=handler_key)

    assert plan.handler_key == handler_key
    assert handler_key in plan.objects
    assert "protocol/handler.py" not in plan.objects
    assert plan.objects["protocol/requirements.json"] == RUNTIME_REQUIREMENTS_JSON
    assert s3.put_requests == []


@pytest.mark.parametrize(
    "handler_key",
    ["", "/handler.py", "protocol/", "protocol/../handler.py", "protocol\\handler.py"],
)
def test_preparation_rejects_unsafe_handler_keys_before_writes(
    tmp_path: Path,
    handler_key: str,
) -> None:
    write_valid_genesis(tmp_path)
    s3 = AuthoringS3()

    with pytest.raises(GenesisAuthoringError, match="handler key"):
        _prepare(tmp_path, s3, handler_key=handler_key)

    assert s3.put_requests == []


def test_caller_s3_manifest_loader_recomputes_top_hash_and_closes_body() -> None:
    s3 = AuthoringS3()
    selection = parse_seal_input_uri(s3.add_source())

    objects = load_quilt_manifest_from_s3(selection.manifest, s3_client=s3)

    assert [item.logical_key for item in objects] == ["evidence/source.txt"]
    assert s3.get_requests[0] == {
        "Bucket": "foreign-registry",
        "Key": f".quilt/packages/{selection.manifest.top_hash}",
    }
    assert s3.bodies[0].was_closed


def test_caller_s3_manifest_loader_refuses_semantically_wrong_manifest_bytes() -> None:
    s3 = AuthoringS3()
    first = parse_seal_input_uri(s3.add_source(content=b"first"))
    second = parse_seal_input_uri(s3.add_source(content=b"second", package_name="foreign/second"))
    first_key = (
        first.manifest.bucket,
        f".quilt/packages/{first.manifest.top_hash}",
        None,
    )
    second_key = (
        second.manifest.bucket,
        f".quilt/packages/{second.manifest.top_hash}",
        None,
    )
    s3.remote[first_key] = s3.remote[second_key]

    with pytest.raises(ManifestLoadError, match="does not match"):
        load_quilt_manifest_from_s3(first.manifest, s3_client=s3)


def test_seal_provenance_is_bound_when_materialized_bytes_are_identical(
    tmp_path: Path,
) -> None:
    write_valid_genesis(tmp_path)
    s3 = AuthoringS3()
    first_uri = s3.add_source(content=b"shared bytes", package_name="foreign/one")
    second_uri = s3.add_source(content=b"shared bytes", package_name="foreign/two")
    first_plan = _prepare(tmp_path, s3, seal_inputs=(first_uri,))
    second_plan = _prepare(tmp_path, s3, seal_inputs=(second_uri,))

    assert first_plan.objects == second_plan.objects
    assert first_plan.seals != second_plan.seals
    assert first_plan.declared_intent != second_plan.declared_intent

    publish_genesis_authoring(first_plan, s3_client=s3, timestamp=generate_time())
    write_count = len(s3.put_requests)

    with pytest.raises(PublicationConflictError, match="different publication intent"):
        publish_genesis_authoring(second_plan, s3_client=s3, timestamp=generate_time())

    assert len(s3.put_requests) == write_count


def test_sealed_input_is_read_by_exact_version_verified_and_copied_as_bytes(
    tmp_path: Path,
) -> None:
    write_valid_genesis(tmp_path)
    s3 = AuthoringS3()
    seal_uri = s3.add_source(content=b"foreign bytes")

    plan = _prepare(tmp_path, s3, seal_inputs=(seal_uri,))

    selection = parse_seal_input_uri(seal_uri)
    source = next(item for item in s3.get_requests if item.get("VersionId") == "source-object-v1")
    assert source == {
        "Bucket": "foreign-data",
        "Key": "published/evidence/source.txt",
        "VersionId": "source-object-v1",
    }
    assert plan.objects["input/evidence/source.txt"] == b"foreign bytes"
    assert plan.seals[0].destination_logical_key == "input/evidence/source.txt"
    assert plan.seals[0].sha256 == sha256(b"foreign bytes")
    assert selection.manifest.top_hash in plan.seals[0].source_uri


def test_exact_version_or_declared_hash_mismatch_refuses_sealing(tmp_path: Path) -> None:
    write_valid_genesis(tmp_path)
    s3 = AuthoringS3()
    seal_uri = s3.add_source(content=b"expected")
    data_key = ("foreign-data", "published/evidence/source.txt", "source-object-v1")
    stored = s3.remote[data_key]
    s3.remote[data_key] = replace(stored, version_id="wrong-version")

    with pytest.raises(InputIntegrityError, match="wrong version"):
        _prepare(tmp_path, s3, seal_inputs=(seal_uri,))
    assert s3.put_requests == []

    s3.remote[data_key] = Stored(b"tampered", "source-object-v1")
    with pytest.raises(InputIntegrityError, match="Content hash mismatch"):
        _prepare(tmp_path, s3, seal_inputs=(seal_uri,))
    assert s3.put_requests == []


def test_duplicate_sources_and_local_sealed_destination_collisions_are_refused(
    tmp_path: Path,
) -> None:
    write_valid_genesis(tmp_path)
    s3 = AuthoringS3()
    seal_uri = s3.add_source()

    with pytest.raises(GenesisAuthoringError, match="duplicate sealed source"):
        _prepare(tmp_path, s3, seal_inputs=(seal_uri, seal_uri))

    collision = tmp_path / "input" / "evidence"
    collision.mkdir()
    (collision / "source.txt").write_bytes(b"local")
    with pytest.raises(GenesisAuthoringError, match="local/sealed destination collision"):
        _prepare(tmp_path, s3, seal_inputs=(seal_uri,))
    assert s3.put_requests == []


def test_preparation_is_a_complete_dry_run_with_no_writes(tmp_path: Path) -> None:
    write_valid_genesis(tmp_path)
    s3 = AuthoringS3()

    plan = _prepare(tmp_path, s3)

    assert plan.workflow == DEFAULT_GENESIS_WORKFLOW
    assert plan.metadata_keys == ("owner",)
    assert [entry.logical_key for entry in plan.entries] == sorted(plan.objects)
    assert all(entry.sha256 == sha256(plan.objects[entry.logical_key]) for entry in plan.entries)
    assert s3.versioning_requests == [{"Bucket": "output-bucket"}]
    assert any(request["Key"] == ".quilt/workflows/config.yml" for request in s3.get_requests)
    assert s3.put_requests == []


def test_workflow_or_versioning_refusal_occurs_before_any_write(tmp_path: Path) -> None:
    write_valid_genesis(tmp_path)
    s3 = AuthoringS3()

    with pytest.raises(WorkflowContractError, match="Package metadata failed validation"):
        _prepare(tmp_path, s3, user_meta={})
    assert s3.put_requests == []

    s3.versioning_status = "Suspended"
    with pytest.raises(PublicationError, match="must have versioning enabled"):
        _prepare(tmp_path, s3)
    assert s3.put_requests == []


def test_publication_uses_the_exact_frozen_bytes_and_returns_canonical_identity(
    tmp_path: Path,
) -> None:
    write_valid_genesis(tmp_path)
    s3 = AuthoringS3()
    seal_uri = s3.add_source(content=b"sealed copy")
    plan = _prepare(tmp_path, s3, seal_inputs=(seal_uri,))
    frozen = plan.objects["input/local.txt"]
    (tmp_path / "input" / "local.txt").write_bytes(b"changed after preparation")

    result = publish_genesis_authoring(
        plan,
        s3_client=s3,
        timestamp=generate_time(),
    )

    request = next(
        item for item in s3.put_requests if item["Key"] == "proj/genesis/input/local.txt"
    )
    assert request["Body"] == frozen
    sealed_request = next(
        item for item in s3.put_requests if item["Key"] == "proj/genesis/input/evidence/source.txt"
    )
    assert sealed_request["Body"] == b"sealed copy"
    assert not any(item["Key"].startswith("published/") for item in s3.put_requests)
    assert result.publication.status == "published"
    assert result.publication.is_latest is True
    assert result.publication.revision_version_id
    parsed = parse_manifest_uri(result.package_uri)
    assert parsed.package_name == "proj/genesis"
    assert parsed.top_hash == result.publication.top_hash
    assert "%2F" in result.package_uri
    assert len(s3.versioning_requests) == 2
    assert sum(request["Key"] == ".quilt/workflows/config.yml" for request in s3.get_requests) == 2


def test_latest_cas_loss_remains_a_successful_durable_publication(tmp_path: Path) -> None:
    write_valid_genesis(tmp_path)
    s3 = AuthoringS3()
    plan = _prepare(tmp_path, s3)
    s3.fail_latest = True

    result = publish_genesis_authoring(plan, s3_client=s3, timestamp=generate_time())

    assert result.publication.status == "published-not-latest"
    assert result.publication.is_latest is False
    assert result.publication.revision_version_id
    assert result.package_uri


def test_authoring_requires_nonblank_message_workflow_and_safe_revision(tmp_path: Path) -> None:
    write_valid_genesis(tmp_path)
    s3 = AuthoringS3()

    with pytest.raises(GenesisAuthoringError, match="message"):
        _prepare(tmp_path, s3, message="  ")
    with pytest.raises(GenesisAuthoringError, match="workflow"):
        _prepare(tmp_path, s3, workflow="")
    with pytest.raises(PublicationError, match="revision_id"):
        _prepare(tmp_path, s3, revision_id="latest")
    assert s3.put_requests == []


def generate_time():
    from datetime import UTC, datetime

    return datetime(2026, 8, 20, 12, tzinfo=UTC)


def test_generated_revision_id_is_safe_and_collision_resistant() -> None:
    first = generate_genesis_revision_id(generate_time())
    second = generate_genesis_revision_id(generate_time())

    assert first.startswith("genesis-20260820T120000Z-")
    assert first != second
    assert len(first.rsplit("-", 1)[1]) == 16


@pytest.mark.parametrize("value", [None, b"not-text"])
def test_seal_input_parser_requires_text(value: object) -> None:
    with pytest.raises(InvalidManifestURI, match="non-empty string"):
        parse_seal_input_uri(value)  # type: ignore[arg-type]


def test_seal_input_parser_rejects_invalid_ports_and_malformed_fragment_pairs() -> None:
    top_hash = "b" * 64
    with pytest.raises(InvalidManifestURI, match="authority"):
        parse_seal_input_uri(
            f"quilt+s3://foreign-registry:not-a-port#package=p&top_hash={top_hash}&path=a"
        )
    with pytest.raises(InvalidManifestURI, match="fragment"):
        parse_seal_input_uri(f"quilt+s3://foreign-registry#package&top_hash={top_hash}&path=a")


def test_freeze_refuses_missing_nondirectory_symlink_and_unreadable_roots(tmp_path: Path) -> None:
    with pytest.raises(GenesisAuthoringError, match="unable to inspect"):
        freeze_genesis_directory(tmp_path / "missing")

    regular = tmp_path / "regular"
    regular.write_bytes(b"file")
    with pytest.raises(GenesisAuthoringError, match="must be a directory"):
        freeze_genesis_directory(regular)

    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(GenesisAuthoringError, match="must not be a symlink"):
        freeze_genesis_directory(linked)

    target.chmod(0)
    try:
        with pytest.raises(GenesisAuthoringError, match="not readable"):
            freeze_genesis_directory(target)
    finally:
        target.chmod(0o700)


def test_freeze_wraps_directory_and_file_operating_system_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_valid_genesis(tmp_path)

    def fail_scan(_: object):
        raise OSError("scan denied")

    monkeypatch.setattr("raja_sts.authoring.os.scandir", fail_scan)
    with pytest.raises(GenesisAuthoringError, match="scan denied"):
        freeze_genesis_directory(tmp_path)

    monkeypatch.undo()

    def fail_open(*_: object, **__: object) -> int:
        raise OSError("open denied")

    monkeypatch.setattr("raja_sts.authoring.os.open", fail_open)
    with pytest.raises(GenesisAuthoringError, match="open denied"):
        freeze_genesis_directory(tmp_path)


def test_freeze_detects_a_file_replaced_between_discovery_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_valid_genesis(tmp_path)
    target = tmp_path / "input" / "local.txt"
    target_identity = (target.stat().st_dev, target.stat().st_ino)
    real_fstat = os.fstat

    def changed_fstat(file_descriptor: int) -> os.stat_result:
        observed = real_fstat(file_descriptor)
        if (observed.st_dev, observed.st_ino) != target_identity:
            return observed
        changed = list(observed)
        changed[1] += 1
        return os.stat_result(changed)

    monkeypatch.setattr("raja_sts.authoring.os.fstat", changed_fstat)
    with pytest.raises(GenesisAuthoringError, match="changed during freeze"):
        freeze_genesis_directory(tmp_path)


def test_freeze_detects_same_inode_mutation_between_discovery_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_valid_genesis(tmp_path)
    target = tmp_path / "input" / "local.txt"
    original = target.stat()
    real_open = os.open
    mutated = False

    def mutate_before_open(
        path: str | os.PathLike[str],
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        nonlocal mutated
        if not mutated and path == "local.txt" and kwargs.get("dir_fd") is not None:
            target.write_bytes(b"same inode, replacement bytes")
            changed = target.stat()
            assert (changed.st_dev, changed.st_ino) == (original.st_dev, original.st_ino)
            mutated = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("raja_sts.authoring.os.open", mutate_before_open)

    with pytest.raises(GenesisAuthoringError, match="changed during freeze"):
        freeze_genesis_directory(tmp_path)
    assert mutated


def test_freeze_detects_same_inode_mutation_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_valid_genesis(tmp_path)
    target = tmp_path / "input" / "local.txt"
    target.write_bytes(b"a" * 1_000_000)
    original = target.stat()
    target_identity = (original.st_dev, original.st_ino)
    real_fdopen = os.fdopen
    mutated = False

    class MutatingReader:
        def __init__(self, handle: object) -> None:
            self._handle = handle

        def __enter__(self) -> MutatingReader:
            return self

        def __exit__(self, *args: object) -> object:
            return self._handle.__exit__(*args)  # type: ignore[attr-defined,no-any-return]

        def fileno(self) -> int:
            return self._handle.fileno()  # type: ignore[attr-defined,no-any-return]

        def read(self) -> bytes:
            nonlocal mutated
            prefix = self._handle.read(4096)  # type: ignore[attr-defined]
            with target.open("r+b") as writer:
                writer.seek(4096)
                writer.write(b"b" * (1_000_000 - 4096))
                writer.flush()
                os.fsync(writer.fileno())
            os.utime(
                target,
                ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000_000),
            )
            assert (os.fstat(self.fileno()).st_dev, os.fstat(self.fileno()).st_ino) == (
                original.st_dev,
                original.st_ino,
            )
            mutated = True
            return prefix + self._handle.read()  # type: ignore[attr-defined,no-any-return]

    def mutating_fdopen(file_descriptor: int, *args: object, **kwargs: object) -> object:
        handle = real_fdopen(file_descriptor, *args, **kwargs)
        observed = os.fstat(file_descriptor)
        if (observed.st_dev, observed.st_ino) == target_identity:
            return MutatingReader(handle)
        return handle

    monkeypatch.setattr("raja_sts.authoring.os.fdopen", mutating_fdopen)

    with pytest.raises(GenesisAuthoringError, match="changed during freeze"):
        freeze_genesis_directory(tmp_path)
    assert mutated


def test_metadata_is_detached_standard_json_with_string_object_keys(tmp_path: Path) -> None:
    write_valid_genesis(tmp_path)
    s3 = AuthoringS3()

    with pytest.raises(GenesisAuthoringError, match="keys must be strings"):
        _prepare(tmp_path, s3, user_meta={"nested": {1: "value"}})
    with pytest.raises(GenesisAuthoringError, match="standard JSON"):
        _prepare(tmp_path, s3, user_meta={"bad": float("nan")})
    with pytest.raises(GenesisAuthoringError, match="JSON object"):
        _prepare(tmp_path, s3, user_meta=object())
    assert s3.put_requests == []

    plan = _prepare(
        tmp_path,
        s3,
        user_meta={"owner": "operator", "nested": {"value": 1}},
    )
    first = plan.user_meta
    first["nested"]["value"] = 2
    assert plan.user_meta["nested"]["value"] == 1


def test_seal_input_collection_must_be_a_sequence_not_one_string(tmp_path: Path) -> None:
    write_valid_genesis(tmp_path)
    s3 = AuthoringS3()

    with pytest.raises(GenesisAuthoringError, match="sequence"):
        _prepare(tmp_path, s3, seal_inputs="not-a-sequence")
    assert s3.put_requests == []


def test_missing_source_key_uses_cached_manifest_and_refuses_before_writes(
    tmp_path: Path,
) -> None:
    write_valid_genesis(tmp_path)
    s3 = AuthoringS3()
    present = s3.add_source()
    missing = present.rsplit("path=", 1)[0] + "path=missing.txt"

    with pytest.raises(ManifestLoadError, match="contains no logical key"):
        _prepare(tmp_path, s3, seal_inputs=(present, missing))

    manifest_reads = [
        request for request in s3.get_requests if str(request["Key"]).startswith(".quilt/packages/")
    ]
    assert len(manifest_reads) == 1
    assert s3.put_requests == []


def test_generated_revision_id_refuses_naive_time() -> None:
    from datetime import datetime

    with pytest.raises(GenesisAuthoringError, match="timezone-aware"):
        generate_genesis_revision_id(datetime(2026, 8, 20, 12))


@pytest.mark.parametrize(
    "uri",
    [
        "quilt+s3://foreign-registry#package=p&top_hash={hash}&path=a\nb",
        "quilt+s3://foreign-registry#package=p&top_hash={hash}&path=a\rb",
        "quilt+s3://foreign-registry#package=p&top_hash={hash}&path=a\tb",
        "quilt+s3://foreign-registry#package=p&top_hash={hash}&path=a%7Fb",
        "quilt+s3://foreign-registry#package=p&top_hash={hash}&path=a%C2%85b",
        "quilt+s3://[broken#package=p&top_hash={hash}&path=a",
    ],
    ids=["newline", "carriage-return", "tab", "del", "c1-control", "bad-ipv6"],
)
def test_seal_input_parser_rejects_lossy_or_control_bearing_uris(uri: str) -> None:
    with pytest.raises(InvalidManifestURI):
        parse_seal_input_uri(uri.format(hash="b" * 64))


def test_metadata_keys_reject_terminal_control_characters(tmp_path: Path) -> None:
    write_valid_genesis(tmp_path)
    s3 = AuthoringS3()

    with pytest.raises(GenesisAuthoringError, match="must not contain controls"):
        _prepare(
            tmp_path,
            s3,
            user_meta={"owner": "operator", "review\nforged": "value"},
        )

    assert s3.put_requests == []


def test_root_directory_swap_cannot_escape_the_frozen_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    write_valid_genesis(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"outside")
    moved = tmp_path / "original-root"
    real_open = os.open
    swapped = False

    def swap_before_open(
        path: str | os.PathLike[str],
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        nonlocal swapped
        if not swapped and os.fspath(path) == os.fspath(root) and "dir_fd" not in kwargs:
            root.rename(moved)
            root.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("raja_sts.authoring.os.open", swap_before_open)

    with pytest.raises(GenesisAuthoringError, match="unable to open|changed during freeze"):
        freeze_genesis_directory(root)


def test_nested_directory_swap_cannot_escape_the_frozen_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    write_valid_genesis(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"outside")
    moved = root / "original-input"
    real_open = os.open
    swapped = False

    def swap_child_before_open(
        path: str | os.PathLike[str],
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        nonlocal swapped
        if not swapped and path == "input" and kwargs.get("dir_fd") is not None:
            (root / "input").rename(moved)
            (root / "input").symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("raja_sts.authoring.os.open", swap_child_before_open)

    with pytest.raises(GenesisAuthoringError, match="unable to open|changed during freeze"):
        freeze_genesis_directory(root)


def test_interrupted_retry_revalidates_changed_workflow_before_advancing_latest(
    tmp_path: Path,
) -> None:
    write_valid_genesis(tmp_path)
    s3 = AuthoringS3()
    plan = _prepare(tmp_path, s3)
    s3.fail_latest = True
    interrupted = publish_genesis_authoring(plan, s3_client=s3, timestamp=generate_time())
    assert interrupted.publication.status == "published-not-latest"
    write_count = len(s3.put_requests)

    s3.fail_latest = False
    s3.remote[("output-bucket", ".quilt/workflows/genesis-metadata.json", None)] = Stored(
        b'{"type":"object","required":["new-required-field"]}',
        "metadata-schema-v2",
    )

    with pytest.raises(WorkflowContractError, match="new-required-field"):
        publish_genesis_authoring(plan, s3_client=s3, timestamp=generate_time())

    assert len(s3.put_requests) == write_count
    assert not any(request["Key"].endswith("/latest") for request in s3.put_requests[write_count:])


@pytest.mark.parametrize("authority", ["@foreign-registry", "foreign-registry:"])
def test_seal_input_parser_rejects_empty_authority_markers(authority: str) -> None:
    uri = f"quilt+s3://{authority}#package=p&top_hash={'b' * 64}&path=evidence.txt"

    with pytest.raises(InvalidManifestURI, match="only the bucket name"):
        parse_seal_input_uri(uri)

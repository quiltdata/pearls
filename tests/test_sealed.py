from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

import pytest
from cli_support import (
    GENESIS_URI,
    HANDLER_SOURCE,
    TOP_HASH,
    FakeGenesisS3,
    GenesisFixture,
)

import raja_sts.sealed as sealed_module
from raja_sts import (
    HandlerContractError,
    InputIntegrityError,
    ManifestLoadError,
    SceneHandlerResult,
    load_sealed_handler,
    verify_sealed_handler,
)


def _verify(fixture: GenesisFixture, **kwargs: Any) -> Any:
    return verify_sealed_handler(
        GENESIS_URI,
        manifest_loader=fixture.loader(),
        genesis_s3_client=FakeGenesisS3(fixture),
        **kwargs,
    )


def _load(fixture: GenesisFixture, **kwargs: Any) -> Any:
    return load_sealed_handler(
        GENESIS_URI,
        manifest_loader=fixture.loader(),
        genesis_s3_client=FakeGenesisS3(fixture),
        **kwargs,
    )


def test_sealed_handler_is_read_by_exact_version_and_verified() -> None:
    fixture = GenesisFixture()
    reader = FakeGenesisS3(fixture)

    sealed = verify_sealed_handler(
        GENESIS_URI, manifest_loader=fixture.loader(), genesis_s3_client=reader
    )

    assert reader.read_keys == ["genesis/protocol/handler.py"]
    assert reader.get_requests[0]["VersionId"] == "version-protocol/handler.py"
    assert sealed.descriptor.logical_key == "protocol/handler.py"
    assert sealed.source_sha256 == hashlib.sha256(HANDLER_SOURCE).hexdigest()
    assert sealed.source_size == len(HANDLER_SOURCE)
    assert sealed.genesis.top_hash == TOP_HASH


def test_hash_verification_happens_before_the_bytes_can_run(tmp_path: Path) -> None:
    """A corrupted handler must be refused without its module body executing."""
    sentinel = tmp_path / "executed"
    source = f"open({str(sentinel)!r}, 'w').close()\n\n\ndef handler(*_):\n    pass\n".encode()

    with pytest.raises(InputIntegrityError, match="Content hash mismatch"):
        _verify(GenesisFixture(handler_source=source, corrupt_handler=True))

    assert not sentinel.exists()


def test_verification_alone_never_executes_the_sealed_bytes(tmp_path: Path) -> None:
    sentinel = tmp_path / "executed"
    source = f"open({str(sentinel)!r}, 'w').close()\n\n\ndef handler(*_):\n    pass\n".encode()
    fixture = GenesisFixture(handler_source=source)

    sealed = _verify(fixture)

    assert not sentinel.exists()
    assert sealed.source_sha256 == hashlib.sha256(source).hexdigest()

    sealed.load()

    # Loading is the documented point at which sealed code runs.
    assert sentinel.exists()


def test_verification_rejects_bytes_that_do_not_bind_a_handler_without_running_them(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "executed"
    source = f"open({str(sentinel)!r}, 'w').close()\n".encode()

    with pytest.raises(HandlerContractError, match="does not bind a module-level 'handler'"):
        _verify(GenesisFixture(handler_source=source))

    assert not sentinel.exists()


@pytest.mark.parametrize(
    "source",
    [
        b"def handler(inputs, session, context):\n    pass\n",
        b"async def handler(inputs, session, context):\n    pass\n",
        b"handler = lambda *_: None\n",
        b"from json import loads as handler\n",
        b"import json as handler\n",
        b"handler: object = None\n",
    ],
    ids=["def", "async-def", "assign", "import-from", "import-as", "annotated"],
)
def test_every_module_level_handler_binding_is_recognized_statically(source: bytes) -> None:
    assert _verify(GenesisFixture(handler_source=source)).source == source


def test_sealed_handler_report_carries_provenance_without_source_bytes() -> None:
    sealed = _load(GenesisFixture())

    report = sealed.report()

    assert report == {
        "logical_key": "protocol/handler.py",
        "bucket": "genesis-bucket",
        "key": "genesis/protocol/handler.py",
        "version_id": "version-protocol/handler.py",
        "quilt_hash": {
            "type": "SHA256",
            "value": hashlib.sha256(HANDLER_SOURCE).hexdigest(),
        },
        "source_sha256": hashlib.sha256(HANDLER_SOURCE).hexdigest(),
        "source_bytes": len(HANDLER_SOURCE),
        "attribute": "handler",
    }
    assert "source" not in report


def test_loaded_handler_executes_the_sealed_bytes() -> None:
    sealed = _load(GenesisFixture())

    class Context:
        control_prompt = "sealed prompt"

    result = sealed.handler({"input/prompt.txt": object()}, None, Context())

    assert isinstance(result, SceneHandlerResult)
    assert result.objects == {"result/answer.txt": "sealed prompt"}
    assert result.invocation == {"input_count": 1}


def test_sealed_module_is_registered_under_a_content_addressed_name() -> None:
    sealed = _load(GenesisFixture())

    expected = f"raja_sts._sealed_handler_{sealed.source_sha256[:16]}"
    origin = f"<sealed {GENESIS_URI}#protocol/handler.py>"

    assert expected in sys.modules
    module = sys.modules[expected]
    # No __file__: the bytes are not on disk, and pretending otherwise would make
    # tracebacks and coverage claim a source file that does not exist.
    assert not hasattr(module, "__file__")
    assert module.__pearls_origin__ == origin
    # Tracebacks from the sealed bytes name the pinned revision they came from.
    assert sealed.handler.__code__.co_filename == origin  # type: ignore[attr-defined]


def test_revision_without_the_conventional_handler_is_refused() -> None:
    with pytest.raises(ManifestLoadError, match="declares no sealed handler"):
        _load(GenesisFixture(handler_source=None))


def test_handler_key_override_is_honored() -> None:
    fixture = GenesisFixture(handler_key="protocol/supervisor.py")

    sealed = _load(fixture, handler_key="protocol/supervisor.py")

    assert sealed.descriptor.logical_key == "protocol/supervisor.py"
    with pytest.raises(ManifestLoadError, match="protocol/handler.py"):
        _load(fixture)


def test_handler_bytes_that_do_not_match_the_manifest_hash_are_refused() -> None:
    with pytest.raises(InputIntegrityError, match="Content hash mismatch"):
        _load(GenesisFixture(corrupt_handler=True))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (b"def handler(:\n", "not valid Python"),
        (b"handler = 1\nraise RuntimeError('sealed import blew up')\n", "failed to import"),
        (b"handler = 3\n", "callable 'handler'"),
        (b"other = 1\n", "does not bind a module-level"),
    ],
    ids=["syntax-error", "import-error", "not-callable", "absent"],
)
def test_unusable_sealed_bytes_are_refused(source: bytes, expected: str) -> None:
    with pytest.raises(HandlerContractError, match=expected):
        _load(GenesisFixture(handler_source=source))


@pytest.mark.parametrize(
    "source",
    [
        b"handler = 1\nraise RuntimeError('sealed import blew up')\n",
        b"handler = 3\n",
        b"import sys\ndel sys.modules[__name__]\nraise RuntimeError('unregistered itself')\n",
    ],
    ids=["import-error", "not-callable", "self-unregistering"],
)
def test_unusable_sealed_bytes_leave_no_module_registered(source: bytes) -> None:
    """A refusal must not leak a module, and must stay a typed refusal."""
    name = f"raja_sts._sealed_handler_{hashlib.sha256(source).hexdigest()[:16]}"

    with pytest.raises(HandlerContractError):
        _load(GenesisFixture(handler_source=source))

    assert name not in sys.modules


def test_default_client_comes_from_boto3_when_none_is_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = GenesisFixture()
    reader = FakeGenesisS3(fixture)
    requested: list[str] = []

    def fake_client(service: str) -> Any:
        requested.append(service)
        return reader

    monkeypatch.setattr(sealed_module.boto3, "client", fake_client)

    sealed = load_sealed_handler(GENESIS_URI, manifest_loader=fixture.loader())

    assert requested == ["s3"]
    assert sealed.descriptor.logical_key == "protocol/handler.py"

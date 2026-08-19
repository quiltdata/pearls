"""Opt-in proof against real AWS IAM/STS/S3 authorization.

This test never provisions infrastructure. It requires an administrator-created,
versioned sandbox, a pinned package, the base role, and disposable object URIs
supplied through the environment. It records request metadata but no credentials
or response bodies and uses the unrestricted base session as a positive baseline.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import ClientError

from raja_sts import mint

pytestmark = [
    pytest.mark.aws,
    pytest.mark.skipif(
        os.getenv("RAJA_STS_RUN_AWS_PROOF") != "1",
        reason="set RAJA_STS_RUN_AWS_PROOF=1 for the live authorization proof",
    ),
]

_REQUIRED_ENV = (
    "RAJA_STS_MANIFEST_URI",
    "RAJA_STS_SCOPE_FILE",
    "RAJA_STS_ROLE_ARN",
    "RAJA_STS_ALLOWED_READ",
    "RAJA_STS_WRONG_VERSION_READ",
    "RAJA_STS_DENIED_READ",
    "RAJA_STS_ALLOWED_WRITE",
    "RAJA_STS_DENIED_WRITE",
)


def _s3_target(uri: str) -> dict[str, str]:
    parsed = urlsplit(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError(f"invalid proof S3 URI: {uri!r}")
    target = {"Bucket": parsed.netloc, "Key": parsed.path.lstrip("/")}
    version = parse_qs(parsed.query).get("versionId")
    if version:
        target["VersionId"] = version[0]
    return target


def _record(transcript: Path, operation: str, outcome: str, details: dict[str, Any]) -> None:
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "operation": operation,
        "outcome": outcome,
        **details,
    }
    with transcript.open("a", encoding="utf-8") as output:
        output.write(json.dumps(entry, sort_keys=True) + "\n")


def _response_details(response: dict[str, Any]) -> dict[str, Any]:
    metadata = response["ResponseMetadata"]
    return {
        "http_status": metadata["HTTPStatusCode"],
        "request_id": metadata.get("RequestId"),
        "version_id": response.get("VersionId"),
    }


def _expect_denied(transcript: Path, operation: str, call: Any, **kwargs: Any) -> None:
    try:
        call(**kwargs)
    except ClientError as error:
        response = error.response
        code = response["Error"]["Code"]
        _record(
            transcript,
            operation,
            "denied",
            {
                "error_code": code,
                "http_status": response["ResponseMetadata"]["HTTPStatusCode"],
                "request_id": response["ResponseMetadata"].get("RequestId"),
            },
        )
        assert code in {"AccessDenied", "AccessDeniedException"}
    else:
        pytest.fail(f"{operation} unexpectedly succeeded")


def _assume_unrestricted_base(role_arn: str, scene_id: str) -> dict[str, str]:
    request: dict[str, Any] = {
        "RoleArn": role_arn,
        "RoleSessionName": f"{scene_id}-baseline"[:64],
        "DurationSeconds": 900,
    }
    if external_id := os.getenv("RAJA_STS_EXTERNAL_ID"):
        request["ExternalId"] = external_id
    response = boto3.client("sts").assume_role(**request)
    credentials = response["Credentials"]
    return {
        "aws_access_key_id": credentials["AccessKeyId"],
        "aws_secret_access_key": credentials["SecretAccessKey"],
        "aws_session_token": credentials["SessionToken"],
    }


def _get_and_close(client: Any, target: dict[str, str]) -> dict[str, Any]:
    response = client.get_object(**target)
    response["Body"].close()
    return response


def _assert_proof_targets(scope: dict[str, Any]) -> tuple[str, str]:
    package_name, output_bucket = scope["writable_package"].rsplit("@", 1)
    allowed_write = _s3_target(os.environ["RAJA_STS_ALLOWED_WRITE"])
    denied_write = _s3_target(os.environ["RAJA_STS_DENIED_WRITE"])
    assert "VersionId" not in allowed_write and "VersionId" not in denied_write
    assert allowed_write["Bucket"] == output_bucket
    assert allowed_write["Key"].startswith(f"{package_name}/")
    assert denied_write["Bucket"] != output_bucket or not any(
        denied_write["Key"].startswith(prefix)
        for prefix in (
            f"{package_name}/",
            ".quilt/packages/",
            f".quilt/named_packages/{package_name}/",
        )
    )
    return package_name, output_bucket


def test_live_scene_authorization_proof(tmp_path: Path) -> None:
    missing = [name for name in _REQUIRED_ENV if not os.getenv(name)]
    assert not missing, f"missing live proof configuration: {', '.join(missing)}"

    scope = json.loads(Path(os.environ["RAJA_STS_SCOPE_FILE"]).read_text(encoding="utf-8"))
    package_name, output_bucket = _assert_proof_targets(scope)
    scene_id = os.getenv("RAJA_STS_SCENE_ID", "raja-sts-proof")
    transcript = Path(os.getenv("RAJA_STS_TRANSCRIPT", tmp_path / "aws-proof-transcript.jsonl"))
    transcript.unlink(missing_ok=True)

    baseline_s3 = boto3.client(
        "s3",
        **_assume_unrestricted_base(os.environ["RAJA_STS_ROLE_ARN"], scene_id),
    )
    scoped = mint(
        os.environ["RAJA_STS_MANIFEST_URI"],
        scope,
        role_arn=os.environ["RAJA_STS_ROLE_ARN"],
        scene_id=scene_id,
        external_id=os.getenv("RAJA_STS_EXTERNAL_ID"),
    )
    scoped_s3 = boto3.client("s3", **scoped.boto3_kwargs())
    _record(
        transcript,
        "AssumeRole:scoped",
        "allowed",
        {
            "scene_id": scene_id,
            "assumed_role_arn": scoped.assumed_role_arn,
            "expiration": scoped.expiration.isoformat(),
            "packed_policy_size": scoped.packed_policy_size,
            "compilation": scoped.compilation.report(),
        },
    )

    allowed_read = _s3_target(os.environ["RAJA_STS_ALLOWED_READ"])
    wrong_version = _s3_target(os.environ["RAJA_STS_WRONG_VERSION_READ"])
    denied_read = _s3_target(os.environ["RAJA_STS_DENIED_READ"])
    allowed_write = _s3_target(os.environ["RAJA_STS_ALLOWED_WRITE"])
    denied_write = _s3_target(os.environ["RAJA_STS_DENIED_WRITE"])
    assert allowed_read.get("VersionId")
    assert wrong_version.get("VersionId")
    assert allowed_read["Bucket"] == wrong_version["Bucket"]
    assert allowed_read["Key"] == wrong_version["Key"]
    assert allowed_read["VersionId"] != wrong_version["VersionId"]

    cleanup_versions: list[tuple[str, str, str]] = []
    token = uuid4().hex
    output_targets = [
        allowed_write,
        {"Bucket": output_bucket, "Key": f".quilt/packages/proof-{token}"},
        {
            "Bucket": output_bucket,
            "Key": f".quilt/named_packages/{package_name}/proof-{token}",
        },
    ]
    try:
        for operation, target in (
            ("Baseline:GetObject:out-of-scope", denied_read),
            ("Baseline:GetObject:wrong-version", wrong_version),
        ):
            response = _get_and_close(baseline_s3, target)
            _record(transcript, operation, "allowed", _response_details(response))

        baseline_write = baseline_s3.put_object(**denied_write, Body=b"baseline authority proof")
        assert baseline_write.get("VersionId"), "proof bucket must have versioning enabled"
        cleanup_versions.append(
            (denied_write["Bucket"], denied_write["Key"], baseline_write["VersionId"])
        )
        _record(
            transcript,
            "Baseline:PutObject:outside-output",
            "allowed",
            _response_details(baseline_write),
        )

        allowed = _get_and_close(scoped_s3, allowed_read)
        _record(transcript, "GetObject:in-scope", "allowed", _response_details(allowed))
        _expect_denied(
            transcript,
            "GetObject:out-of-scope",
            scoped_s3.get_object,
            **denied_read,
        )
        _expect_denied(
            transcript,
            "GetObject:wrong-version",
            scoped_s3.get_object,
            **wrong_version,
        )

        for target in output_targets:
            response = scoped_s3.put_object(**target, Body=b"scoped output proof")
            assert response.get("VersionId"), "proof bucket must have versioning enabled"
            cleanup_versions.append((target["Bucket"], target["Key"], response["VersionId"]))
            _record(
                transcript,
                f"PutObject:in-scope:{target['Key']}",
                "allowed",
                _response_details(response),
            )

        _expect_denied(
            transcript,
            "PutObject:outside-output",
            scoped_s3.put_object,
            **denied_write,
            Body=b"scoped authority proof",
        )
        _expect_denied(
            transcript,
            "ListBucket:anywhere",
            scoped_s3.list_objects_v2,
            Bucket=output_bucket,
            MaxKeys=1,
        )
        _expect_denied(
            transcript,
            "DeleteObject:anywhere",
            scoped_s3.delete_object,
            Bucket=allowed_write["Bucket"],
            Key=allowed_write["Key"],
        )
        allowed_version = cleanup_versions[1][2]
        _expect_denied(
            transcript,
            "DeleteObjectVersion:anywhere",
            scoped_s3.delete_object,
            Bucket=allowed_write["Bucket"],
            Key=allowed_write["Key"],
            VersionId=allowed_version,
        )
    finally:
        for bucket, key, version_id in reversed(cleanup_versions):
            baseline_s3.delete_object(Bucket=bucket, Key=key, VersionId=version_id)

    assert transcript.exists()

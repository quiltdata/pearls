"""Opt-in proof against real AWS IAM/STS/S3 authorization.

This test never provisions infrastructure. It requires an administrator-created,
versioned sandbox, a pinned package, the base role, a separate cleanup role, and
disposable object URIs supplied through the environment. It records request
metadata but no credentials or response bodies. The unrestricted base session
provides positive read/write baselines but has no delete authority.
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
from quilt3 import Package

from pearls import SceneHandlerResult, mint, run_scene

pytestmark = [
    pytest.mark.aws,
    pytest.mark.skipif(
        os.getenv("PEARLS_RUN_AWS_PROOF") != "1",
        reason="set PEARLS_RUN_AWS_PROOF=1 for the live authorization proof",
    ),
]

_REQUIRED_ENV = (
    "PEARLS_MANIFEST_URI",
    "PEARLS_SCOPE_FILE",
    "PEARLS_ROLE_ARN",
    "PEARLS_CLEANUP_ROLE_ARN",
    "PEARLS_ALLOWED_READ",
    "PEARLS_CROSS_OBJECT_VERSION_READ",
    "PEARLS_WRONG_VERSION_READ",
    "PEARLS_DENIED_READ",
    "PEARLS_WORKFLOW_READ",
    "PEARLS_OUTPUT_LATEST_READ",
    "PEARLS_ALLOWED_WRITE",
    "PEARLS_DENIED_WRITE",
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


def _expect_missing(transcript: Path, operation: str, call: Any, **kwargs: Any) -> None:
    try:
        call(**kwargs)
    except ClientError as error:
        response = error.response
        code = response["Error"]["Code"]
        _record(
            transcript,
            operation,
            "not-found",
            {
                "error_code": code,
                "http_status": response["ResponseMetadata"]["HTTPStatusCode"],
                "request_id": response["ResponseMetadata"].get("RequestId"),
            },
        )
        assert code in {"NoSuchKey", "NoSuchVersion"}
    else:
        pytest.fail(f"{operation} unexpectedly returned an object")


def _assume_role(
    role_arn: str,
    scene_id: str,
    purpose: str,
    external_id: str | None,
) -> tuple[dict[str, str], str]:
    request: dict[str, Any] = {
        "RoleArn": role_arn,
        "RoleSessionName": f"{scene_id}-{purpose}"[:64],
        "DurationSeconds": 900,
    }
    if external_id:
        request["ExternalId"] = external_id
    response = boto3.client("sts").assume_role(**request)
    credentials = response["Credentials"]
    return (
        {
            "aws_access_key_id": credentials["AccessKeyId"],
            "aws_secret_access_key": credentials["SecretAccessKey"],
            "aws_session_token": credentials["SessionToken"],
        },
        response["AssumedRoleUser"]["Arn"],
    )


def _get_and_close(client: Any, target: dict[str, str]) -> dict[str, Any]:
    response = client.get_object(**target)
    response["Body"].close()
    return response


def _assert_proof_targets(scope: dict[str, Any]) -> tuple[str, str]:
    package_name, output_bucket = scope["writable_package"].rsplit("@", 1)
    allowed_write = _s3_target(os.environ["PEARLS_ALLOWED_WRITE"])
    denied_write = _s3_target(os.environ["PEARLS_DENIED_WRITE"])
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

    scope = json.loads(Path(os.environ["PEARLS_SCOPE_FILE"]).read_text(encoding="utf-8"))
    package_name, output_bucket = _assert_proof_targets(scope)
    scene_id = os.getenv("PEARLS_SCENE_ID", "pearls-proof")
    transcript = Path(os.getenv("PEARLS_TRANSCRIPT", tmp_path / "aws-proof-transcript.jsonl"))
    transcript.unlink(missing_ok=True)

    role_arn = os.environ["PEARLS_ROLE_ARN"]
    cleanup_role_arn = os.environ["PEARLS_CLEANUP_ROLE_ARN"]
    assert cleanup_role_arn != role_arn, "cleanup and Scene execution roles must differ"
    baseline_kwargs, baseline_assumed_role_arn = _assume_role(
        role_arn,
        scene_id,
        "baseline",
        os.getenv("PEARLS_EXTERNAL_ID"),
    )
    cleanup_kwargs, cleanup_assumed_role_arn = _assume_role(
        cleanup_role_arn,
        scene_id,
        "cleanup",
        os.getenv("PEARLS_CLEANUP_EXTERNAL_ID"),
    )
    baseline_s3 = boto3.client("s3", **baseline_kwargs)
    cleanup_s3 = boto3.client("s3", **cleanup_kwargs)
    scoped = mint(
        os.environ["PEARLS_MANIFEST_URI"],
        scope,
        role_arn=role_arn,
        scene_id=scene_id,
        external_id=os.getenv("PEARLS_EXTERNAL_ID"),
    )
    scoped_s3 = boto3.client("s3", **scoped.boto3_kwargs())
    _record(
        transcript,
        "AssumeRole:baseline",
        "allowed",
        {"assumed_role_arn": baseline_assumed_role_arn},
    )
    _record(
        transcript,
        "AssumeRole:cleanup",
        "allowed",
        {"assumed_role_arn": cleanup_assumed_role_arn},
    )
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

    allowed_read = _s3_target(os.environ["PEARLS_ALLOWED_READ"])
    cross_object_version = _s3_target(os.environ["PEARLS_CROSS_OBJECT_VERSION_READ"])
    wrong_version = _s3_target(os.environ["PEARLS_WRONG_VERSION_READ"])
    denied_read = _s3_target(os.environ["PEARLS_DENIED_READ"])
    workflow_read = _s3_target(os.environ["PEARLS_WORKFLOW_READ"])
    output_latest_read = _s3_target(os.environ["PEARLS_OUTPUT_LATEST_READ"])
    manifest_read = {
        "Bucket": scoped.compilation.manifest.bucket,
        "Key": f".quilt/packages/{scoped.compilation.manifest.top_hash}",
    }
    allowed_write = _s3_target(os.environ["PEARLS_ALLOWED_WRITE"])
    denied_write = _s3_target(os.environ["PEARLS_DENIED_WRITE"])
    assert allowed_read.get("VersionId")
    assert cross_object_version.get("VersionId") == allowed_read["VersionId"]
    assert (cross_object_version["Bucket"], cross_object_version["Key"]) != (
        allowed_read["Bucket"],
        allowed_read["Key"],
    )
    selected_locations = {(item.bucket, item.key) for item in scoped.compilation.selected_objects}
    assert (allowed_read["Bucket"], allowed_read["Key"]) in selected_locations
    assert (
        cross_object_version["Bucket"],
        cross_object_version["Key"],
    ) in selected_locations
    assert wrong_version.get("VersionId")
    assert allowed_read["Bucket"] == wrong_version["Bucket"]
    assert allowed_read["Key"] == wrong_version["Key"]
    assert allowed_read["VersionId"] != wrong_version["VersionId"]
    assert workflow_read["Bucket"] == output_bucket
    assert workflow_read["Key"].startswith(".quilt/workflows/")
    assert output_latest_read == {
        "Bucket": output_bucket,
        "Key": f".quilt/named_packages/{package_name}/latest",
    }

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
        _expect_denied(
            transcript,
            "Baseline:DeleteObjectVersion:anywhere",
            baseline_s3.delete_object,
            Bucket=denied_write["Bucket"],
            Key=denied_write["Key"],
            VersionId=baseline_write["VersionId"],
        )

        allowed = _get_and_close(scoped_s3, allowed_read)
        _record(transcript, "GetObject:in-scope", "allowed", _response_details(allowed))
        for operation, target in (
            ("GetObject:pinned-manifest", manifest_read),
            ("GetObject:output-workflow", workflow_read),
            ("GetObject:output-latest", output_latest_read),
        ):
            response = _get_and_close(scoped_s3, target)
            _record(transcript, operation, "allowed", _response_details(response))
        _expect_denied(
            transcript,
            "GetObject:out-of-scope",
            scoped_s3.get_object,
            **denied_read,
        )
        wrong = _get_and_close(scoped_s3, wrong_version)
        _record(
            transcript,
            "GetObject:wrong-version-inside-prefix",
            "allowed",
            _response_details(wrong),
        )
        _expect_missing(
            transcript,
            "GetObject:admitted-version-wrong-object",
            scoped_s3.get_object,
            **cross_object_version,
        )
        current = _get_and_close(
            scoped_s3,
            {"Bucket": allowed_read["Bucket"], "Key": allowed_read["Key"]},
        )
        _record(
            transcript,
            "GetObject:selected-current-version-inside-prefix",
            "allowed",
            _response_details(current),
        )
        for operation, target in (
            (
                "GetObject:sibling-manifest",
                {
                    "Bucket": scoped.compilation.manifest.bucket,
                    "Key": f".quilt/packages/not-selected-{token}",
                },
            ),
            (
                "GetObject:sibling-output-package",
                {
                    "Bucket": output_bucket,
                    "Key": f".quilt/named_packages/not-authorized-{token}/latest",
                },
            ),
            (
                "GetObject:non-latest-output-pointer",
                {
                    "Bucket": output_bucket,
                    "Key": f".quilt/named_packages/{package_name}/not-latest-{token}",
                },
            ),
        ):
            _expect_denied(transcript, operation, scoped_s3.get_object, **target)

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
            response = cleanup_s3.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
            _record(
                transcript,
                "Cleanup:DeleteObjectVersion",
                "allowed",
                {"bucket": bucket, "key": key, **_response_details(response)},
            )

    assert transcript.exists()


_RUNNER_REQUIRED_ENV = (
    "PEARLS_MANIFEST_URI",
    "PEARLS_RUNNER_SCOPE_FILE",
    "PEARLS_ROLE_ARN",
    "PEARLS_CLEANUP_ROLE_ARN",
    "PEARLS_RUNNER_PROMPT_KEY",
    "PEARLS_ALTERNATE_MODEL_ID",
)


def test_live_bounded_scene_runner_proof(tmp_path: Path) -> None:
    """Prove exact-model execution and no-list publication through run_scene()."""
    missing = [name for name in _RUNNER_REQUIRED_ENV if not os.getenv(name)]
    assert not missing, f"missing live runner proof configuration: {', '.join(missing)}"

    scope = json.loads(Path(os.environ["PEARLS_RUNNER_SCOPE_FILE"]).read_text())
    assert scope.get("bedrock_model"), "runner scope must select one exact Bedrock model"
    base_package, output_bucket = scope["writable_package"].rsplit("@", 1)
    token = uuid4().hex
    package_name = f"{base_package}/runner-proof-{token}"
    scope["writable_package"] = f"{package_name}@{output_bucket}"
    scene_id = os.getenv("PEARLS_SCENE_ID", "pearls-proof") + "-runner"
    transcript = Path(os.getenv("PEARLS_TRANSCRIPT", tmp_path / "aws-proof-transcript.jsonl"))

    cleanup_kwargs, cleanup_assumed_role_arn = _assume_role(
        os.environ["PEARLS_CLEANUP_ROLE_ARN"],
        scene_id,
        "cleanup",
        os.getenv("PEARLS_CLEANUP_EXTERNAL_ID"),
    )
    cleanup_s3 = boto3.client("s3", **cleanup_kwargs)
    result = None

    def handler(inputs: dict[str, Any], session: Any, context: Any) -> SceneHandlerResult:
        prompt_key = os.environ["PEARLS_RUNNER_PROMPT_KEY"]
        prompt = inputs[prompt_key].content.decode("utf-8")
        started_at = datetime.now(UTC).isoformat()
        response = session.client("bedrock-runtime").converse(
            modelId=context.bedrock_model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
        )
        text = response["output"]["message"]["content"][0]["text"]
        return SceneHandlerResult(
            objects={"result/prompt.txt": prompt, "result/response.txt": text},
            invocation={
                "model_id": response.get("modelId", context.bedrock_model_id),
                "request_id": response["ResponseMetadata"].get("RequestId"),
                "usage": response.get("usage"),
                "stop_reason": response.get("stopReason"),
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
            },
        )

    try:
        result = run_scene(
            manifest_uri=os.environ["PEARLS_MANIFEST_URI"],
            scope_spec=scope,
            role_arn=os.environ["PEARLS_ROLE_ARN"],
            scene_id=scene_id[:64],
            handler=handler,
            external_id=os.getenv("PEARLS_EXTERNAL_ID"),
        )
        package = Package.browse(
            package_name,
            registry=f"s3://{output_bucket}",
            top_hash=result.top_hash,
        )
        assert package.top_hash == result.top_hash
        assert result.publication.status == "published"
        assert result.publication.is_latest
        assert result.publication.revision_id == scene_id[:64]
        assert {key for key, _ in package.walk()} >= {
            "result/prompt.txt",
            "result/response.txt",
            "run/scene.json",
            "run/invocation.json",
        }
        _record(
            transcript,
            "RunScene:exact-model-publication",
            "allowed",
            {
                "scene_id": result.scene_id,
                "assumed_role_arn": result.assumed_role_arn,
                "output_package": package_name,
                "top_hash": result.top_hash,
                "revision_id": result.publication.revision_id,
                "publication_status": result.publication.status,
                "is_latest": result.publication.is_latest,
            },
        )

        denial = mint(
            os.environ["PEARLS_MANIFEST_URI"],
            scope,
            role_arn=os.environ["PEARLS_ROLE_ARN"],
            scene_id=f"{scene_id}-alternate"[:64],
            external_id=os.getenv("PEARLS_EXTERNAL_ID"),
        )
        denied_session = boto3.Session(
            region_name=str(scope["bedrock_model"]).split(":", 5)[3],
            **denial.boto3_kwargs(),
        )
        _expect_denied(
            transcript,
            "Converse:alternate-model",
            denied_session.client("bedrock-runtime").converse,
            modelId=os.environ["PEARLS_ALTERNATE_MODEL_ID"],
            messages=[{"role": "user", "content": [{"text": "authorization proof"}]}],
        )
    finally:
        prefixes = [
            f"{package_name}/",
            f".quilt/named_packages/{package_name}/",
        ]
        if result is not None:
            prefixes.append(f".quilt/packages/{result.top_hash}")
        for prefix in prefixes:
            paginator = cleanup_s3.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=output_bucket, Prefix=prefix):
                for version in page.get("Versions", []):
                    response = cleanup_s3.delete_object(
                        Bucket=output_bucket,
                        Key=version["Key"],
                        VersionId=version["VersionId"],
                    )
                    _record(
                        transcript,
                        "RunnerCleanup:DeleteObjectVersion",
                        "allowed",
                        {
                            "cleanup_assumed_role_arn": cleanup_assumed_role_arn,
                            "bucket": output_bucket,
                            "key": version["Key"],
                            **_response_details(response),
                        },
                    )

    assert transcript.exists()
